from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Dict, Optional

try:
    from .fda import lookup_ndc_package, lookup_generic_name
    from .pipeline import Pipeline, PipelineResult
except ImportError:
    from fda import lookup_ndc_package, lookup_generic_name
    from pipeline import Pipeline, PipelineResult


# ---------------------------------------------------------------------------
# expected_components string parser
# ---------------------------------------------------------------------------

# Container / dosage-form tokens that appear in component strings but are not
# part of the drug name.
_CONTAINER_FORMS: frozenset[str] = frozenset({
    "VIAL", "AMPULE", "AMPOULE", "SYRINGE", "IVPB", "IV", "BAG",
    "BOTTLE", "PFS", "KIT", "MDV", "SDV", "PIGGYBACK",
})

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
    r"(\d+(?:\.\d+)?)\s*(g|mg|mcg|ug|units?|u|mEq|%|mL|ML|L)"
    r"(?:\s*/\s*(\d+(?:\.\d+)?)\s*(g|mg|mcg|ug|units?|u|mEq|%|mL|ML|L))?",
    re.IGNORECASE,
)


def _parse_component_string(s: str) -> dict:
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
        drug_name = (description[:m.start()] + description[m.end():]).strip()

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


@dataclass
class PhysicalQuantity:
    value: float | int
    unit: str

    def __str__(self) -> str:
        return f"{self.value} {self.unit}"

def mL(value): return PhysicalQuantity(value, "mL")
def mg(value): return PhysicalQuantity(value, "mg")
def mEq(value): return PhysicalQuantity(value, "mEq")
def g(value): return PhysicalQuantity(value, "g")

@dataclass
class Product:
    generic_name: str
    brand_name: str | None
    labeler_name: str
    product_ndc: str
    active_ingredients: List[Dict[str, str]]
    dosage_form: str
    route: list[str]

    def __str__(self) -> str: return self.describe()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Product):
            return NotImplemented
        return (self.generic_name == other.generic_name
                and self.brand_name == other.brand_name
                and self.labeler_name == other.labeler_name
                and self.product_ndc == other.product_ndc
                and self.active_ingredients == other.active_ingredients
                and self.dosage_form == other.dosage_form
                and self.route == other.route)

    def __hash__(self) -> int:
        return hash(self.generic_name + self.brand_name + self.labeler_name + self.product_ndc + str(self.active_ingredients) + self.dosage_form + str(self.route))

    @classmethod
    def from_fda_result(cls, result: dict) -> "Product":
        return cls(
            generic_name=result.get("generic_name", ""),
            brand_name=result.get("brand_name"),
            labeler_name=result.get("labeler_name", ""),
            product_ndc=result.get("product_ndc", ""),
            active_ingredients=result.get("active_ingredients") or [],
            dosage_form=result.get("dosage_form", ""),
            route=result.get("route") or [],
        )

    @classmethod
    def lookup_by_ndc(cls, ndc: str) -> "Product | None":
        results = lookup_ndc_package(ndc)
        if results:
            return cls.from_fda_result(results[0])
        return None

    @classmethod
    def lookup_by_generic_name(
        cls, generic_name: str, dosage_form: str | None = None, brand_name: str | None = None
    ) -> "list[Product]":
        results = lookup_generic_name(generic_name, dosage_form)
        if results:
            return [cls.from_fda_result(r) for r in results]
        return []

    def describe(self) -> str:
        parts = [self.generic_name]
        if self.brand_name and self.brand_name.upper() != self.generic_name.upper():
            parts.append(f"({self.brand_name})")
        if self.dosage_form:
            parts.append(self.dosage_form)
        return " ".join(parts)


