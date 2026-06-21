from typing import Literal
from dataclasses import dataclass
import requests
import re
import json
from functools import lru_cache

RXNORM_URL = "https://rxnav.nlm.nih.gov"


class RelatedLevel:
      CONCEPT = "concept"
      DRUG = "drug"
      PRODUCT = "product"


type RelatedLevelType = Literal["concept", "drug", "product"]
type ConceptStatus = Literal["Active", "Obsolete", "Quantified", "Remapped", "NotCurrent", "Unknown"]

_VOLUME_PREFIX = re.compile(r"^\d+(?:\.\d+)?\s+ML\s+", re.IGNORECASE)


def _strip_volume_prefix(name: str) -> str:
      return _VOLUME_PREFIX.sub("", name, count=1)


@dataclass(frozen=True, slots=True)
class RxConcept:
      """A single RxNorm concept: identifier, name, and term type.

      Frozen & slotted so instances are hashable -- they can be set members,
      dict keys, or cached return values. tty/name may be None when a source
      payload omits them.

      :var rxcui: RxCUI ID string
      :var name:  concept name
      :var tty:   term type
      """
      rxcui: str
      name: str | None = None
      tty: str | None = None

      @classmethod
      def from_props(cls, props: dict, default_tty: str | None = None) -> "RxConcept":
            """Build from a conceptProperties / minConcept / quantifiedConcept dict."""
            return cls(
                  rxcui=props.get("rxcui"),
                  name=props.get("name"),
                  tty=props.get("tty") or default_tty,
            )

      @property
      def is_quantified(self) -> bool:
            """True if the name carries a volume prefix (a quantified normal form)."""
            return bool(self.name) and _VOLUME_PREFIX.match(self.name) is not None

      @property
      def unquantified_name(self) -> str | None:
            """The name with any leading volume prefix removed."""
            return _strip_volume_prefix(self.name) if self.name else None

      def __str__(self) -> str:
            return f"{self.rxcui} [{self.tty}] {self.name}"


@dataclass(frozen=True, slots=True)
class VolumeGroupKey:
      """A handle that links volume variants (5 ML, 15 ML, ...) of one drug.

      kind == "scd"  -> base is the unquantified SCD that groups them.
      kind == "scdc" -> components is the set of SCDC (ingredient+strength) CUIs
                        the variants share, used when the base can't be resolved.
      Use .key for the hashable value to group/dict on.
      """

      kind: Literal["scd", "scdc"]
      base: RxConcept | None = None
      components: frozenset[str] = frozenset()

      @property
      def key(self) -> object:
            return self.base.rxcui if self.kind == "scd" else self.components

      def __str__(self) -> str:
            if self.kind == "scd":
                  return f"scd:{self.base}"
            return f"scdc:{sorted(self.components)}"


@lru_cache(maxsize=128)
def get_rxcui_code(ndc, relation: RelatedLevelType = RelatedLevel.CONCEPT) -> list[tuple[str, str]]:
      url = RXNORM_URL + f"/REST/relatedndc.json?ndc={ndc}&relation={relation}"
      response = requests.get(url, timeout=10)

      if response.status_code == 200:
            data_root = response.json().get("ndcInfoList")
            if not data_root or not isinstance(data_root, dict):
                  return []

            data = data_root.get("ndcInfo")
            if not data or not isinstance(data, list) or len(data) == 0:
                  return []

            seen = set()
            results = []
            for item in data:
                  cui = item.get("rxcui")
                  tty = item.get("tty")
                  if cui and (cui, tty) not in seen:
                        results.append((cui, tty))
                        seen.add((cui, tty))
            return results
      elif response.status_code == 404:
            return []
      else:
            raise Exception(f"Failed to fetch related NDCs: {response.status_code} - {response.text}")


@lru_cache(maxsize=128)
def get_rxcui_name(rxcui_id: str) -> str | None:
      url = RXNORM_URL + f"/REST/rxcui/{rxcui_id}.json"
      response = requests.get(url, timeout=10)

      if response.status_code == 200:
            return response.json()["idGroup"].get("name")
      else:
            raise Exception(f"Failed to fetch RxCUI: {response.status_code} - {response.text}")
      return None


