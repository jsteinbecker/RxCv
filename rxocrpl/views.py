import csv
import json
import re

from django.core.paginator import Paginator
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.forms import forms, fields
from django.db.models import Q

from .models import (
      RxNormConcept,
      RxNormConceptRelation,
      DoseForm,
      Product,
      ListedIngredient,
      PackagedProduct,
      Labeler,
      ProductRxNormMapping,
)


def index(request):
      total_concepts = RxNormConcept.objects.count()
      total_products = Product.objects.count()
      total_ingredients = ListedIngredient.objects.count()

      context = {
            'total_concepts': total_concepts,
            'total_products': total_products,
            'total_ingredients': total_ingredients,
      }
      return render(request, 'rxocrpl/index.html', context)


def stats(request):
      total_concepts = RxNormConcept.objects.count()
      total_relations = RxNormConceptRelation.objects.count()
      total_products = Product.objects.count()
      total_ingredients = ListedIngredient.objects.count()
      total_packages = PackagedProduct.objects.count()

      context = {
            'total_concepts': total_concepts,
            'total_relations': total_relations,
            'total_products': total_products,
            'total_ingredients': total_ingredients,
            'total_packages': total_packages
      }
      return render(request, 'rxocrpl/stats.html', context)


TTY_RANK = {
      'IN': 0,
      'PIN': 0,
      'SCDC': 1,
      'SBDC': 1,
      'SCD': 2,
      'SBD': 2,
      'SCDG': 3,
      'SBDG': 3,
      'BN': 4,
}


def _graph_product_candidate_terms(concepts):
      useful_ttys = {'IN', 'PIN', 'BN'}
      stopwords = {'MG', 'ML', 'MCG', 'G', 'L', 'UNT', 'UNIT', 'UNITS', 'ORAL', 'TABLET', 'CAPSULE'}
      terms = []

      for concept in concepts:
            if concept.tty not in useful_ttys or not concept.name:
                  continue
            term = re.sub(r'\[[^\]]+\]', '', concept.name).strip()
            if len(term) < 4 or term.upper() in stopwords:
                  continue
            if term not in terms:
                  terms.append(term)

      return terms[:8]


def _mapping_rows_for_product(product):
      rows = []
      seen = set()

      mapping = ProductRxNormMapping.objects.filter(product_ndc=product.product_ndc).first()
      if mapping:
            rows.append({
                  'rxcui': mapping.rxcui,
                  'tty': mapping.tty,
                  'name': mapping.name,
                  'source': 'ProductRxNormMapping',
            })
            seen.add(mapping.rxcui)

      payload = product.rxcui_mapping or {}
      if isinstance(payload, dict):
            entries = payload.values() if any(isinstance(v, dict) for v in payload.values()) else [payload]
            for entry in entries:
                  if not isinstance(entry, dict):
                        continue
                  rxcui = entry.get('rxcui') or entry.get('rxnormId') or entry.get('rxnorm_id')
                  if not rxcui or rxcui in seen:
                        continue
                  rows.append({
                        'rxcui': rxcui,
                        'tty': entry.get('tty'),
                        'name': entry.get('name') or entry.get('rxstring'),
                        'source': 'Product.rxcui_mapping',
                  })
                  seen.add(rxcui)

      return rows


