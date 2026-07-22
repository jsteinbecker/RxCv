from __future__ import annotations

import csv
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from django.db import transaction

from rxocrpl.dailymed import find_setid

if TYPE_CHECKING:
      from rxocrpl.models import Product

logger = logging.getLogger(__name__)

DEFAULT_MAP_PATH = Path(__file__).resolve().parent / "db" / "rxnorm-dailymed_setid_map.txt"

CONCEPT_TTY_PRIORITY_GENERIC = {
      "SCD": 0,
      "SBD": 1,
      "SCDG": 2,
      "SBDG": 3,
      "GPCK": 4,
      "BPCK": 5,
}
CONCEPT_TTY_PRIORITY_BRANDED = {
      "SBD": 0,
      "SCD": 1,
      "SBDG": 2,
      "SCDG": 3,
      "GPCK": 4,
      "BPCK": 5,
}


@dataclass(frozen=True, slots=True)
class SetidRxNormEntry:
      setid: str
      spl_version: int
      rxcui: str
      rxstring: str
      tty: str

      def to_mapping_defaults (self) -> dict[str, str]:
            return {
                  "rxcui": self.rxcui,
                  "tty": self.tty,
                  "name": self.rxstring,
            }


def _normalize_text (value: str | None) -> str:
      if not value:
            return ""
      value = value.casefold()
      value = re.sub(r"[\[\](),]", " ", value)
      value = re.sub(r"\s+", " ", value).strip()
      return value


def _product_is_branded (product: Product) -> bool:
      brand = _normalize_text(product.brand_name)
      generic = _normalize_text(product.generic_name)
      return bool(brand) and brand != generic


def _entry_priority (product: Product, entry: SetidRxNormEntry) -> tuple[int, int, str]:
      if _product_is_branded(product):
            tty_rank = CONCEPT_TTY_PRIORITY_BRANDED.get(entry.tty, 99)
      else:
            tty_rank = CONCEPT_TTY_PRIORITY_GENERIC.get(entry.tty, 99)

      product_texts = [
            _normalize_text(product.as_substance()),
            _normalize_text(product.describe()),
            _normalize_text(product.generic_name),
            _normalize_text(product.brand_name),
      ]
      rxstring = _normalize_text(entry.rxstring)

      if rxstring and any(rxstring == text for text in product_texts if text):
            text_rank = 0
      elif rxstring and any(text and (rxstring.startswith(text) or text in rxstring) for text in product_texts):
            text_rank = 1
      else:
            text_rank = 2

      return text_rank, tty_rank, -entry.spl_version, entry.rxcui


@lru_cache(maxsize=8)
def load_setid_map (map_path: str | Path = DEFAULT_MAP_PATH) -> dict[str, list[SetidRxNormEntry]]:
      """Parse the DailyMed/RxNorm setid map into per-setid candidate rows."""
      path = Path(map_path)
      deduped: dict[tuple[str, str, str], SetidRxNormEntry] = {}

      with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="|")
            for row in reader:
                  setid = (row.get("SETID") or "").strip()
                  rxcui = (row.get("RXCUI") or "").strip()
                  rxstring = (row.get("RXSTRING") or "").strip()
                  tty = (row.get("RXTTY") or "").strip()
                  if not setid or not rxcui or not rxstring or not tty:
                        continue

                  try:
                        spl_version = int((row.get("SPL_VERSION") or "0").strip() or 0)
                  except ValueError:
                        spl_version = 0

                  entry = SetidRxNormEntry(
                        setid=setid,
                        spl_version=spl_version,
                        rxcui=rxcui,
                        rxstring=rxstring,
                        tty=tty,
                  )
                  key = (entry.setid, entry.rxcui, entry.tty)
                  existing = deduped.get(key)
                  if existing is None or entry.spl_version > existing.spl_version:
                        deduped[key] = entry

      grouped: dict[str, list[SetidRxNormEntry]] = defaultdict(list)
      for entry in deduped.values():
            grouped[entry.setid].append(entry)

      for entries in grouped.values():
            entries.sort(key=lambda entry: (-entry.spl_version, entry.tty, entry.rxcui))

      return dict(grouped)


def select_best_entry (product: Product, entries: Iterable[SetidRxNormEntry]) -> SetidRxNormEntry | None:
      candidates = [entry for entry in entries if entry.tty in CONCEPT_TTY_PRIORITY_GENERIC]
      if not candidates:
            return None
      return min(candidates, key=lambda entry: _entry_priority(product, entry))


def link_product_from_setid_map (
          product: Product,
          setid_index: dict[str, list[SetidRxNormEntry]],
) -> SetidRxNormEntry | None:
      """Resolve a product's DailyMed setid to the best RxNorm concept mapping."""
      from rxocrpl.models import ProductRxNormMapping

      best = resolve_product_from_setid_map(product, setid_index)
      if best is None:
            return None

      ProductRxNormMapping.objects.update_or_create(
            product_ndc=product.product_ndc,
            defaults=best.to_mapping_defaults(),
      )

      return best


def resolve_product_from_setid_map (
          product: Product,
          setid_index: dict[str, list[SetidRxNormEntry]],
) -> SetidRxNormEntry | None:
      """Return the best setid-map row for *product* without writing to the DB."""
      setid = find_setid(product.product_ndc)
      if not setid:
            return None

      entries = setid_index.get(setid)
      if not entries:
            return None

      return select_best_entry(product, entries)


def link_products_from_setid_map (
          products: Iterable[Product],
          *,
          map_path: str | Path = DEFAULT_MAP_PATH,
) -> dict[str, int]:
      """Backfill ProductRxNormMapping rows using the DailyMed setid map."""
      setid_index = load_setid_map(map_path)
      linked = 0
      skipped = 0

      with transaction.atomic():
            for product in products:
                  entry = link_product_from_setid_map(product, setid_index)
                  if entry is None:
                        skipped += 1
                        continue
                  linked += 1

      logger.info(
            "Linked %s products from DailyMed setid map; skipped %s",
            linked,
            skipped,
      )
      return {"linked": linked, "skipped": skipped}