def get_all_rxcui(ndc: str) -> dict[str, list[tuple[str, str]]]:
      if not ndc:
            return {}
      output = dict()
      for relation in ["concept", "drug", "product"]:
            items = get_rxcui_code(ndc, relation)
            if items:
                  output[relation] = items
      return output


# ---------------------------------------------------------------------------
# Relationship / status traversal
#
# Grouping volume variants (e.g. 5 ML vs 15 ML of the same drug) is the job of
# the *unquantified* SCD. That base concept carries SUPPRESS="E" ("Quantified"
# status: non-dispensable for lack of a quantity factor). Every active-scoped
# endpoint -- /related (by rela or tty), /allrelated -- only emits SUPPRESS="N"
# atoms, so they silently drop the base. The reciprocal edges are:
#
#     base  --has_quantified_form-->  child   (5 ML, 15 ML, ...)
#     child --quantified_form_of--->  base
#
# historystatus (current+historical scope) is the only API surface that sees
# the base, and it only walks base -> children. There is therefore NO clean
# active-API path from an active quantified *child* up to its suppressed base;
# RXNREL.RRF is authoritative for that direction.
# ---------------------------------------------------------------------------

def _parse_related_group(payload: dict) -> list[RxConcept]:
      """Flatten a relatedGroup payload into RxConcept objects.

      Handles the empty shape RxNav returns when nothing matches, i.e.
      {"relatedGroup": {"rxcui": null}} -> [].
      """
      group = payload.get("relatedGroup") or {}
      results: list[RxConcept] = []
      for cg in group.get("conceptGroup") or []:
            tty = cg.get("tty")
            for cp in cg.get("conceptProperties") or []:
                  results.append(RxConcept.from_props(cp, default_tty=tty))
      return results


@lru_cache(maxsize=128)
def get_related_by_rela(rxcui_id: str, rela: str) -> tuple[RxConcept, ...]:
      """Concepts linked to rxcui_id by an explicit RxNorm relationship (rela).

      Active-scoped (SUPPRESS="N" only). Asking an active quantified concept for
      rela="quantified_form_of" returns () even though the edge exists, because
      the base SCD is SUPPRESS="E". See get_unquantified_form.
      """
      url = RXNORM_URL + f"/REST/rxcui/{rxcui_id}/related.json?rela={rela}"
      response = requests.get(url, timeout=10)
      if response.status_code == 200:
            return tuple(_parse_related_group(response.json()))
      elif response.status_code == 404:
            return ()
      else:
            raise Exception(f"Failed to fetch related-by-rela: {response.status_code} - {response.text}")


@lru_cache(maxsize=128)
def get_related_by_type(rxcui_id: str, tty: str) -> tuple[RxConcept, ...]:
      """Concepts of a given term type (tty) related to rxcui_id (active-scoped)."""
      url = RXNORM_URL + f"/REST/rxcui/{rxcui_id}/related.json?tty={tty}"
      response = requests.get(url, timeout=10)
      if response.status_code == 200:
            return tuple(_parse_related_group(response.json()))
      elif response.status_code == 404:
            return ()
      else:
            raise Exception(f"Failed to fetch related-by-type: {response.status_code} - {response.text}")


@lru_cache(maxsize=128)
def get_history_status(rxcui_id: str) -> dict:
      """Raw rxcuiStatus block: status, minConcept, quantifiedConcept, etc.

      Unlike /related this is current+historical scope, so it can see SUPPRESS="E"
      concepts. status == "Quantified" means rxcui_id is itself the unquantified
      base, and its volume variants are listed under "quantifiedConcept".
      """
      url = RXNORM_URL + f"/REST/rxcui/{rxcui_id}/historystatus.json"
      response = requests.get(url, timeout=10)
      if response.status_code == 200:
            return response.json().get("rxcuiStatus") or {}
      elif response.status_code == 404:
            return {}
      else:
            raise Exception(f"Failed to fetch history status: {response.status_code} - {response.text}")


