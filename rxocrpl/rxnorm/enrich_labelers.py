"""Enrich Labelers.json using a local product-level NDC file.

Run:

    python enrich_labelers.py

Required files in the same directory as this script:

    Labelers.json
    rawndc.txt

Outputs:

    Labelers.json
        Original full list, enriched in place.

    Labelers.active.json
        New filtered list containing only labelers with at least one active
        matching NDC product.

rawndc.txt must be a tab-delimited product-level NDC file containing:

    PRODUCTNDC
    PRODUCTTYPENAME
    STARTMARKETINGDATE
    ENDMARKETINGDATE
    NDC_EXCLUDE_FLAG
    LISTING_RECORD_CERTIFIED_THROUGH

For each labeler, the script counts unique active human prescription and OTC
PRODUCTNDC values whose first NDC segment matches one of the labeler's codes.

Fields written to each labeler record:

    "active_ndc_product_count": <int>
    "has_active_ndc_products": <bool>

Legacy compatibility fields are also written:

    "active_rx_product_count": <int>
    "in_rxnorm": <bool>

The legacy "in_rxnorm" field does not indicate actual RxNorm membership.
It only indicates that matching active NDC Directory products were found.

A product is included when:

    - PRODUCTTYPENAME is HUMAN PRESCRIPTION DRUG or HUMAN OTC DRUG
    - NDC_EXCLUDE_FLAG is not Y
    - STARTMARKETINGDATE is blank or on/before today
    - ENDMARKETINGDATE is blank or on/after today
    - LISTING_RECORD_CERTIFIED_THROUGH is blank or on/after today

The script automatically detects UTF-8, Windows-1252, or Latin-1 encoding.

Both JSON files are replaced atomically only after successful processing.
"""

from __future__ import annotations

import codecs
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, TextIO

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent

LABELERS_PATH = SCRIPT_DIR / "Labelers.json"
ACTIVE_LABELERS_PATH = SCRIPT_DIR / "Labelers.active.json"
RAW_NDC_PATH = SCRIPT_DIR / "rawndc.txt"

ALLOWED_PRODUCT_TYPES = {
      "HUMAN PRESCRIPTION DRUG",
      "HUMAN OTC DRUG",
}

REQUIRED_NDC_COLUMNS = {
      "PRODUCTNDC",
      "PRODUCTTYPENAME",
      "STARTMARKETINGDATE",
      "ENDMARKETINGDATE",
      "NDC_EXCLUDE_FLAG",
      "LISTING_RECORD_CERTIFIED_THROUGH",
}

SUPPORTED_ENCODINGS = (
      "utf-8-sig",
      "cp1252",
      "latin-1",
)

ENCODING_TEST_CHUNK_SIZE = 1024 * 1024


# ---------------------------------------------------------------------------
# Text encoding
# ---------------------------------------------------------------------------

def can_decode_file(
          path: Path,
          encoding: str,
) -> bool:
      """Test whether an entire file can be decoded with an encoding."""

      decoder_factory = codecs.getincrementaldecoder(encoding)
      decoder = decoder_factory(errors="strict")

      try:
            with path.open("rb") as file_handle:
                  while chunk := file_handle.read(ENCODING_TEST_CHUNK_SIZE):
                        decoder.decode(chunk, final=False)

                  decoder.decode(b"", final=True)

      except UnicodeDecodeError:
            return False

      return True


def detect_text_encoding(path: Path) -> str:
      """Return the first supported encoding that decodes the entire file."""

      for encoding in SUPPORTED_ENCODINGS:
            if can_decode_file(path, encoding):
                  return encoding

      raise UnicodeError(
            f"could not decode {path.name} using any supported encoding: "
            f"{', '.join(SUPPORTED_ENCODINGS)}"
      )


def open_detected_text_file(
          path: Path,
) -> tuple[TextIO, str]:
      """Open a text file using its detected encoding."""

      encoding = detect_text_encoding(path)

      file_handle = path.open(
            "r",
            encoding=encoding,
            newline="",
      )

      return file_handle, encoding


# ---------------------------------------------------------------------------
# Date handling
# ---------------------------------------------------------------------------

