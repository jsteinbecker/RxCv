"""RxNav HTTP client.

A thin, cacheable wrapper around the RxNav REST API.  All network I/O lives
here; the rest of the package never imports ``requests`` directly.

Caching
-------
Each public method is decorated with ``@lru_cache`` on the underlying free
function it delegates to, keeping cache semantics simple and avoiding the
``lru_cache``-on-method/``self``-leak footgun.  The shared ``requests.Session``
is module-level, so connection pooling is preserved across calls.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, Tuple
from urllib.parse import quote, urlencode

import requests

from rxocrpl.rxnorm.dataclasses import RxConcept, RxNavError

_BASE_URL = "https://rxnav.nlm.nih.gov/REST"
_TIMEOUT = 10  # seconds

_session = requests.Session()


def _get(path: str, **params) -> dict | None:
      """GET ``_BASE_URL + path`` with *params* as query-string arguments.

      Returns the parsed JSON dict on success.
      Returns ``None`` on 404 (resource not found — not an error).
      Raises :class:`~rxnorm.models.RxNavError` on any other non-200 status.
      """
      url = f"{_BASE_URL}/{path.lstrip('/')}"
      if params:
            url = f"{url}?{urlencode(params)}"

      response = _session.get(url, timeout=_TIMEOUT)

      if response.status_code == 200:
            return response.json()
      if response.status_code == 404:
            return None
      raise RxNavError(
            f"RxNav returned {response.status_code} for {url}: {response.text}"
      )


def _parse_related_group(payload: dict) -> tuple[RxConcept, ...]:
      """Flatten a ``relatedGroup`` payload into a tuple of :class:`RxConcept`.

      Handles the empty shape RxNav returns when nothing matches, i.e.
      ``{"relatedGroup": {"rxcui": null}}`` → ``()``.
      """
      group = payload.get("relatedGroup") or {}
      results: list[RxConcept] = []
      for cg in group.get("conceptGroup") or []:
            tty = cg.get("tty")
            for cp in cg.get("conceptProperties") or []:
                  results.append(RxConcept.from_props(cp, default_tty=tty))
      return tuple(results)


@lru_cache(maxsize=256)
def fetch_ndc_rxcui(ndc: str, relation: str) -> list[list[str]]:
      """Resolve *ndc* to (rxcui, tty) pairs at the given *relation* scope.

      Returns ``[]`` when the NDC is not found.
      """
      data = _get("relatedndc.json", ndc=ndc, relation=relation)
      if data is None:
            return []

      info_list = data.get("ndcInfoList") or {}
      items = info_list.get("ndcInfo") or []
      seen = set()
      results = []
      for item in items:
            cui = item.get("rxcui")
            tty = item.get("tty")
            if cui and (cui, tty) not in seen:
                  results.append([cui, tty])
                  seen.add((cui, tty))
      return results


@lru_cache(maxsize=256)
def fetch_ndcs_by_rxcui(rxcui: str) -> list[str]:
      """Return all NDCs associated with *rxcui*."""
      data = _get(f"rxcui/{rxcui}/allndcs.json")
      if not data:
            return []
      return (data.get("ndcGroup") or {}).get("ndcList", {}).get("ndc", [])


@lru_cache(maxsize=256)
def fetch_rxcui_name(rxcui: str) -> str | None:
      """Return the display name for *rxcui*, or ``None`` if not found."""
      data = _get(f"rxcui/{rxcui}.json")
      if data is None:
            return None
      return (data.get("idGroup") or {}).get("name")


@lru_cache(maxsize=256)
def fetch_history_status(rxcui: str) -> dict:
      """Return the raw ``rxcuiStatus`` block for *rxcui*.

      Unlike ``/related``, historystatus uses current+historical scope, so it
      can see ``SUPPRESS="E"`` concepts.  ``status == "Quantified"`` means
      *rxcui* is itself the unquantified base; its volume variants appear under
      ``"quantifiedConcept"``.

      Returns an empty dict when not found.
      """
      data = _get(f"rxcui/{rxcui}/historystatus.json")
      return (data or {}).get("rxcuiStatus") or {}


@lru_cache(maxsize=256)
def fetch_related_by_rela(rxcui: str, rela: str) -> tuple[RxConcept, ...]:
      """Concepts linked to *rxcui* by the explicit RxNorm relationship *rela*.

      Active-scoped (``SUPPRESS="N"`` only).  Querying a quantified concept
      with ``rela="quantified_form_of"`` normally returns ``()`` because the
      unquantified base carries ``SUPPRESS="E"``.  See
      :func:`~rxnorm.enrichment.get_unquantified_form` for the workaround.
      """
      data = _get(f"rxcui/{rxcui}/related.json", rela=rela)
      return _parse_related_group(data) if data else ()


@lru_cache(maxsize=256)
def fetch_related_by_tty(rxcui: str, tty: str) -> tuple[RxConcept, ...]:
      """Concepts of term type *tty* related to *rxcui* (active-scoped)."""
      data = _get(f"rxcui/{rxcui}/related.json", tty=tty)
      return _parse_related_group(data) if data else ()


@lru_cache(maxsize=256)
def fetch_rxcui_by_name(name: str, allsrc: bool = True, search: int = 2) -> str | None:
      """Resolve a concept *name* to an RxCUI, or ``None`` if unresolvable.

      ``allsrc=True`` (``allsrc=1``) widens the search beyond active RxNorm
      atoms, which is needed to reach ``SUPPRESS="E"`` bases by name.  This is
      best-effort: name look-ups are not guaranteed to surface suppressed
      concepts.
      """
      data = _get("rxcui.json", name=quote(name), allsrc=int(allsrc), search=search)
      ids = ((data or {}).get("idGroup") or {}).get("rxnormId") or []
      return ids[0] if ids else None


if __name__ == "__main__":
      print(fetch_rxcui_by_name("dalbavancin"))
      print(fetch_rxcui_by_name("ampicillin"))
      print(fetch_rxcui_by_name("hydromorphone"))
      print(fetch_rxcui_by_name("labetalol"))

      print(fetch_related_by_tty("6918", "pin"))
      print(fetch_related_by_tty("6918", "scd"))

      print(fetch_rxcui_name("221124"))
