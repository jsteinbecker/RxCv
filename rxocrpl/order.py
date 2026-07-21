from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Dict, Optional, cast

try:
      from .fda import lookup_ndc_package, lookup_generic_name, parse_product
      from .pipeline import Pipeline, PipelineResult
      from .detection.process_inference import (
            ProcessInferenceManager,
            ProcessInferenceResult,
      )
      from .quantities import (Quantity, PhysicalQuantity, DimensionalityError, mcg, mg, g, kg, mL, L, mmol, mol, mEq, units, percent, each)
      from .models import Product, Component, CspOrder as OrderModel
except ImportError:
      from rxocrpl.fda import lookup_ndc_package, lookup_generic_name
      from rxocrpl.pipeline import Pipeline, PipelineResult
      from rxocrpl.detection.process_inference import (
            ProcessInferenceManager,
            ProcessInferenceResult,
      )
      from rxocrpl.quantities import (
            Quantity,
            PhysicalQuantity,
            DimensionalityError,
            mcg,
            mg,
            g,
            kg,
            mL,
            L,
            mmol,
            mol,
            mEq,
            units,
            percent,
            each,
      )

      try:
            from rxocrpl.models import Product, Component, CspOrder as OrderModel
      except ImportError:
            raise ImportError(
                  "rxocrpl.models is not available; please ensure the models module is installed and accessible."
            )

# ---------------------------------------------------------------------------
# expected_components string parser
# ---------------------------------------------------------------------------

# Container / dosage-form tokens that appear in component strings but are not
# part of the drug name.
_CONTAINER_FORMS: frozenset[str] = frozenset(
      {
            "VIAL",
            "AMPULE",
            "AMPOULE",
            "SYRINGE",
            "IVPB",
            "IV",
            "BAG",
            "BOTTLE",
            "PFS",
            "KIT",
            "MDV",
            "SDV",
            "PIGGYBACK",
      }
)

# Common IV-fluid abbreviations mapped to their nonproprietary name as it
# appears in the FDA NDC Directory, for use in NDC lookups.
_DRUG_ABBREVIATIONS: dict[str, str] = {
      "NS": "sodium chloride",
      "NS IVPB": "sodium chloride",
      "0.9NS": "sodium chloride",
      "D5W": "dextrose",
      "D10W": "dextrose",
      "D5NS": "dextrose",
      "D5LR": "dextrose",
      "LR": "lactated ringer",
      "RL": "lactated ringer",
      "SW": "sterile water",
      "SWI": "sterile water",
}

_STRENGTH_RE = re.compile(
      r"(\d+(?:\.\d+)?)"
      r"\s*(g|mg|mcg|ug|units?|u|mEq|%|mL|ML|L)"
      r"(?:\s*/\s*(\d+(?:\.\d+)?)"
      r"\s*(g|mg|mcg|ug|units?|u|mEq|%|mL|ML|L))?",
      re.IGNORECASE,
)


def _parse_component_string (s: str) -> dict:
      """Parse a human-readable component string into structured fields.

      Format (comma-separated, order-agnostic):
          [FORM,] DRUG [STRENGTH[/VOLUME]] [, FORM], #QUANTITY

      Examples:
          "VIAL, FUROSEMIDE 100mg/10mL, #1"
          "NS IVPB 100ML, #1"
          "MORPHINE 1mg/mL, 250mL IVPB, #2"
          "D5W 250ML, #1"
      """
      parts = [p.strip() for p in s.split(",")]

      quantity = 1
      form: str | None = None
      drug_tokens: list[str] = []

      for part in parts:
            if re.match(r"#\d+$", part):
                  quantity = int(part[1:])
                  continue
            if part.upper() in _CONTAINER_FORMS:
                  form = part.upper()
                  continue
            drug_tokens.append(part)

      description = " ".join(drug_tokens).strip()

      # Extract strength / volume expression.
      strength: str | None = None
      drug_name = description
      m = _STRENGTH_RE.search(description)
      if m:
            strength = m.group(0)
            drug_name = (description[: m.start()] + description[m.end():]).strip()

      # Strip any embedded container-form tokens from the drug name.
      words = [w for w in drug_name.split() if w.upper() not in _CONTAINER_FORMS]
      drug_name = " ".join(words).strip()

      # Expand common abbreviations so the NDC directory lookup can find them.
      expanded = _DRUG_ABBREVIATIONS.get(drug_name.upper())
      if expanded:
            drug_name = expanded

      return {
            "form": form,
            "drug_name": drug_name or description,
            "strength": strength,
            "quantity": quantity,
            "raw": s,
      }


# ---------------------------------------------------------------------------
# Physical quantities
# ---------------------------------------------------------------------------
# The PhysicalQuantity / Dimension / DimensionalUnit / Measurement stubs that
# used to live here are now the dimensional system in ``quantities.py``:
#   * Quantity   -- magnitude + unit, dimension-aware arithmetic
#   * Dimension  -- exponent vector over base dims (MASS, VOLUME, SUBSTANCE,
#                   CHARGE, TIME, COUNT, ACTIVITY)
#   * Unit       -- named scale on a dimension (+ optional activity standard)
# PhysicalQuantity is kept as an alias of Quantity for backward compatibility,
# and the mL/mg/mEq/g constructors are imported above (now joined by mcg/L/
# kg/mmol/mol/units/percent/each).