def parse_ndc_date(
          value: str | None,
          *,
          field_name: str,
          line_number: int,
) -> date | None:
      """Parse a YYYYMMDD date from the NDC file."""

      if value is None:
            return None

      value = value.strip()

      if not value:
            return None

      try:
            return datetime.strptime(value, "%Y%m%d").date()

      except ValueError as exc:
            raise ValueError(
                  f"{RAW_NDC_PATH.name} line {line_number}: invalid "
                  f"{field_name} value {value!r}; expected YYYYMMDD"
            ) from exc


# ---------------------------------------------------------------------------
# Labeler and NDC normalization
# ---------------------------------------------------------------------------

def normalize_labeler_code(value: Any) -> str:
      """Normalize a labeler code while preserving leading zeroes."""

      if not isinstance(value, str):
            raise ValueError(
                  f"labeler code must be stored as a JSON string, got {value!r}"
            )

      code = value.strip()

      if "-" in code:
            code = code.split("-", 1)[0].strip()

      if not code:
            raise ValueError("labeler code cannot be empty")

      if not code.isdigit():
            raise ValueError(
                  f"labeler code must contain only digits, got {value!r}"
            )

      return code


def canonical_labeler_code(value: Any) -> str:
      """Return a zero-padding-independent labeler-code key."""

      normalized = normalize_labeler_code(value)
      stripped = normalized.lstrip("0")

      return stripped or "0"


def parse_product_ndc(
          value: str | None,
          *,
          line_number: int,
) -> tuple[str, str]:
      """Return the canonical labeler key and normalized PRODUCTNDC."""

      if value is None:
            raise ValueError(
                  f"{RAW_NDC_PATH.name} line {line_number}: missing PRODUCTNDC"
            )

      product_ndc = value.strip()

      if not product_ndc:
            raise ValueError(
                  f"{RAW_NDC_PATH.name} line {line_number}: empty PRODUCTNDC"
            )

      parts = product_ndc.split("-")

      if len(parts) < 2:
            raise ValueError(
                  f"{RAW_NDC_PATH.name} line {line_number}: invalid "
                  f"PRODUCTNDC {product_ndc!r}"
            )

      labeler_segment = parts[0].strip()

      if not labeler_segment.isdigit():
            raise ValueError(
                  f"{RAW_NDC_PATH.name} line {line_number}: invalid "
                  f"PRODUCTNDC labeler segment {labeler_segment!r}"
            )

      canonical_labeler = canonical_labeler_code(labeler_segment)

      return canonical_labeler, product_ndc


# ---------------------------------------------------------------------------
# Product filtering
# ---------------------------------------------------------------------------

def is_relevant_product(
          row: dict[str, str],
          *,
          today: date,
          line_number: int,
) -> bool:
      """Return whether an NDC product should count as active and relevant."""

      product_type = (
                row.get("PRODUCTTYPENAME") or ""
      ).strip().upper()

      if product_type not in ALLOWED_PRODUCT_TYPES:
            return False

      exclude_flag = (
                row.get("NDC_EXCLUDE_FLAG") or ""
      ).strip().upper()

      if exclude_flag == "Y":
            return False

      start_date = parse_ndc_date(
            row.get("STARTMARKETINGDATE"),
            field_name="STARTMARKETINGDATE",
            line_number=line_number,
      )

      end_date = parse_ndc_date(
            row.get("ENDMARKETINGDATE"),
            field_name="ENDMARKETINGDATE",
            line_number=line_number,
      )

      certified_through = parse_ndc_date(
            row.get("LISTING_RECORD_CERTIFIED_THROUGH"),
            field_name="LISTING_RECORD_CERTIFIED_THROUGH",
            line_number=line_number,
      )

      if start_date is not None and start_date > today:
            return False

      if end_date is not None and end_date < today:
            return False

      if certified_through is not None and certified_through < today:
            return False

      return True


# ---------------------------------------------------------------------------
# rawndc.txt indexing
# ---------------------------------------------------------------------------