@dataclass
class Component:
    """One drug component within a compounded or prepared order.

    numerator/denominator encode concentration: e.g. 10 mg / 1 mL.
    package_ndc is the NDC of the specific package (may differ from product_ndc
    when the same drug is sold in multiple package sizes).
    """
    product: Product
    numerator: PhysicalQuantity
    denominator: PhysicalQuantity
    quantity: int = 1
    lot: str | None = None
    exp: str | None = None
    package_ndc: str | None = None

    def __str__(self) -> str: return self.describe()

    def concentration_str(self) -> str:
        return f"{self.numerator}/{self.denominator}"

    def describe(self) -> str:
        parts = [self.product.generic_name, self.concentration_str()]
        if self.product.dosage_form:
            parts.append(self.product.dosage_form)
        if self.quantity != 1:
            parts.append(f"x{self.quantity}")
        return " ".join(parts)

    def matches_ndc(self, ndc: str) -> bool:
        """True if *ndc* (package or product) belongs to this component."""
        if self.package_ndc and self.package_ndc == ndc:
            return True
        if self.product.product_ndc and self.product.product_ndc == ndc:
            return True
        return False


@dataclass
class ComponentVerification:
    """Cross-reference of one order component against what the pipeline detected."""
    component: Component
    slot_id: int | None        # reference slot the component was matched to
    ndc_match: bool
    lot_match: bool | None     # None when component.lot was not specified
    exp_match: bool | None     # None when component.exp was not specified
    found: bool
    notes: list[str] = field(default_factory=list)

    def is_discrepant(self) -> bool:
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

    def discrepancies(self) -> list[ComponentVerification]:
        return [v for v in self.verifications if v.is_discrepant()]

    def all_clear(self) -> bool:
        return len(self.discrepancies()) == 0

    def summary(self) -> str:
        lines = [self.pipeline_result.summary(), "", f"Order verification ({len(self.verifications)} component(s)):"]
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

    def add_image(self, path: str | Path, *, reference: bool = False) -> None:
        if reference:
            self.reference_image = path
        else:
            self.verification_images.append(path)

    def component_for_ndc(self, ndc: str) -> Component | None:
        for c in self.components:
            if c.matches_ndc(ndc):
                return c
        return None

    def resolve_expected_ndcs(self, directory=None) -> set[str]:
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
                    from ocr.ndc_directory import get_directory
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

    def run(self, pipeline: Optional[Pipeline] = None) -> OrderResult:
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

        images = [str(self.reference_image)] + [str(p_str) for p_str in self.verification_images]
        pr = pipeline.process(
            images,
            certified_subset_in_inventory=self.certified_subset,
            order=self,
        )
        return OrderResult(
            pipeline_result=pr,
            verifications=self._verify_components(pr),
        )

    def _verify_components(self, pr: PipelineResult) -> list[ComponentVerification]:
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


if __name__ == "__main__":
    from json import dumps

    def levo_40mg_250ml_ns():
        o = Order(id=1002, components=[
            Component(
                product=Product.lookup_by_generic_name("phenylephrine", "INJECTION")[0],
                numerator=PhysicalQuantity(40, "mg"),
                denominator=PhysicalQuantity(4, "mL"),
                quantity=4,
            ),
            Component(
                product=Product.lookup_by_generic_name(
                    "0.9% sodium chloride", "INJECTION", "NORMAL SALINE")[0],
                numerator=PhysicalQuantity(250, "mL"),
                denominator=PhysicalQuantity(250, "mL"),
                quantity=1,
            )
        ])
        return o
    o = levo_40mg_250ml_ns()
    print(dumps(asdict(o), indent=4))

    def cisplatin_70mg_20meq_K_1000mL_NS():
        o = Order(id=1003, components=[
            Component(
                product=Product.lookup_by_generic_name("cisplatin", "INJECTION")[0],
                numerator=mg(70),
                denominator=mL(70),
                quantity=1,
            ),
            Component(
                product=Product.lookup_by_generic_name("Potassium Chloride", "CONCENTRATE")[0],
                numerator=mEq(20),
                denominator=mL(10),
                quantity=1,
            ),
            Component(
                product=Product.lookup_by_generic_name("Sodium Chloride", "INJECTION")[0],
                numerator=mL(1000),
                denominator=mL(1000),
                quantity=1,
            )
        ])
        return o
    o = cisplatin_70mg_20meq_K_1000mL_NS()
    print(dumps(asdict(o), indent=4))
