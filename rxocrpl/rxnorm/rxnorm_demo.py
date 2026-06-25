"""examples/rxnorm_demo.py — quick smoke-test / demo of the rxnorm package.

Run from the repo root:
    python -m examples.rxnorm_demo

or:
    python examples/rxnorm_demo.py
"""

from __future__ import annotations

import json

from rxocrpl import rxnorm
from rxocrpl.rxnorm import (
      get_all_rxcui,
      get_concept,
      get_concept_status,
      get_quantified_forms,
      get_rxnorm_enrichment,
      get_volume_group_key,
)


def demo_basic_ndc(ndc: str) -> None:
      print(f"\n{'=' * 60}")
      print(f"NDC: {ndc}")
      print(f"{'=' * 60}")

      all_cui = get_all_rxcui(ndc)
      print("All RxCUI scopes:", all_cui)

      concept_entry = all_cui.get(rxnorm.RelatedLevel.CONCEPT)
      if not concept_entry:
            print("  → no concept-level CUI found")
            return

      concept_rxcui = concept_entry[0][0]
      concept = get_concept(concept_rxcui)
      print(f"Concept:         {concept}")
      print(f"Is quantified:   {concept.is_quantified if concept else 'n/a'}")
      print(f"Status:          {get_concept_status(concept_rxcui)}")


def demo_volume_grouping(ndc: str) -> None:
      print(f"\n{'=' * 60}")
      print(f"Volume grouping for NDC: {ndc}")
      print(f"{'=' * 60}")

      all_cui = get_all_rxcui(ndc)
      concept_entry = all_cui.get(rxnorm.RelatedLevel.CONCEPT)
      if not concept_entry:
            print("  → no concept-level CUI found")
            return

      concept_rxcui = concept_entry[0][0]
      concept = get_concept(concept_rxcui)
      if not concept or not concept.is_quantified:
            print("  → concept is not a quantified form; nothing to group")
            return

      group = get_volume_group_key(concept_rxcui)
      print(f"Volume group:    {group}")
      print(f"Group key:       {group.key}")
 
      if group.kind == "scd" and group.base:
            variants = get_quantified_forms(group.base.rxcui)
            print(f"Variants ({len(variants)}):")
            for v in variants:
                  print(f"  {v}")


def demo_enrichment(ndc: str) -> None:
      print(f"\n{'=' * 60}")
      print(f"Full enrichment for NDC: {ndc}")
      print(f"{'=' * 60}")
      result = get_rxnorm_enrichment(ndc)
      print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
      # Basic concept resolution
      ndc = "0641-0497"
      demo_basic_ndc(ndc)
      demo_volume_grouping(ndc)
      demo_enrichment(ndc)

      # Volume-variant grouping for a sodium phosphate product
      sodium_phos_ndc = "0517-7305"
      demo_basic_ndc(sodium_phos_ndc)
      demo_volume_grouping(sodium_phos_ndc)
      demo_enrichment(sodium_phos_ndc)

      print(rxnorm.get_quantified_forms("335591"))
      print(get_rxnorm_enrichment("0641-0497-01"))

      demo_enrichment("0641-0497")
      demo_basic_ndc("0641-0497")