def build_active_product_index(
          path: Path,
) -> tuple[dict[str, set[str]], int, int, str]:
      """Build an index of active PRODUCTNDC values by labeler code."""

      if not path.exists():
            raise FileNotFoundError(
                  f"{path.name} was not found next to this script "
                  f"(looked in {SCRIPT_DIR})"
            )

      products_by_labeler: dict[str, set[str]] = defaultdict(set)

      total_rows = 0
      included_rows = 0
      today = date.today()

      try:
            file_handle, detected_encoding = open_detected_text_file(path)

      except OSError as exc:
            raise OSError(
                  f"could not open {path.name}: {exc}"
            ) from exc

      with file_handle:
            reader = csv.DictReader(
                  file_handle,
                  delimiter="\t",
            )

            if reader.fieldnames is None:
                  raise ValueError(
                        f"{path.name} does not contain a header row"
                  )

            normalized_fieldnames = [
                  field.strip() if field is not None else field
                  for field in reader.fieldnames
            ]

            reader.fieldnames = normalized_fieldnames

            available_columns = {
                  field
                  for field in normalized_fieldnames
                  if field
            }

            missing_columns = REQUIRED_NDC_COLUMNS - available_columns

            if missing_columns:
                  missing = ", ".join(sorted(missing_columns))

                  raise ValueError(
                        f"{path.name} is missing required columns: {missing}"
                  )

            for line_number, row in enumerate(reader, start=2):
                  total_rows += 1

                  if not is_relevant_product(
                            row,
                            today=today,
                            line_number=line_number,
                  ):
                        continue

                  canonical_labeler, product_ndc = parse_product_ndc(
                        row.get("PRODUCTNDC"),
                        line_number=line_number,
                  )

                  products_by_labeler[canonical_labeler].add(product_ndc)
                  included_rows += 1

      return (
            dict(products_by_labeler),
            total_rows,
            included_rows,
            detected_encoding,
      )


# ---------------------------------------------------------------------------
# Labelers.json handling
# ---------------------------------------------------------------------------

def load_labeler_records() -> list[dict[str, Any]]:
      if not LABELERS_PATH.exists():
            raise FileNotFoundError(
                  f"{LABELERS_PATH.name} was not found next to this script "
                  f"(looked in {SCRIPT_DIR})"
            )

      try:
            contents = LABELERS_PATH.read_text(
                  encoding="utf-8",
            )

      except OSError as exc:
            raise OSError(
                  f"could not read {LABELERS_PATH.name}: {exc}"
            ) from exc

      try:
            records = json.loads(contents)

      except json.JSONDecodeError as exc:
            raise ValueError(
                  f"could not parse {LABELERS_PATH.name}: {exc}"
            ) from exc

      if not isinstance(records, list):
            raise ValueError(
                  f"expected a top-level JSON array, got "
                  f"{type(records).__name__}"
            )

      return records


def parse_labeler_record(
          record: Any,
          index: int,
) -> tuple[str, dict[str, Any], set[str]]:
      """Validate one Labelers.json record."""

      if not isinstance(record, dict):
            raise ValueError(
                  f"Labelers.json record {index} must be an object"
            )

      if len(record) != 1:
            raise ValueError(
                  f"Labelers.json record {index} must contain exactly "
                  f"one labeler key"
            )

      name = next(iter(record))
      inner = record[name]

      if not isinstance(name, str):
            raise ValueError(
                  f"Labelers.json record {index} has a non-string labeler name"
            )

      if not isinstance(inner, dict):
            raise ValueError(
                  f"Labelers.json record {index} ({name!r}) must contain "
                  f"an object"
            )

      raw_codes = inner.get("codes", [])

      if raw_codes is None:
            raw_codes = []

      if not isinstance(raw_codes, list):
            raise ValueError(
                  f"Labelers.json record {index} ({name!r}) has a "
                  f"non-array 'codes' value"
            )

      canonical_codes: set[str] = set()

      for raw_code in raw_codes:
            try:
                  canonical_codes.add(
                        canonical_labeler_code(raw_code)
                  )

            except ValueError as exc:
                  raise ValueError(
                        f"Labelers.json record {index} ({name!r}): {exc}"
                  ) from exc

      return name, inner, canonical_codes