def concept_graph_view(request, rxcui):
      concept = get_object_or_404(RxNormConcept, rxcui=rxcui)

      # Get outbound and inbound relations
      outbound = list(RxNormConceptRelation.objects.filter(source=concept).select_related('target'))
      inbound = list(RxNormConceptRelation.objects.filter(target=concept).select_related('source'))

      nodes = [
            {
                  'data': {
                        'id': concept.rxcui,
                        'label': concept.name,
                        'tty': concept.tty,
                        'rank': TTY_RANK.get(concept.tty, 2),
                        'is_anchor': True,
                        'color': '#ffaaaa'
                  }
            }
      ]

      edges = []
      edge_rows = []

      seen_nodes = {concept.rxcui}
      concept_objects = {concept.rxcui: concept}
      in_counts = {concept.rxcui: 0}
      out_counts = {concept.rxcui: 0}

      for rel in outbound:
            concept_objects[rel.target.rxcui] = rel.target
            out_counts[concept.rxcui] = out_counts.get(concept.rxcui, 0) + 1
            in_counts[rel.target.rxcui] = in_counts.get(rel.target.rxcui, 0) + 1
            if rel.target.rxcui not in seen_nodes:
                  nodes.append({
                        'data': {
                              'id': rel.target.rxcui,
                              'label': rel.target.name,
                              'tty': rel.target.tty,
                              'rank': TTY_RANK.get(rel.target.tty, 2)
                        }
                  })
                  seen_nodes.add(rel.target.rxcui)

            edges.append({
                  'data': {
                        'id': f"e{rel.id}",
                        'source': concept.rxcui,
                        'target': rel.target.rxcui,
                        'label': rel.rela
                  }
            })
            edge_rows.append(rel)

      for rel in inbound:
            concept_objects[rel.source.rxcui] = rel.source
            out_counts[rel.source.rxcui] = out_counts.get(rel.source.rxcui, 0) + 1
            in_counts[concept.rxcui] = in_counts.get(concept.rxcui, 0) + 1
            if rel.source.rxcui not in seen_nodes:
                  nodes.append({
                        'data': {
                              'id': rel.source.rxcui,
                              'label': rel.source.name,
                              'tty': rel.source.tty,
                              'rank': TTY_RANK.get(rel.source.tty, 2)
                        }
                  })
                  seen_nodes.add(rel.source.rxcui)

            edges.append({
                  'data': {
                        'id': f"e{rel.id}",
                        'source': rel.source.rxcui,
                        'target': concept.rxcui,
                        'label': rel.rela
                  }
            })
            edge_rows.append(rel)

      graph_node_rows = []
      for node in nodes:
            data = node['data']
            graph_node_rows.append({
                  'rxcui': data['id'],
                  'name': data['label'],
                  'tty': data['tty'],
                  'is_anchor': data.get('is_anchor', False),
                  'in_count': in_counts.get(data['id'], 0),
                  'out_count': out_counts.get(data['id'], 0),
            })

      tty_counts = {}
      for row in graph_node_rows:
            tty_counts[row['tty']] = tty_counts.get(row['tty'], 0) + 1
      tty_summary = [{'tty': tty, 'count': count} for tty, count in sorted(tty_counts.items())]

      confirmed_mappings = list(
            ProductRxNormMapping.objects.filter(rxcui__in=seen_nodes).order_by('rxcui', 'product_ndc')
      )
      mapped_ndcs = [mapping.product_ndc for mapping in confirmed_mappings]
      products_by_ndc = Product.objects.in_bulk(mapped_ndcs, field_name='product_ndc')
      confirmed_product_links = [
            {
                  'mapping': mapping,
                  'product': products_by_ndc.get(mapping.product_ndc),
                  'is_anchor': mapping.rxcui == concept.rxcui,
            }
            for mapping in confirmed_mappings
      ]

      possible_product_links = []
      possible_ndcs = set(mapped_ndcs)
      concept_list = list(concept_objects.values())
      for term in _graph_product_candidate_terms(concept_list):
            candidates = Product.objects.filter(
                  Q(generic_name__icontains=term) | Q(brand_name__icontains=term)
            ).order_by('generic_name', 'brand_name')[:20]
            for product in candidates:
                  if product.product_ndc in possible_ndcs:
                        continue
                  possible_product_links.append({
                        'product': product,
                        'matched_term': term,
                  })
                  possible_ndcs.add(product.product_ndc)
                  if len(possible_product_links) >= 30:
                        break
            if len(possible_product_links) >= 30:
                  break

      graph_summary = {
            'node_count': len(nodes),
            'edge_count': len(edges),
            'confirmed_product_count': len(confirmed_product_links),
            'possible_product_count': len(possible_product_links),
      }

      context = {
            'concept': concept,
            'graph_data': {
                  'nodes': nodes,
                  'edges': edges
            },
            'graph_summary': graph_summary,
            'tty_summary': tty_summary,
            'graph_node_rows': graph_node_rows,
            'edge_rows': edge_rows,
            'confirmed_product_links': confirmed_product_links,
            'possible_product_links': possible_product_links,
      }

      if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse(context['graph_data'])

      return render(request, 'rxocrpl/concept_graph.html', context)


def concept_tty_list_view(request, tty):
      concepts = RxNormConcept.objects.filter(tty=tty).order_by('name')
      context = {
            'tty': tty,
            'concepts': concepts
      }
      return render(request, 'rxocrpl/concept_tty_list.html', context)


class ScdLookupForm(forms.Form):
      dfg = fields.ChoiceField(choices=DoseForm.choices, label="Dose Form Group")
      ingr = fields.CharField(max_length=100, label="Ingredient Name")


def scd_ndc_lookup_view(request):
      decoded_query = ('rxnorm.findRxcuiByString) (allsrc:"1", search:"9"): idGroup.rxnormId; '
                       'rxnorm.getRelatedByType (expand:"psn", tty:" SCDF SBDF SCDFP SBDFP SCDG SBDG SCDGP"): '
                       'relatedGroup.conceptGroup.conceptProperties.rxcui; rxnorm.getRelatedByType (expand:"", '
                       'tty:" SCD GPCK"): relatedGroup.conceptGroup.conceptProperties.rxcui; rxnorm.getNDCs '
                       '(): ndcGroup.ndcList.ndc')


def ndc_product_list_view(request):
      query = request.GET.get('q', '').strip()
      products = Product.objects.all().order_by('generic_name', 'brand_name')

      if query:
            products = products.filter(
                  Q(generic_name__icontains=query) |
                  Q(brand_name__icontains=query) |
                  Q(product_ndc__icontains=query)
            )

      paginator = Paginator(products, 25)
      page_obj = paginator.get_page(request.GET.get('page'))
      context = {
            'page_obj': page_obj,
            'query': query,
      }
      return render(request, 'rxocrpl/ndc_product_list.html', context)


