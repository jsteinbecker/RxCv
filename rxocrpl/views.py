from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.forms import forms, fields
from .models import RxNormConcept, RxNormConceptRelation, DoseForm


def concept_graph_view(request, rxcui):
      concept = get_object_or_404(RxNormConcept, rxcui=rxcui)

      # Get outbound and inbound relations
      outbound = RxNormConceptRelation.objects.filter(source=concept).select_related('target')
      inbound = RxNormConceptRelation.objects.filter(target=concept).select_related('source')

      nodes = [
            {
                  'data': {
                        'id': concept.rxcui,
                        'label': f"{concept.name}\n({concept.tty})",
                        'tty': concept.tty,
                        'color': '#ffaaaa'  # Main node color
                  }
            }
      ]

      edges = []

      seen_nodes = {concept.rxcui}

      for rel in outbound:
            if rel.target.rxcui not in seen_nodes:
                  nodes.append({
                        'data': {
                              'id': rel.target.rxcui,
                              'label': f"{rel.target.name}\n({rel.target.tty})",
                              'tty': rel.target.tty
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

      for rel in inbound:
            if rel.source.rxcui not in seen_nodes:
                  nodes.append({
                        'data': {
                              'id': rel.source.rxcui,
                              'label': f"{rel.source.name}\n({rel.source.tty})",
                              'tty': rel.source.tty
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

      context = {
            'concept': concept,
            'graph_data': {
                  'nodes': nodes,
                  'edges': edges
            }
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