def enrich_labelers(
          records: list[dict[str, Any]],
          products_by_labeler: dict[str, set[str]],
) -> tuple[int, int, list[dict[str, Any]]]:
      """Enrich records and return only records that have active products."""

      kept = 0
      dropped = 0
      active_records: list[dict[str, Any]] = []

      print(
            f"{'#':>5}  "
            f"{'labeler':<28} "
            f"{'codes':>5} "
            f"{'products':>8}  "
            f"flag"
      )
      print("-" * 66)

      for index, record in enumerate(records, start=1):
            name, inner, canonical_codes = parse_labeler_record(
                  record,
                  index,
            )

            matching_products: set[str] = set()

            for code in canonical_codes:
                  matching_products.update(
                        products_by_labeler.get(code, set())
                  )

            product_count = len(matching_products)
            has_products = product_count > 0

            inner["active_ndc_product_count"] = product_count
            inner["has_active_ndc_products"] = has_products

            # Legacy compatibility fields.
            inner["active_rx_product_count"] = product_count
            inner["in_rxnorm"] = has_products

            if has_products:
                  kept += 1
                  flag = "KEEP"
                  active_records.append(record)
            else:
                  dropped += 1
                  flag = "drop"

            print(
                  f"{index:>5}  "
                  f"{name[:28]:<28} "
                  f"{len(canonical_codes):>5} "
                  f"{product_count:>8}  "
                  f"{flag}"
            )

      return kept, dropped, active_records


def write_json_atomically(
          path: Path,
          records: list[dict[str, Any]],
) -> None:
      """Write JSON through a temporary file, then replace the destination."""

      temporary_path = path.with_suffix(
            path.suffix + ".tmp"
      )

      serialized = json.dumps(
            records,
            indent=2,
            ensure_ascii=False,
      )

      try:
            temporary_path.write_text(
                  serialized + "\n",
                  encoding="utf-8",
            )

            os.replace(
                  temporary_path,
                  path,
            )

      except OSError:
            try:
                  temporary_path.unlink(missing_ok=True)
            except OSError:
                  pass

            raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
      """Orchestrates data indexing, enrichment, and atomic file persistence"""
      try:
            records = load_labeler_records()
            (products_by_labeler, total_rows, included_rows, detected_encoding,) = build_active_product_index(
                  RAW_NDC_PATH)

      except (FileNotFoundError, UnicodeError, ValueError, OSError,) as exc:
            print(f"ERROR: {exc}", file=sys.stderr, )
            return 1

      unique_products = sum(
            len(products)
            for products in products_by_labeler.values()
      )

      print(f"Loaded {RAW_NDC_PATH.name}")
      print(f"Detected encoding:       {detected_encoding}")
      print(f"Product rows read:       {total_rows:,}")
      print(f"Relevant rows included:  {included_rows:,}")
      print(f"Unique active products:  {unique_products:,}")
      print(f"Indexed labeler codes:   {len(products_by_labeler):,}")
      print()

      print(
            f"Enriching {len(records):,} labelers from "
            f"{LABELERS_PATH.name}\n"
      )

      try:
            kept, dropped, active_records = enrich_labelers(
                  records,
                  products_by_labeler,
            )

      except ValueError as exc:
            print(
                  f"\nERROR: {exc}",
                  file=sys.stderr,
            )
            print(
                  "No output files were modified.",
                  file=sys.stderr,
            )
            return 1

      try:
            write_json_atomically(
                  LABELERS_PATH,
                  records,
            )

            write_json_atomically(
                  ACTIVE_LABELERS_PATH,
                  active_records,
            )

      except OSError as exc:
            print(
                  f"\nERROR: could not write output files: {exc}",
                  file=sys.stderr,
            )
            return 1

      print("-" * 66)
      print(
            f"Done. {kept:,} kept, {dropped:,} dropped."
      )
      print(f"Wrote full enriched file: {LABELERS_PATH}")
      print(f"Wrote filtered file:      {ACTIVE_LABELERS_PATH}")

      return 0


if __name__ == "__main__":
      sys.exit(main())
