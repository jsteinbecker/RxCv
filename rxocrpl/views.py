import csv
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.forms import fields, forms
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from rxocrpl.dailymed import get_dailymed_url
from rxocrpl.integrity import check_for_products_without_ingredients

from .models import (
      DoseForm,
      Facility,
      Labeler,
      ListedIngredient,
      PackagedProduct,
      Product,
      ProductRxNormMapping,
      Role,
      RoleGrant,
      RxNormConcept,
      RxNormConceptRelation,
)


def index (request):
      total_concepts = RxNormConcept.objects.count()
      total_products = Product.objects.count()
      total_ingredients = ListedIngredient.objects.count()

      context = {
            "total_concepts": total_concepts,
            "total_products": total_products,
            "total_ingredients": total_ingredients,
            "products_without_ingredients": check_for_products_without_ingredients().count(),
      }
      return render(request, "rxocrpl/index.html", context)


def stats (request):
      total_concepts = RxNormConcept.objects.count()
      total_relations = RxNormConceptRelation.objects.count()
      total_products = Product.objects.count()
      total_ingredients = ListedIngredient.objects.count()
      total_packages = PackagedProduct.objects.count()

      context = {
            "total_concepts": total_concepts,
            "total_relations": total_relations,
            "total_products": total_products,
            "total_ingredients": total_ingredients,
            "total_packages": total_packages,
      }
      return render(request, "rxocrpl/stats.html", context)


def products_without_ingredients_view (request):
      products = check_for_products_without_ingredients()
      context = {
            "products": products,
            "count": products.count(),
      }
      return render(request, "rxocrpl/products_without_ingredients.html", context)


TTY_RANK = {
      "IN": 0,
      "PIN": 0,
      "SCDC": 1,
      "SBDC": 1,
      "SCD": 2,
      "SBD": 2,
      "SCDG": 3,
      "SBDG": 3,
      "BN": 4,
}

# Minimum number of same-rela, same-TTY leaf neighbors (i.e. nodes with no
# other connections in this ego graph) before they're collapsed into a single
# expandable group node instead of being drawn individually.
GRAPH_GROUP_THRESHOLD = 6