def ndc_product_detail_view(request, ndc):
      product = get_object_or_404(Product, product_ndc=ndc)
      packages = PackagedProduct.objects.filter(product=product)
      ingredients = ListedIngredient.objects.filter(product=product)
      confirmed_mappings = _mapping_rows_for_product(product)
      confirmed_rxcuis = {row['rxcui'] for row in confirmed_mappings}

      strengths = set()
      for ing in ingredients:
            num = re.match(r'\s*([\d.]+)', ing.strength or '')
            if num:
                  strengths.add(num.group(1).rstrip('0').rstrip('.'))  # normalize "10.0" -> "10"

      concepts = RxNormConcept.objects.filter(name__icontains=product.generic_name)

      dose_bearing_ttys = {'SCD', 'SCDC', 'BPCK', 'GPCK'}

      def is_relevant(concept):
            if concept.tty not in dose_bearing_ttys:
                  return True
            name_nums = set(re.findall(r'([\d.]+)\s*MG', concept.name, re.IGNORECASE))
            name_nums = {n.rstrip('0').rstrip('.') for n in name_nums}
            return bool(name_nums & strengths)

      possible_concepts = sorted(
            (c for c in concepts if c.rxcui not in confirmed_rxcuis and is_relevant(c)),
            key=lambda c: (c.tty, c.name)
      )

      context = {
            'product': product,
            'packages': packages,
            'ingredients': ingredients,
            'confirmed_mappings': confirmed_mappings,
            'possible_concepts': possible_concepts,
      }
      return render(request, 'rxocrpl/ndc_product_detail.html', context)


def labeler_list_view(request):
      labelers = Labeler.objects.filter(active=True).order_by('name')
      paginator = Paginator(labelers, 50)
      page_obj = paginator.get_page(request.GET.get('page'))
      context = {'page_obj': page_obj}
      return render(request, 'rxocrpl/labeler_list.html', context)


def labeler_detail_view(request, labeler_code):
      labeler = get_object_or_404(Labeler, labeler_code=labeler_code)
      products = Product.objects.filter(labeler=labeler).order_by('generic_name')
      paginator = Paginator(products, 25)
      page_obj = paginator.get_page(request.GET.get('page'))
      context = {'labeler': labeler, 'page_obj': page_obj}
      return render(request, 'rxocrpl/labeler_detail.html', context)


def import_all_ndc_products(request):
      path = "/Users/jts/PycharmProjects/RxCv/rxocrpl/db/ndcproduct.csv"
      with open(path, newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            print(f"fieldnames: {[f for f in reader.fieldnames]}")
            for row in reader:
                  Product.objects.update_or_create(
                        product_ndc=row['PRODUCTNDC'],
                        defaults={
                              'generic_name': row['NONPROPRIETARYNAME'],
                              'brand_name': row['PROPRIETARYNAME'],
                              'labeler_name': row['LABELERNAME'],
                              'dosage_form': row['DOSAGEFORMNAME'],
                              'route': row['ROUTENAME'],
                              'active_ingredients': [
                                    {'ingredient': row['SUBSTANCENAME'],
                                     'strength': row['ACTIVE_NUMERATOR_STRENGTH'],
                                     'unit': row['ACTIVE_INGRED_UNIT']}
                              ],
                              'rxcui_mapping': {}
                        }
                  )
      return JsonResponse({'status': 'success', 'message': 'NDC products imported successfully.'})


def import_all_packages(request):
      path = "/Users/jts/PycharmProjects/RxCv/rxocrpl/db/package.txt"
      with open(path, newline='') as csvfile:
            reader = csv.DictReader(csvfile, delimiter='\t')
            print(f"fieldnames: {[f for f in reader.fieldnames]}")
            for row in reader:
                  product_ndc = row['PRODUCTNDC']
                  product = Product.objects.filter(product_ndc=product_ndc).first()
                  if product:
                        package = PackagedProduct.objects.update_or_create(
                              product=product,
                              package_code=row['NDCPACKAGECODE'].strip().split('-')[-1],
                              defaults={'description': row['PACKAGEDESCRIPTION'], }
                        )
      return JsonResponse({'status': 'success', 'message': 'NDC packages imported successfully.'})


def ingredients_dict_to_model(request):
      """
      Pulls the dict .active_ingredients from json field on Product, and creates a related model for each ingredient, with a foreign key back to Product.
      """
      products = Product.objects.all()
      for product in products:
            ingredients = product.active_ingredients
            if ingredients:
                  for ingredient in ingredients:
                        ListedIngredient.objects.update_or_create(
                              product=product,
                              name=ingredient.get('ingredient', ingredient.get('name')),
                              defaults={
                                    'strength': ingredient['strength'],
                                    'unit': ingredient['unit']
                              }
                        )
      return JsonResponse({'status': 'success', 'message': 'Ingredients imported successfully.'})
