"""Test the OCR field extractor against realistic label text.

Run from the bundle root:
    python -m pharmacy_pipeline.tests.test_extractor
"""
from __future__ import annotations

import pathlib
import sys

from django.test import TestCase

from rxocrpl.ocr.ocr_fields import extract_fields

HERE = pathlib.Path(__file__).resolve()
ROOT = HERE.parent.parent
if str(ROOT.parent) not in sys.path:
      sys.path.insert(0, str(ROOT.parent))


def show (name, lines):
      # Directory disabled — this test exercises regex/heuristics only.
      fields, audit = extract_fields(lines, auto_load_directory=False)
      print(f"--- {name} ---")
      for line in lines:
            print(f"  raw: {line!r}")
      # Print only the populated fields to keep output readable.
      populated = {k: getattr(fields, k) for k in fields._FIELD_NAMES
                   if getattr(fields, k) is not None}
      confidences = {k: getattr(fields, f"{k}_confidence") for k in populated}
      print(f"  -> {populated}")
      print(f"  conf: {confidences}")
      flagged = [k for k in populated if confidences[k] < 0.7]
      if flagged:
            print(f"  needs review: {flagged}")
      print()


class ExtractorTests(TestCase):
      def test_examples (self):
            show("rx3 IV bag (NS 250mL Baxter, lot 2JD588, NDC 0264-7800-10)", [
                  "BAXTER",
                  "0.9% Sodium Chloride Injection USP",
                  "250 mL",
                  "NDC 0264-7800-10",
                  "LOT 2JD588",
                  "EXP 07/27",
                  "For intravenous use only",
                  "Rx only",
            ])

            show("rx3 vial (Sterile Water 20mL, lot HK3092)", [
                  "Sterile Water for Injection",
                  "20 mL",
                  "Multi-dose vial",
                  "Lot: HK3092",
                  "EXP 12/26",
            ])

            show("rx3 vial (lot 308777251000, no NDC visible)", [
                  "Lot 308777251000",
                  "EXP 05/30/2028",
            ])

            show("rx4 vial (Ceftazidime-Avibactam 2.5g Pfizer, NDC 0009-0224-20)", [
                  "AVYCAZ",
                  "ceftazidime and avibactam",
                  "2.5 g per vial",
                  "For intravenous use only",
                  "Single-dose vial",
                  "Discard unused portion",
                  "PFIZER",
                  "NDC 0009-0224-20",
            ])

            show("Concentration vs total strength (heparin 1000 units/mL)", [
                  "HEPARIN SODIUM",
                  "1000 units/mL",
                  "10 mL",
                  "Multiple-Dose Vial",
                  "NDC 0641-2440-45",
                  "LOT A12345",
                  "EXP MAR 2027",
            ])

            show("Messy OCR: O-for-0 in lot, ambiguous date", [
                  "LOT 3O8777251OOO",
                  "EXP 5/3O/2O28",
                  "STERILE WATER",
            ])

            show("Low-quality crop: only a date, no labels", [
                  "12/26",
            ])
