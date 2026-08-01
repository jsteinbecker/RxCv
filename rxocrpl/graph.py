"""
rxocrpl/graph.py

Concept ego-graph construction, bundling/consolidation, and product-link
discovery — extracted from views.py so ConceptGraphView stays thin and the
logic is unit-testable without a request.
"""

import re

from django.db.models import Q

from .models import Product, ProductRxNormMapping, RxNormConceptRelation

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

# Minimum number of same-rela, same-TTY leaf neighbors before collapsing
# them into a single expandable group node.
GRAPH_GROUP_THRESHOLD = 6


def bundle_concept_graph (nodes, edges, anchor_rxcui,
                          group_threshold=GRAPH_GROUP_THRESHOLD):
      """Reduce a raw ego-graph to something legible to render:

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
                  display_nodes.append({
                        "data": {
                              "id": group_id,
                              "label": f"+{len(members)} {tty or 'concepts'}",
                              "tty": tty,
                              "is_group": True,
                              "count": len(members),
                              "rela": rela,
                        }
                  })
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
                  display_edges.append({
                        "data": {
                              "id": f"ge__{group_id}",
                              "source": src,
                              "target": tgt,
                              "label": rela,
                              "count": len(members),
                        }
                  })
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
            display_edges.append({
                  "data": {
                        "id": f"e__{s}__{t}",
                        "source": s,
                        "target": t,
                        "label": relas[0] if len(relas) == 1
                                 else f"{len(relas)} relations",
                        "rela_list": relas,
                        "count": len(relas),
                  }
            })

      return display_nodes, display_edges, groups_meta


def graph_product_candidate_terms (concepts):
      useful_ttys = {"IN", "PIN", "BN"}
      stopwords = {
            "MG", "ML", "MCG", "G", "L", "UNT", "UNIT", "UNITS",
            "ORAL", "TABLET", "CAPSULE",
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


def build_concept_graph_context (concept):
      """Build the full template context for a concept's ego-graph page."""
      outbound = list(
            RxNormConceptRelation.objects.filter(source=concept)
            .select_related("target")
      )
      inbound = list(
            RxNormConceptRelation.objects.filter(target=concept)
            .select_related("source")
      )

      nodes = [{
            "data": {
                  "id": concept.rxcui,
                  "label": concept.name,
                  "tty": concept.tty,
                  "rank": TTY_RANK.get(concept.tty, 2),
                  "is_anchor": True,
                  "color": "#ffaaaa",
            }
      }]
      edges = []
      edge_rows = []
      seen_nodes = {concept.rxcui}
      concept_objects = {concept.rxcui: concept}
      in_counts = {concept.rxcui: 0}
      out_counts = {concept.rxcui: 0}

      def _add_node (c):
            if c.rxcui not in seen_nodes:
                  nodes.append({
                        "data": {
                              "id": c.rxcui,
                              "label": c.name,
                              "tty": c.tty,
                              "rank": TTY_RANK.get(c.tty, 2),
                        }
                  })
                  seen_nodes.add(c.rxcui)

      for rel in outbound:
            concept_objects[rel.target.rxcui] = rel.target
            out_counts[concept.rxcui] = out_counts.get(concept.rxcui, 0) + 1
            in_counts[rel.target.rxcui] = in_counts.get(rel.target.rxcui, 0) + 1
            _add_node(rel.target)
            edges.append({
                  "data": {
                        "id": f"e{rel.pk}",
                        "source": concept.rxcui,
                        "target": rel.target.rxcui,
                        "label": rel.rela,
                  }
            })
            edge_rows.append(rel)

      for rel in inbound:
            concept_objects[rel.source.rxcui] = rel.source
            out_counts[rel.source.rxcui] = out_counts.get(rel.source.rxcui, 0) + 1
            in_counts[concept.rxcui] = in_counts.get(concept.rxcui, 0) + 1
            _add_node(rel.source)
            edges.append({
                  "data": {
                        "id": f"e{rel.pk}",
                        "source": rel.source.rxcui,
                        "target": concept.rxcui,
                        "label": rel.rela,
                  }
            })
            edge_rows.append(rel)

      graph_node_rows = [
            {
                  "rxcui": n["data"]["id"],
                  "name": n["data"]["label"],
                  "tty": n["data"]["tty"],
                  "is_anchor": n["data"].get("is_anchor", False),
                  "in_count": in_counts.get(n["data"]["id"], 0),
                  "out_count": out_counts.get(n["data"]["id"], 0),
            }
            for n in nodes
      ]

      tty_counts = {}
      for row in graph_node_rows:
            tty_counts[row["tty"]] = tty_counts.get(row["tty"], 0) + 1
      tty_summary = [
            {"tty": tty, "count": count}
            for tty, count in sorted(tty_counts.items())
      ]

      confirmed_mappings = list(
            ProductRxNormMapping.objects.filter(rxcui__in=seen_nodes)
            .order_by("rxcui", "product_ndc")
      )
      mapped_ndcs = [m.product_ndc for m in confirmed_mappings]
      products_by_ndc = Product.objects.in_bulk(
            mapped_ndcs, field_name="product_ndc"
      )
      confirmed_product_links = [
            {
                  "mapping": m,
                  "product": products_by_ndc.get(m.product_ndc),
                  "is_anchor": m.rxcui == concept.rxcui,
            }
            for m in confirmed_mappings
      ]

      possible_product_links = []
      possible_ndcs = set(mapped_ndcs)
      for term in graph_product_candidate_terms(list(concept_objects.values())):
            candidates = Product.objects.filter(
                  Q(generic_name__icontains=term) | Q(brand_name__icontains=term)
            ).order_by("generic_name", "brand_name")[:20]
            for product in candidates:
                  if product.product_ndc in possible_ndcs:
                        continue
                  possible_product_links.append(
                        {"product": product, "matched_term": term}
                  )
                  possible_ndcs.add(product.product_ndc)
                  if len(possible_product_links) >= 30:
                        break
            if len(possible_product_links) >= 30:
                  break

      display_nodes, display_edges, groups_meta = bundle_concept_graph(
            nodes, edges, concept.rxcui
      )

      return {
            "graph_data": {
                  "nodes": display_nodes,
                  "edges": display_edges,
                  "groups": groups_meta,
            },
            "graph_summary": {
                  "node_count": len(nodes),
                  "edge_count": len(edges),
                  "displayed_node_count": len(display_nodes),
                  "displayed_edge_count": len(display_edges),
                  "group_count": len(groups_meta),
                  "confirmed_product_count": len(confirmed_product_links),
                  "possible_product_count": len(possible_product_links),
            },
            "tty_summary": tty_summary,
            "graph_node_rows": graph_node_rows,
            "edge_rows": edge_rows,
            "confirmed_product_links": confirmed_product_links,
            "possible_product_links": possible_product_links,
      }
