"""Test the OCR field extractor with the FDA NDC Directory CSV.

Run from the bundle root:
    python -m pharmacy_pipeline.tests.test_with_directory
"""
from __future__ import annotations
import sys, pathlib

HERE = pathlib.Path(__file__).resolve()
ROOT = HERE.parent.parent  # pharmacy_pipeline/
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from rxocrpl.ocr.ocr_fields import extract_fields
from rxocrpl.ocr.ndc_directory import NDCDirectory, set_directory

# Use the bundled sample CSV.
csv_path = ROOT / "db" / "ndcproduct.sample.csv"
directory = NDCDirectory(str(csv_path))
directory.load()
set_directory(directory)

print(f"Loaded {len(directory._by_ndc)} NDC entries (delisted excluded).\n")


def show(name, lines):
    fields, audit = extract_fields(lines)
    print(f"--- {name} ---")
    for line in lines:
        print(f"  raw: {line!r}")
    populated = {k: getattr(fields, k) for k in fields._FIELD_NAMES
                 if getattr(fields, k) is not None}
    confidences = {k: getattr(fields, f"{k}_confidence") for k in populated}
    print(f"  -> {populated}")
    print(f"  conf: {confidences}")
    flagged = [k for k in populated if confidences[k] < 0.7]
    if flagged:
        print(f"  needs review: {flagged}")
    print()


if __name__ == "__main__":
    show("rx3 IV bag (NS 250mL Baxter, lot 2JD588)", [
        "BAXTER",
        "0.9% Sodium Chloride Injection USP",
        "250 mL",
        "NDC 0264-7800-10",
        "LOT 2JD588",
        "EXP 07/27",
        "For intravenous use only",
        "Rx only",
    ])

    show("rx3 vial (Sterile Water 20mL, lot HK3092 — no NDC printed)", [
        "Sterile Water for Injection",
        "20 mL",
        "Multi-dose vial",
        "Lot: HK3092",
        "EXP 12/26",
    ])

    show("rx4 vial (Avycaz / Ceftazidime-Avibactam, NDC 0009-0224-20)", [
        "AVYCAZ",
        "ceftazidime and avibactam",
        "2.5 g per vial",
        "For intravenous use only",
        "Single-dose vial",
        "Discard unused portion",
        "PFIZER",
        "NDC 0009-0224-20",
    ])

    show("Heparin (concentration vs vol)", [
        "HEPARIN SODIUM",
        "1000 units/mL",
        "10 mL",
        "Multiple-Dose Vial",
        "NDC 0641-2440-45",
        "LOT A12345",
        "EXP MAR 2027",
    ])

    show("Delisted NDC should NOT enrich", [
        "OLDNAME",
        "NDC 9999-9999-01",
        "LOT XYZ123",
    ])

    show("Bare NDC, no other text — full enrichment from DB", [
        "NDC 0009-0224-20",
    ])

    show("Sanity: not in directory → only OCR-derived fields populate", [
        "ACME WIDGET CORP",
        "WIDGET 5g",
        "LOT W001",
        "EXP 01/2030",
    ])