def get_concept_status(rxcui_id: str) -> ConceptStatus | None:
      """Active | Obsolete | Quantified | Remapped | NotCurrent | Unknown."""
      return get_history_status(rxcui_id).get("status")


def get_concept(rxcui_id: str) -> RxConcept | None:
      """Resolve an RxCUI to a fully-populated RxConcept (rxcui + name + tty).

      Uses historystatus.minConcept, which carries tty (the plain /rxcui endpoint
      does not). Falls back to the name-only /rxcui lookup if needed.
      """
      mc = get_history_status(rxcui_id).get("minConcept") or {}
      print(mc)
      if mc.get("rxcui"):
            return RxConcept.from_props(mc)
      name = get_rxcui_name(rxcui_id)
      return RxConcept(rxcui_id, name, None) if name else None


def get_quantified_forms(base_rxcui: str) -> list[RxConcept]:
      """From an unquantified base SCD, list its quantified (volume) variants.

      This is the has_quantified_form direction, which historystatus exposes.
      Returns [] if base_rxcui is not an unquantified base.
      """
      status = get_history_status(base_rxcui)
      return [RxConcept.from_props(c) for c in (status.get("quantifiedConcept") or [])]


@lru_cache(maxsize=128)
def get_rxcui_by_name(name: str, allsrc: bool = True, search: int = 2) -> str | None:
      """Resolve a concept name to an RxCUI.

      allsrc=True (allsrc=1) widens the lookup beyond active RxNorm atoms, which
      is needed to have any chance of hitting a SUPPRESS="E" base by name. Still
      best-effort: name lookups are not guaranteed to surface suppressed concepts.
      """
      q = requests.utils.quote(name)
      url = RXNORM_URL + f"/REST/rxcui.json?name={q}&allsrc={int(allsrc)}&search={search}"
      response = requests.get(url, timeout=10)
      if response.status_code == 404:
            return None
      if response.status_code != 200:
            raise Exception(f"Failed to resolve name: {response.status_code} - {response.text}")
      ids = (response.json().get("idGroup") or {}).get("rxnormId") or []
      return ids[0] if ids else None


def get_unquantified_form(rxcui_id: str) -> RxConcept | None:
      """The unquantified base SCD that groups every volume variant of a quantified
      SCD, or None if it can't be resolved via the API.

      Strategy, in order:
        1. quantified_form_of directly -- only succeeds if NLM ever unsuppresses
           the base (normally returns nothing; documented above).
        2. Name fallback -- strip the leading "<n> ML " volume prefix and resolve
           the unquantified name with allsrc=1.

      If both miss, the base is reachable only via RXNREL.RRF
      (RELA in {quantified_form_of, has_quantified_form}); for an active grouping
      handle without the base, use get_scdc_group_key.
      """
      for concept in get_related_by_rela(rxcui_id, "quantified_form_of"):
            if concept.tty == "SCD":
                  return concept

      name = get_rxcui_name(rxcui_id)
      if not name:
            return None
      base_name = _strip_volume_prefix(name)
      if base_name == name:
            return None  # no volume prefix => already unquantified, or not a quantified SCD
      cui = get_rxcui_by_name(base_name, allsrc=True)
      return RxConcept(cui, base_name, "SCD") if cui else None


def get_scdc_group_key(rxcui_id: str) -> frozenset[str]:
      """Active-concept key for 'same ingredients at the same strengths'.

      Every volume variant reduces to the same set of SCDC (ingredient+strength)
      component CUIs, so this frozenset links the 5 ML and 15 ML products without
      ever touching the SUPPRESS="E" base. Caveat: it encodes ingredient+strength
      but NOT dose form, so it will not separate, e.g., an Injection from an oral
      solution at identical strengths.
      """
      return frozenset(c.rxcui for c in get_related_by_type(rxcui_id, "SCDC") if c.rxcui)


