"""Thin, dependency-free client for the RxNav / RxNorm REST API.

Only the three endpoints the graph pipeline needs are implemented:

    * ``properties``        -> getRxcuiProperties   (anchor's own tty/name)
    * ``all_related``       -> getAllRelatedInfo    (whole concept family, by TTY)
    * ``related_by_rela``   -> getRelatedByRelationship (one edge type at a time)

The client uses ``urllib`` so it runs with no third-party packages. If you
already have an ``httpx``/``requests`` session in ``rxocrpl``, pass a callable
as ``fetch_json`` and this module will use it instead -- the only contract is
``fetch_json(url: str) -> dict``.

Reference:
    https://lhncbc.nlm.nih.gov/RxNav/APIs/RxNormAPIs.html
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from rxocrpl.rxnorm.client import fetch_rxcui_by_name, _get

BASE_URL = "https://rxnav.nlm.nih.gov/REST"

# RxNav asks callers to stay under ~20 requests/second. We are intentionally
# more conservative because the pipeline can issue many small edge probes.
DEFAULT_MIN_INTERVAL = 0.06  # seconds between requests (~16 req/s)
DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 3


class RxNavError(RuntimeError):
      """Raised when RxNav cannot be reached or returns a non-JSON body."""


def _default_fetch_json(
          url: str,
          *,
          timeout: float,
          retries: int,
          user_agent: str,
) -> dict:
      last_exc: Optional[Exception] = None
      for attempt in range(retries):
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            try:
                  with urllib.request.urlopen(req, timeout=timeout) as resp:
                        raw = resp.read().decode("utf-8")
                  return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:  # noqa: PERF203
                  # 429 / 5xx are worth retrying; 4xx (e.g. bad rxcui) are not.
                  if exc.code in (429, 500, 502, 503, 504):
                        last_exc = exc
                        time.sleep(0.5 * (attempt + 1))
                        continue
                  raise RxNavError(f"RxNav HTTP {exc.code} for {url}") from exc
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                  last_exc = exc
                  time.sleep(0.5 * (attempt + 1))
      raise RxNavError(f"RxNav request failed after {retries} attempts: {url}") from last_exc


class RxNavClient:
      """Minimal RxNorm REST client with polite rate limiting."""

      def __init__(
                self,
                *,
                base_url: str = BASE_URL,
                fetch_json: Optional[Callable[[str], dict]] = None,
                min_interval: float = DEFAULT_MIN_INTERVAL,
                timeout: float = DEFAULT_TIMEOUT,
                retries: int = DEFAULT_RETRIES,
                user_agent: str = "rxgraph/1.0 (+pharmacy-informatics)",
      ) -> None:
            self.base_url = base_url.rstrip("/")
            self.min_interval = min_interval
            self.timeout = timeout
            self.retries = retries
            self.user_agent = user_agent
            self._injected_fetch = fetch_json
            self._last_call = 0.0

      # -- low level ---------------------------------------------------------
      def _get(self, path: str, **params: str) -> dict:
            self._throttle()
            if self._injected_fetch is not None:
                  url = f"{self.base_url}/{path.lstrip('/')}"
                  if params:
                        url = f"{url}?{urllib.parse.urlencode(params)}"
                  return self._injected_fetch(url)
            return _get(path, **params) or {}

      def _throttle(self) -> None:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self.min_interval:
                  time.sleep(self.min_interval - elapsed)
            self._last_call = time.monotonic()

      # -- endpoints ---------------------------------------------------------
      def find_rxcui_by_name(self, name: str, allsrc: bool = True, search: int = 2) -> str | None:
            """Resolve a concept ``name`` to an RxCUI using ``/rxcui.json``.

            ``allsrc=True`` (allsrc=1) widens the search beyond active RxNorm atoms.
            ``search=2`` (default) uses the RxNav "normalized" search strategy.
            """
            return fetch_rxcui_by_name(name, allsrc=allsrc, search=search)

      def properties(self, rxcui: str) -> dict:
            """Return the concept's own properties dict (``name``, ``tty``, ...).

            Empty dict if the rxcui has no RxNorm normalized name.
            """
            data = self._get(f"rxcui/{rxcui}/properties.json")
            return data.get("properties", {}) or {}

      def all_related(self, rxcui: str) -> list[dict]:
            """Return ``conceptGroup`` list from getAllRelatedInfo.

            Each item is ``{"tty": <str>, "conceptProperties": [ {...}, ... ]}``.
            Groups for TTYs with no members are present but lack
            ``conceptProperties``; callers should ``.get`` defensively.
            """
            data = self._get(f"rxcui/{rxcui}/allrelated.json")
            group = (data.get("allRelatedGroup") or {}).get("conceptGroup") or []
            return group

      def related_by_rela(self, rxcui: str, rela: str) -> list[dict]:
            """Return concepts reachable from ``rxcui`` through a single ``rela``.

            Querying one rela at a time means every concept in the result is known
            to be related by exactly that rela -- the multi-rela form collapses the
            groups and loses that attribution.
            """
            data = self._get(f"rxcui/{rxcui}/related.json", rela=rela)
            group = (data.get("relatedGroup") or {}).get("conceptGroup") or []
            concepts: list[dict] = []
            for entry in group:
                  concepts.extend(entry.get("conceptProperties") or [])
            return concepts