def _bundle_concept_graph (
          nodes, edges, anchor_rxcui, group_threshold=GRAPH_GROUP_THRESHOLD
):
      """Reduce a raw ego-graph (nodes/edges) to something legible to render:

      - parallel edges between the same node pair are merged into one edge
        (edge bundling), labeled with a count when more than one rela applies.
      - leaf neighbors (only connected to the anchor) that share the same
        direction/rela/tty are collapsed into a single group node the client
        can expand on demand (concept consolidation).
      """
      node_by_id = {n["data"]["id"]: n for n in nodes}

      degree = {}
      for e in edges:
            s, t = e["data"]["source"], e["data"]["target"]
            degree[s] = degree.get(s, 0) + 1
            degree[t] = degree.get(t, 0) + 1

      grouped_keys = {}
      direct_edges = []

      for e in edges:
            data = e["data"]
            s, t = data["source"], data["target"]
            if s == anchor_rxcui:
                  other, direction = t, "out"
            elif t == anchor_rxcui:
                  other, direction = s, "in"
            else:
                  other, direction = None, None

            if other is not None and degree.get(other) == 1:
                  tty = node_by_id[other]["data"].get("tty")
                  key = (direction, data["label"], tty)
                  grouped_keys.setdefault(key, []).append((other, data))
            else:
                  direct_edges.append(data)

      display_node_ids = {anchor_rxcui}
      display_nodes = [node_by_id[anchor_rxcui]]
      display_edges = []
      groups_meta = {}

      for (direction, rela, tty), members in grouped_keys.items():
            if len(members) >= group_threshold:
                  group_id = f"group__{direction}__{rela}__{tty or 'NA'}"
                  member_info = [
                        {
                              "id": nid,
                              "label": node_by_id[nid]["data"]["label"],
                              "tty": node_by_id[nid]["data"].get("tty"),
                        }
                        for nid, _ in members
                  ]
                  display_nodes.append(
                        {
                              "data": {
                                    "id": group_id,
                                    "label": f"+{len(members)} {tty or 'concepts'}",
                                    "tty": tty,
                                    "is_group": True,
                                    "count": len(members),
                                    "rela": rela,
                              }
                        }
                  )
                  groups_meta[group_id] = {
                        "rela": rela,
                        "direction": direction,
                        "tty": tty,
                        "members": member_info,
                  }
                  src, tgt = (
                        (anchor_rxcui, group_id)
                        if direction == "out"
                        else (group_id, anchor_rxcui)
                  )
                  display_edges.append(
                        {
                              "data": {
                                    "id": f"ge__{group_id}",
                                    "source": src,
                                    "target": tgt,
                                    "label": rela,
                                    "count": len(members),
                              }
                        }
                  )
            else:
                  for nid, edge_data in members:
                        if nid not in display_node_ids:
                              display_nodes.append(node_by_id[nid])
                              display_node_ids.add(nid)
                        direct_edges.append(edge_data)

      merged = {}
      for data in direct_edges:
            pair = (data["source"], data["target"])
            bucket = merged.setdefault(pair, {"relas": [], "ids": []})
            bucket["relas"].append(data["label"])
            bucket["ids"].append(data.get("id"))
            for nid in pair:
                  if nid not in display_node_ids and nid in node_by_id:
                        display_nodes.append(node_by_id[nid])
                        display_node_ids.add(nid)

      for (s, t), bucket in merged.items():
            relas = bucket["relas"]
            display_edges.append(
                  {
                        "data": {
                              "id": f"e__{s}__{t}",
                              "source": s,
                              "target": t,
                              "label": relas[0] if len(relas) == 1 else f"{len(relas)} relations",
                              "rela_list": relas,
                              "count": len(relas),
                        }
                  }
            )

      return display_nodes, display_edges, groups_meta


def _graph_product_candidate_terms (concepts):
      useful_ttys = {"IN", "PIN", "BN"}
      stopwords = {
            "MG",
            "ML",
            "MCG",
            "G",
            "L",
            "UNT",
            "UNIT",
            "UNITS",
            "ORAL",
            "TABLET",
            "CAPSULE",
      }
      terms = []

      for concept in concepts:
            if concept.tty not in useful_ttys or not concept.name:
                  continue
            term = re.sub(r"\[[^]]+]", "", concept.name).strip()
            if len(term) < 4 or term.upper() in stopwords:
                  continue
            if term not in terms:
                  terms.append(term)

      return terms[:8]


def _mapping_rows_for_product (product):
      rows = []
      seen = set()

      mapping = ProductRxNormMapping.objects.filter(
            product_ndc=product.product_ndc
      ).first()
      if mapping:
            rows.append(
                  {
                        "rxcui": mapping.rxcui,
                        "tty": mapping.tty,
                        "name": mapping.name,
                        "source": "ProductRxNormMapping",
                  }
            )
            seen.add(mapping.rxcui)

      payload = product.rxcui_mapping or {}
      if isinstance(payload, dict):
            entries = (
                  payload.values()
                  if any(isinstance(v, dict) for v in payload.values())
                  else [payload]
            )
            for entry in entries:
                  if not isinstance(entry, dict):
                        continue
                  rxcui = (
                            entry.get("rxcui") or entry.get("rxnormId") or entry.get("rxnorm_id")
                  )
                  if not rxcui or rxcui in seen:
                        continue
                  rows.append(
                        {
                              "rxcui": rxcui,
                              "tty": entry.get("tty"),
                              "name": entry.get("name") or entry.get("rxstring"),
                              "source": "Product.rxcui_mapping",
                        }
                  )
                  seen.add(rxcui)

      return rows