@dataclass
class ComponentVerification:
      """Cross-reference of one order component against what the pipeline detected."""

      component: Component
      slot_id: int | None  # reference slot the component was matched to
      ndc_match: bool
      lot_match: bool | None  # None when component.lot was not specified
      exp_match: bool | None  # None when component.exp was not specified
      found: bool
      notes: list[str] = field(default_factory=list)

      def is_discrepant (self) -> bool:
            return (
                      not self.found
                      or not self.ndc_match
                      or self.lot_match is False
                      or self.exp_match is False
            )


@dataclass
class OrderResult:
      """Output of Order.run(): pipeline result plus per-component verification."""

      pipeline_result: PipelineResult
      verifications: list[ComponentVerification]
      process_inference: Optional[ProcessInferenceResult] = None

      def discrepancies (self) -> list[ComponentVerification]:
            return [v for v in self.verifications if v.is_discrepant()]

      def all_clear (self) -> bool:
            return len(self.discrepancies()) == 0

      def summary (self) -> str:
            lines = []
            if self.process_inference is not None:
                  lines.append(
                        f"Inferred compounding process: {self.process_inference.describe()}"
                  )
                  for note in self.process_inference.notes:
                        lines.append(f"  - {note}")
                  lines.append("")
            lines += [
                  self.pipeline_result.summary(),
                  "",
                  f"Order verification ({len(self.verifications)} component(s)):",
            ]
            for v in self.verifications:
                  status = "OK" if not v.is_discrepant() else "DISCREPANT"
                  lines.append(f"  [{status}] {v.component.describe()}")
                  for note in v.notes:
                        lines.append(f"    - {note}")
            disc = self.discrepancies()
            if disc:
                  lines.append(f"\n{len(disc)} discrepancy(ies) require review.")
            else:
                  lines.append("\nAll components verified.")
            return "\n".join(lines)