def get_volume_group_key(rxcui_id: str) -> VolumeGroupKey:
      """Best available handle that links volume variants of the same drug.

      Returns a VolumeGroupKey of kind "scd" when the unquantified SCD resolves,
      otherwise kind "scdc" with the shared component CUIs.
      """
      base = get_unquantified_form(rxcui_id)
      if base:
            return VolumeGroupKey(kind="scd", base=base)
      return VolumeGroupKey(kind="scdc", components=get_scdc_group_key(rxcui_id))


@lru_cache(maxsize=256)
def get_rxnorm_enrichment(ndc: str) -> dict:
      """Collect all relevant RxNorm data for one NDC into a serializable dict.

      Orchestrates get_all_rxcui → get_concept → get_concept_status →
      get_scdc_group_key → get_volume_group_key, all individually cached, so
      repeated calls for the same NDC are free after the first fetch.

      Returns a dict with:
          rxcui        -- raw get_all_rxcui output (concept/drug/product maps)
          concept      -- str(RxConcept) for the concept-level CUI, or None
          concept_rxcui-- the concept-level RxCUI string, or None
          status       -- Active | Obsolete | Quantified | Remapped | NotCurrent | Unknown
          scdc_group   -- sorted list of SCDC CUIs (ingredient+strength identity)
          volume_group_key -- str(VolumeGroupKey) when the drug is a quantified form

      scdc_group is a list (not frozenset) so the result is JSON-serializable.
      Callers that need the frozenset for set operations should use
      frozenset(result["scdc_group"]).
      """
      if not ndc:
            return {}

      rxcui_map = get_all_rxcui(ndc)
      concept_items = rxcui_map.get("concept") or []

      # Select the best concept CUI: prefer SCSD, then SCD, then first available
      concept_rxcui: str | None = None
      if concept_items:
            best_items = [c[0] for c in concept_items if c[1] == "SCSD"]
            if not best_items:
                  best_items = [c[0] for c in concept_items if c[1] == "SCD"]
            concept_rxcui = best_items[0] if best_items else concept_items[0][0]

      result: dict = {
            "rxcui": rxcui_map,
            "concept": None,
            "concept_rxcui": concept_rxcui,
            "status": None,
            "scdc_group": [],
            "volume_group_key": None,
      }

      if not concept_rxcui:
            return result

      try:
            concept = get_concept(concept_rxcui)
      except Exception:
            return result

      if not concept:
            return result

      result["concept"] = str(concept)

      try:
            result["status"] = get_concept_status(concept_rxcui)
      except Exception:
            pass

      try:
            scdc = get_scdc_group_key(concept_rxcui)
            result["scdc_group"] = sorted(scdc)
      except Exception:
            pass

      if concept.is_quantified:
            try:
                  vgk = get_volume_group_key(concept_rxcui)
                  result["volume_group_key"] = str(vgk)
            except Exception:
                  pass

      return result


if __name__ == "__main__":
      print(get_all_rxcui("0641-0497"))
      cui_codes = get_rxcui_code("0641-0497")
      cui_id = cui_codes[0][0] if cui_codes else None
      print(cui_id)
      print(get_rxcui_name(cui_id))

      # sodium_phos_ndc = "0409-7391"  # 15 ML -> 1872384
      sodium_phos_ndc = "0517-7305"
      cui_id = get_all_rxcui(sodium_phos_ndc)["concept"][0][0]

      rxconcept = get_concept(cui_id)
      print("concept:        ", rxconcept)
      print("is_quantified:  ", rxconcept.is_quantified if rxconcept else None)
      print("status:         ", get_concept_status(cui_id))
      print("scdc group key: ", get_scdc_group_key(cui_id))

      group = get_volume_group_key(cui_id)
      print("volume group:   ", group)
      print("group key:      ", group.key)

      # If the base resolves, walk back out to every volume variant:
      if group.kind == "scd":
            for variant in get_quantified_forms(group.base.rxcui):
                  print("  variant:      ", variant)