def concept_graph_view (request, rxcui):
      concept = get_object_or_404(RxNormConcept, rxcui=rxcui)

      # Get outbound and inbound relations
      outbound = list(RxNormConceptRelation.objects.filter(source=concept).select_related("target"))
      inbound = list(RxNormConceptRelation.objects.filter(target=concept).select_related("source"))

      nodes = [
            {
                  "data": {
                        "id": concept.rxcui,
                        "label": concept.name,
                        "tty": concept.tty,
                        "rank": TTY_RANK.get(concept.tty, 2),
                        "is_anchor": True,
                        "color": "#ffaaaa",
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
                  nodes.append(
                        {
                              "data": {
                                    "id": rel.target.rxcui,
                                    "label": rel.target.name,
                                    "tty": rel.target.tty,
                                    "rank": TTY_RANK.get(rel.target.tty, 2),
                              }
                        }
                  )
                  seen_nodes.add(rel.target.rxcui)

            edges.append(
                  {
                        "data": {
                              "id": f"e{rel.pk}",
                              "source": concept.rxcui,
                              "target": rel.target.rxcui,
                              "label": rel.rela,
                        }
                  }
            )
            edge_rows.append(rel)

      for rel in inbound:
            concept_objects[rel.source.rxcui] = rel.source
            out_counts[rel.source.rxcui] = out_counts.get(rel.source.rxcui, 0) + 1
            in_counts[concept.rxcui] = in_counts.get(concept.rxcui, 0) + 1
            if rel.source.rxcui not in seen_nodes:
                  nodes.append(
                        {
                              "data": {
                                    "id": rel.source.rxcui,
                                    "label": rel.source.name,
                                    "tty": rel.source.tty,
                                    "rank": TTY_RANK.get(rel.source.tty, 2),
                              }
                        }
                  )
                  seen_nodes.add(rel.source.rxcui)

            edges.append(
                  {
                        "data": {
                              "id": f"e{rel.pk}",
                              "source": rel.source.rxcui,
                              "target": concept.rxcui,
                              "label": rel.rela,
                        }
                  }
            )
            edge_rows.append(rel)

      graph_node_rows = []
      for node in nodes:
            data = node["data"]
            graph_node_rows.append(
                  {
                        "rxcui": data["id"],
                        "name": data["label"],
                        "tty": data["tty"],
                        "is_anchor": data.get("is_anchor", False),
                        "in_count": in_counts.get(data["id"], 0),
                        "out_count": out_counts.get(data["id"], 0),
                  }
            )

      tty_counts = {}
      for row in graph_node_rows:
            tty_counts[row["tty"]] = tty_counts.get(row["tty"], 0) + 1
      tty_summary = [
            {"tty": tty, "count": count} for tty, count in sorted(tty_counts.items())
      ]

      confirmed_mappings = list(
            ProductRxNormMapping.objects.filter(rxcui__in=seen_nodes).order_by(
                  "rxcui", "product_ndc"
            )
      )
      mapped_ndcs = [mapping.product_ndc for mapping in confirmed_mappings]
      products_by_ndc = Product.objects.in_bulk(mapped_ndcs, field_name="product_ndc")
      confirmed_product_links = [
            {
                  "mapping": mapping,
                  "product": products_by_ndc.get(mapping.product_ndc),
                  "is_anchor": mapping.rxcui == concept.rxcui,
            }
            for mapping in confirmed_mappings
      ]

      possible_product_links = []
      possible_ndcs = set(mapped_ndcs)
      concept_list = list(concept_objects.values())
      for term in _graph_product_candidate_terms(concept_list):
            candidates = Product.objects.filter(
                  Q(generic_name__icontains=term) | Q(brand_name__icontains=term)
            ).order_by("generic_name", "brand_name")[:20]
            for product in candidates:
                  if product.product_ndc in possible_ndcs:
                        continue
                  possible_product_links.append(
                        {
                              "product": product,
                              "matched_term": term,
                        }
                  )
                  possible_ndcs.add(product.product_ndc)
                  if len(possible_product_links) >= 30:
                        break
            if len(possible_product_links) >= 30:
                  break

      display_nodes, display_edges, groups_meta = _bundle_concept_graph(
            nodes, edges, concept.rxcui
      )

      graph_summary = {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "displayed_node_count": len(display_nodes),
            "displayed_edge_count": len(display_edges),
            "group_count": len(groups_meta),
            "confirmed_product_count": len(confirmed_product_links),
            "possible_product_count": len(possible_product_links),
      }

      context = {
            "concept": concept,
            "graph_data": {
                  "nodes": display_nodes,
                  "edges": display_edges,
                  "groups": groups_meta,
            },
            "graph_summary": graph_summary,
            "tty_summary": tty_summary,
            "graph_node_rows": graph_node_rows,
            "edge_rows": edge_rows,
            "confirmed_product_links": confirmed_product_links,
            "possible_product_links": possible_product_links,
      }

      if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse(context["graph_data"])

      return render(request, "rxocrpl/concept_graph.html", context)


@require_POST
def sync_concept_from_rxnorm (request, rxcui):
      from rxocrpl.rxgraph.pipeline import materialize_concept
      try:
            result = materialize_concept(rxcui)
      except Exception as exc:
            return JsonResponse({"status": "error", "message": str(exc)}, status=500)

      return JsonResponse({
            "status": "ok",
            "rxcui": rxcui,
            "concepts": len(result.concepts),
            "created_concepts": result.created_concepts,
            "relations": len(result.relations),
            "created_relations": result.created_relations,
      })


def concept_tty_list_view (request, tty):
      query = request.GET.get("q", "").strip()
      concepts = RxNormConcept.objects.filter(tty=tty).order_by("name")
      if query:
            concepts = concepts.filter(
                  Q(name__icontains=query) | Q(rxcui__icontains=query)
            )
      total = RxNormConcept.objects.filter(tty=tty).count()
      paginator = Paginator(concepts, 50)
      page_obj = paginator.get_page(request.GET.get("page"))
      context = {"tty": tty, "page_obj": page_obj, "query": query, "total": total}
      return render(request, "rxocrpl/concept_tty_list.html", context)


class ScdLookupForm(forms.Form):
      dfg = fields.ChoiceField(choices=DoseForm.choices, label="Dose Form Group")
      ingr = fields.CharField(max_length=100, label="Ingredient Name")


def scd_ndc_lookup_view (request):
      decoded_query = (
            'rxnorm.findRxcuiByString) (allsrc:"1", search:"9"): idGroup.rxnormId; '
            'rxnorm.getRelatedByType (expand:"psn", tty:" SCDF SBDF SCDFP SBDFP SCDG SBDG SCDGP"): '
            'relatedGroup.conceptGroup.conceptProperties.rxcui; rxnorm.getRelatedByType (expand:"", '
            'tty:" SCD GPCK"): relatedGroup.conceptGroup.conceptProperties.rxcui; rxnorm.getNDCs '
            "(): ndcGroup.ndcList.ndc"
      )

      return render(
            request, "rxocrpl/scd_ndc_lookup.html", {"decoded_query": decoded_query}
      )


def ndc_product_list_view (request):
      query = request.GET.get("q", "").strip()
      products = Product.objects.filter(active=True).order_by("generic_name")

      if query:
            products = products.filter(
                  Q(generic_name__icontains=query)
                  | Q(brand_name__icontains=query)
                  | Q(product_ndc__icontains=query)
            )

      paginator = Paginator(products, 25)
      page_obj = paginator.get_page(request.GET.get("page"))
      context = {
            "page_obj": page_obj,
            "query": query,
      }
      return render(request, "rxocrpl/ndc_product_list.html", context)


def ndc_product_detail_view (request, ndc):
      product = get_object_or_404(Product, product_ndc=ndc)
      packages = PackagedProduct.objects.filter(product=product)
      ingredients = ListedIngredient.objects.filter(product=product)
      confirmed_mappings = _mapping_rows_for_product(product)
      confirmed_rxcuis = {row["rxcui"] for row in confirmed_mappings}

      strengths = set()
      for ing in ingredients:
            num = re.match(r"\s*([\d.]+)", ing.strength or "")
            if num:
                  strengths.add(
                        num.group(1).rstrip("0").rstrip(".")
                  )  # normalize "10.0" -> "10"

      concepts = RxNormConcept.objects.filter(name__icontains=product.generic_name)

      dose_bearing_ttys = {"SCD", "SCDC", "BPCK", "GPCK"}

      def is_relevant (concept):
            if concept.tty not in dose_bearing_ttys:
                  return True
            name_nums = set(re.findall(r"([\d.]+)\s*MG", concept.name, re.IGNORECASE))
            name_nums = {n.rstrip("0").rstrip(".") for n in name_nums}
            return bool(name_nums & strengths)

      def concept_ingredient_count (name):
            # Count "/" before the first digit (strength section) to get number of drug components.
            # e.g. "Amoxicillin / Clavulanate 500 MG / 125 MG" → base="Amoxicillin / Clavulanate " → 2
            base = re.split(r"\d", name)[0]
            return base.count("/") + 1

      product_ingredient_count = ingredients.count()

      filtered = sorted(
            (c for c in concepts if c.rxcui not in confirmed_rxcuis and is_relevant(c)),
            key=lambda c: (c.tty, c.name),
      )

      close_concepts = [c for c in filtered if concept_ingredient_count(c.name) <= product_ingredient_count]
      obscure_concepts = [c for c in filtered if concept_ingredient_count(c.name) > product_ingredient_count]

      context = {
            "product": product,
            "packages": packages,
            "ingredients": ingredients,
            "confirmed_mappings": confirmed_mappings,
            "close_concepts": close_concepts,
            "obscure_concepts": obscure_concepts,
            "dailymed_url": get_dailymed_url(product.product_ndc),
      }
      return render(request, "rxocrpl/ndc_product_detail.html", context)


def labeler_list_view (request):
      query = request.GET.get("q", "").strip()
      labelers = Labeler.objects.filter(active=True).order_by("name")
      if query:
            labelers = labelers.filter(
                  Q(name__icontains=query) | Q(labeler_code__icontains=query)
            )
      paginator = Paginator(labelers, 50)
      page_obj = paginator.get_page(request.GET.get("page"))
      context = {"page_obj": page_obj, "query": query}
      return render(request, "rxocrpl/labeler_list.html", context)


def labeler_detail_view (request, labeler_code):
      labeler = get_object_or_404(Labeler, labeler_code=labeler_code)
      products = Product.objects.filter(labeler=labeler).order_by("generic_name")
      paginator = Paginator(products, 25)
      page_obj = paginator.get_page(request.GET.get("page"))
      context = {"labeler": labeler, "page_obj": page_obj}
      return render(request, "rxocrpl/labeler_detail.html", context)


def import_all_ndc_products (request):
      path = "/Users/jts/PycharmProjects/RxCv/rxocrpl/db/ndcproduct.csv"
      with open(path, newline="") as csvfile:
            reader = csv.DictReader(csvfile)
            print(f"fieldnames: {[f for f in reader.fieldnames]}")  # ty:ignore[not-iterable]
            for row in reader:
                  Product.objects.update_or_create(
                        product_ndc=row["PRODUCTNDC"],
                        defaults={
                              "generic_name": row["NONPROPRIETARYNAME"],
                              "brand_name": row["PROPRIETARYNAME"],
                              "labeler_name": row["LABELERNAME"],
                              "dosage_form": row["DOSAGEFORMNAME"],
                              "route": row["ROUTENAME"],
                              "active_ingredients": [
                                    {
                                          "ingredient": row["SUBSTANCENAME"],
                                          "strength": row["ACTIVE_NUMERATOR_STRENGTH"],
                                          "unit": row["ACTIVE_INGRED_UNIT"],
                                    }
                              ],
                              "rxcui_mapping": {},
                        },
                  )
      return JsonResponse(
            {"status": "success", "message": "NDC products imported successfully."}
      )


def import_all_packages (request):
      path = "/Users/jts/PycharmProjects/RxCv/rxocrpl/db/package.txt"
      with open(path, newline="") as csvfile:
            reader = csv.DictReader(csvfile, delimiter="\t")
            print(f"fieldnames: {[f for f in reader.fieldnames]}")  # ty:ignore[not-iterable]
            for row in reader:
                  product_ndc = row["PRODUCTNDC"]
                  product = Product.objects.filter(product_ndc=product_ndc).first()
                  if product:
                        PackagedProduct.objects.update_or_create(
                              product=product,
                              package_code=row["NDCPACKAGECODE"].strip().split("-")[-1],
                              defaults={
                                    "description": row["PACKAGEDESCRIPTION"],
                              },
                        )
      return JsonResponse(
            {"status": "success", "message": "NDC packages imported successfully."}
      )


def ingredients_dict_to_model (request):
      """
      Pulls the dict .active_ingredients from json field on Product, and creates a related model for each ingredient,
      with a foreign key back to Product.
      """
      products = Product.objects.all()
      for product in products:
            ingredients = product.active_ingredients
            if ingredients:
                  for ingredient in ingredients:
                        ListedIngredient.objects.update_or_create(
                              product=product,
                              name=ingredient.get("ingredient", ingredient.get("name")),
                              defaults={
                                    "strength": ingredient["strength"],
                                    "unit": ingredient["unit"],
                              },
                        )
      return JsonResponse(
            {"status": "success", "message": "Ingredients imported successfully."}
      )


def facility_list_view (request):
      facilities = Facility.objects.select_related("organization", "parent").order_by("name")
      facility_data = []
      for f in facilities:
            admins = f.current_admins
            facility_data.append({
                  "facility": f,
                  "admins": admins,
                  "is_unclaimed": not admins.exists(),
            })
      return render(request, "rxocrpl/facility_list.html", {"facility_data": facility_data})


@login_required
def claim_facility_view (request, pk):
      facility = get_object_or_404(Facility, pk=pk)
      current_admins = facility.current_admins

      if current_admins.exists():
            return render(request, "rxocrpl/facility_claim.html", {
                  "facility": facility,
                  "state": "already_claimed",
                  "admins": current_admins,
            })

      if request.user.facility_id and request.user.facility_id != facility.pk:
            return render(request, "rxocrpl/facility_claim.html", {
                  "facility": facility,
                  "state": "wrong_facility",
                  "your_facility": request.user.facility,
            })

      if request.method == "POST":
            if not request.user.facility_id:
                  request.user.facility = facility
                  request.user.save(update_fields=["facility"])

            role, _ = Role.objects.get_or_create(
                  name="facility_admin",
                  defaults={"description": "Facility administrator"},
            )
            already_admin = RoleGrant.objects.filter(
                  user=request.user, facility=facility, role=role, revoked_at__isnull=True
            ).exists()
            if not already_admin:
                  grant = RoleGrant(
                        user=request.user,
                        role=role,
                        facility=facility,
                        granted_by=None,
                        reason="system_bootstrap",
                  )
                  grant.save()

            messages.success(request, f"You are now the admin for {facility}.")
            return render(request, "rxocrpl/facility_claim.html", {
                  "facility": facility,
                  "state": "success",
            })

      return render(request, "rxocrpl/facility_claim.html", {
            "facility": facility,
            "state": "confirm",
            "existing_profile": request.user if request.user.facility_id else None,
      })
