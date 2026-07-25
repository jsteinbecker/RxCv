import re
from django.db import transaction
from django.db.models import Q
from rxocrpl.models import RxNormConcept, RxNormConceptRelation

import re
from django.db import transaction

QUANT_PREFIX_RE = re.compile(
            r"""^\s*
        (?P<magnitude>\d+(?:\.\d+)?)
        \s+
        (?P<unit>[A-Za-z/%]{1,10})
        \s+
        (?P<base>\S.*)$
    """,
      re.VERBOSE,
)

RELA = "quantified_form_of"
TTYS = ("SCD", "SBD")


@transaction.atomic
def link_quantified_forms (batch_size: int = 5000) -> dict:
      """
      Link quantified SCDs/SBDs to their unquantified base concept of the SAME
      tty via RxNormConceptRelation(rela='quantified_form_of').

      SCD: '10 ML ipilimumab 5 MG/ML Injection'  -> 'ipilimumab 5 MG/ML Injection'
      SBD: '10 ML ipilimumab 5 MG/ML Injection [Yervoy]'
           -> 'ipilimumab 5 MG/ML Injection [Yervoy]'
      """
      concepts = list(
            RxNormConcept.objects
            .filter(tty__in=TTYS)
            .values_list("pk", "name", "tty")
      )

      def norm (s: str) -> str:
            return re.sub(r"\s+", " ", s).strip().lower()

      # index per tty so an SBD never links to an SCD base and vice versa
      by_name: dict[tuple[str, str], int] = {
            (tty, norm(name)): pk for pk, name, tty in concepts
      }

      pairs: list[tuple[int, int]] = []
      unmatched: list[tuple[str, str]] = []

      for pk, name, tty in concepts:
            m = QUANT_PREFIX_RE.match(name)
            if not m:
                  continue
            base_id = by_name.get((tty, norm(m.group("base"))))
            if base_id is None or base_id == pk:
                  unmatched.append((tty, name))
                  continue
            pairs.append((pk, base_id))

      existing = set(
            RxNormConceptRelation.objects
            .filter(rela=RELA, source_id__in=[q for q, _ in pairs])
            .values_list("source_id", "target_id")
      )

      relations = [
            RxNormConceptRelation(source_id=q, target_id=b, rela=RELA)
            for q, b in pairs
            if (q, b) not in existing
      ]

      RxNormConceptRelation.objects.bulk_create(
            relations, batch_size=batch_size, ignore_conflicts=True
      )

      return {
            "concepts_total": len(concepts),
            "quantified_matched": len(pairs),
            "created": len(relations),
            "already_linked": len(pairs) - len(relations),
            "unmatched_prefixed": unmatched,
      }