@dataclass
class Order:
      """Encapsulating object for a pharmacy preparation order.

      Holds the expected drug components and drives the OCR pipeline over a set
      of images. The first image passed to run() (or set as reference_image) is
      treated as the reference inventory; subsequent images are verified against it.

      expected_components: human-readable shorthand strings such as
          ["VIAL, FUROSEMIDE 100mg/10mL, #1", "NS IVPB 100ML, #1"]
      These are parsed and resolved to NDC codes via the FDA NDC Directory,
      then passed to the pipeline as priority hints that boost matching accuracy
      when OCR is weak. They complement (and can substitute for) fully-specified
      Component objects when you only know what *should* be on the tray.
      """

      id: int
      components: List[Component]
      scanned_barcodes: list[str] = field(default_factory=list)
      expected_components: list[str] = field(default_factory=list)
      reference_image: str | Path | None = None
      verification_images: List[str | Path] = field(default_factory=list)
      certified_subset: bool = False

      def add_image (self, path: str | Path, *, reference: bool = False) -> None:
            if reference:
                  self.reference_image = path
            else:
                  self.verification_images.append(path)

      def component_for_ndc (self, ndc: str) -> Component | None:
            for c in self.components:
                  if c.matches_ndc(ndc):
                        return c
            return None

      def resolve_expected_ndcs (self, directory=None) -> set[str]:
            """Resolve expected_components strings to NDC product codes.

            Parses each string in self.expected_components, looks up the drug name
            in the FDA NDC Directory, and returns the union of all matching
            product_ndc values. Returns an empty set if expected_components is empty
            or the directory is unavailable.

            Args:
                directory: pre-loaded NDCDirectory instance. When None, the module
                    singleton is loaded lazily. Pass the pipeline's ndc_directory
                    to avoid loading a second copy.
            """
            ndcs: set[str] = set()
            if not self.expected_components:
                  return ndcs
            if directory is None:
                  try:
                        try:
                              from .ocr.ndc_directory import get_directory
                        except ImportError:
                              from rxocrpl.ocr.ndc_directory import get_directory
                        directory = get_directory()
                  except Exception:
                        return ndcs
            for comp_str in self.expected_components:
                  parsed = _parse_component_string(comp_str)
                  drug = parsed.get("drug_name", "")
                  if drug and len(drug) >= 3:
                        for entry in directory.entries_for_generic(drug, max_results=20):
                              ndcs.add(entry.product_ndc)
            return ndcs

      def run (self, pipeline: Optional[Pipeline] = None) -> OrderResult:
            """Detect, OCR, embed, and match all images; verify against order components.

            Args:
                pipeline: pre-constructed Pipeline to reuse (avoids reloading models).
                    When None, a fresh Pipeline is constructed with this order's
                    certified_subset flag.

            Returns:
                OrderResult containing the full pipeline output and per-component
                verification status.
            """
            if self.reference_image is None:
                  raise ValueError("reference_image must be set before calling run()")

            if pipeline is None:
                  pipeline = Pipeline(certified_subset_in_inventory=self.certified_subset)

            images = [str(self.reference_image)] + [
                  str(p_str) for p_str in self.verification_images
            ]
            pr = pipeline.process(
                  image_paths=images,
                  certified_subset_in_inventory=self.certified_subset,
                  order=cast(Any, self),
            )
            # When fully-specified Component objects are available, prefer them
            # over the pipeline's enrichment-derived inference (more authoritative).
            order_pi: Optional[ProcessInferenceResult] = None
            if self.components:
                  order_pi = ProcessInferenceManager.from_order_components(
                        self.components
                  ).infer()
            return OrderResult(
                  pipeline_result=pr,
                  verifications=self._verify_components(pr),
                  process_inference=order_pi or pr.process_inference,
            )

      def _verify_components (self, pr: PipelineResult) -> list[ComponentVerification]:
            verifications: list[ComponentVerification] = []

            for cmpt in self.components:
                  slot_id = None
                  ndc_match = False
                  lot_match: bool | None = None
                  exp_match: bool | None = None
                  found = False
                  notes: list[str] = []

                  for slot in pr.match.slots:
                        if slot.ndc and cmpt.matches_ndc(slot.ndc):
                              slot_id = slot.slot_id
                              ndc_match = True
                              found = True
                              if cmpt.lot is not None:
                                    lot_match = slot.lot == cmpt.lot
                                    if not lot_match:
                                          notes.append(
                                                f"lot mismatch: order={cmpt.lot!r}, "
                                                f"detected={slot.lot!r}"
                                          )
                              if cmpt.exp is not None:
                                    exp_match = slot.exp == cmpt.exp
                                    if not exp_match:
                                          notes.append(
                                                f"exp mismatch: order={cmpt.exp!r}, "
                                                f"detected={slot.exp!r}"
                                          )
                              break

                  if not found:
                        expected_ndc = cmpt.package_ndc or cmpt.product.product_ndc
                        notes.append(f"no reference slot matched NDC {expected_ndc!r}")

                  verifications.append(
                        ComponentVerification(
                              component=cmpt,
                              slot_id=slot_id,
                              ndc_match=ndc_match,
                              lot_match=lot_match,
                              exp_match=exp_match,
                              found=found,
                              notes=notes,
                        )
                  )

            return verifications

      def to_dict (self) -> dict[str, Any]:
            return {
                  "id": self.id,
                  "components": [c.to_dict() for c in self.components],
                  "scanned_barcodes": self.scanned_barcodes,
                  "expected_components": self.expected_components,
                  "reference_image": str(self.reference_image)
                  if self.reference_image
                  else None,
                  "verification_images": [str(img) for img in self.verification_images],
                  "certified_subset": self.certified_subset,
            }


if __name__ == "__main__":
      from json import dumps


      def levo_40mg_250ml_ns ():
            om = OrderModel(id=1002)
            c1 = Component(
                  order=om,
                  product=Product.lookup_by_generic_name("phenylephrine", "INJECTION")[0],
                  quantity=4,
            )
            c1.numerator = Quantity(40, "mg")
            c1.denominator = Quantity(4, "mL")

            c2 = Component(
                  order=om,
                  product=Product.lookup_by_generic_name(
                        "0.9% sodium chloride", "INJECTION", "NORMAL SALINE"
                  )[0],
                  quantity=1,
            )
            c2.numerator = Quantity(250, "mL")
            c2.denominator = Quantity(250, "mL")

            o = Order(id=1002, components=[c1, c2])
            return o


      print(dumps(levo_40mg_250ml_ns().to_dict(), indent=4))


      def cisplatin_70mg_20meq_K_1000mL_NS ():
            om = OrderModel(id=1003)
            c1 = Component(
                  order=om,
                  product=Product.lookup_by_generic_name("cisplatin", "INJECTION")[0],
                  quantity=1,
            )
            c1.numerator = mg(70)
            c1.denominator = mL(70)

            c2 = Component(
                  order=om,
                  product=Product.lookup_by_generic_name("Potassium Chloride", "CONCENTRATE")[
                        0
                  ],
                  quantity=1,
            )
            c2.numerator = mEq(20)
            c2.denominator = mL(10)

            c3 = Component(
                  order=om,
                  product=Product.lookup_by_generic_name("Sodium Chloride", "INJECTION")[0],
                  quantity=1,
            )
            c3.numerator = mL(1000)
            c3.denominator = mL(1000)

            o = Order(id=1003, components=[c1, c2, c3])
            return o


      print(dumps(cisplatin_70mg_20meq_K_1000mL_NS().to_dict(), indent=4))

      ns = lookup_generic_name(
            "sodium chloride", dosage_form="INJECTION", extract_package=True
      )
      if ns:
            print(dumps(ns[0].to_dict(), indent=4))
