"""
Process Inference
=================
Deduce compounding steps (reconstitution, dilution, repack, combination) from
the dosage forms and drug types of the components present on a preparation tray.

Accepts components in any of the formats produced elsewhere in the pipeline:
  - Enrichment dicts from Pipeline Stage 3 (keys: dosage_form, generic / drug)
  - NDCEntry objects from ocr.ndc_directory
  - Product / Component objects from order.py
  - (dosage_form, generic_name) tuples
  - Plain dosage-form strings

Example rules applied:
  - Lyophilized powder + SWFI                → Reconstitution
  - Lyophilized powder + SWFI + IV bag       → Reconstitution + Dilution
  - Lyophilized powder + IV bag (no SWFI)    → Reconstitution + Dilution
  - Concentrated injection + IV bag          → Dilution only
  - Ready-to-use vial drawn into IV bag      → Dilution / Admixture
  - Single ready-to-use vial (IM/SC/IV)      → Repack-Only
  - Multiple drugs, no clear diluent role    → Combination Admixture
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ComponentRole(str, Enum):
      """Pharmacy role of one component, inferred from FDA dosage form + generic name."""
      LYOPHILIZED_POWDER = "lyophilized_powder"
      POWDER_FOR_SOLUTION = "powder_for_solution"
      IV_DILUENT = "iv_diluent"  # NS / D5W / LR / SW large-volume bag
      SWFI = "swfi"  # Sterile Water for Injection
      CONCENTRATED_INJECTION = "concentrated_injection"  # additive / concentrate vial
      READY_INJECTION = "ready_injection"  # small-volume ready-to-use vial/ampule
      SUSPENSION = "suspension"  # injectable suspension
      UNKNOWN = "unknown"


class ProcessStep(str, Enum):
      RECONSTITUTION = "reconstitution"
      DILUTION = "dilution"
      REPACK = "repack"
      COMBINATION = "combination"


# ---------------------------------------------------------------------------
# Dosage-form classification patterns
# ---------------------------------------------------------------------------

_RE_LYOPHI = re.compile(r"LYOPHI", re.IGNORECASE)
_RE_POWDER = re.compile(r"POWDER", re.IGNORECASE)
_RE_SUSPENSION = re.compile(r"SUSPENSION", re.IGNORECASE)
_RE_CONCENTRATE = re.compile(r"CONCENTRAT", re.IGNORECASE)
_RE_INJECTION = re.compile(r"INJECTION", re.IGNORECASE)

# Generic names that mark a plain IV carrier / diluent bag.
_IV_DILUENTS = re.compile(
      r"\b(sodium\s+chloride|normal\s+saline|dextrose|lactated\s+ringer|"
      r"ringers?\s+lactate|ringer'?s?|dextrose\s+sodium\s+chloride|"
      r"0\.9\s*%?\s*nacl|d5w|d10w|d5ns|d5lr)\b",
      re.IGNORECASE,
)

# Names that indicate sterile water diluent.
_SWFI_PATTERN = re.compile(
      r"\b(sterile\s+water|water\s+for\s+injection|bacteriostatic\s+water|"
      r"\bswfi\b|\bswi\b)\b",
      re.IGNORECASE,
)


def classify_component(dosage_form: str, generic_name: str = "") -> ComponentRole:
      """Map (dosage_form, generic_name) to a ComponentRole.

      Uses the FDA DOSAGEFORMNAME string as the primary signal and the
      nonproprietary name to distinguish diluent carriers from drug solutions.
      """
      df = (dosage_form or "").strip()
      gn = (generic_name or "").strip()

      # SWFI — must check before the generic injection path
      if _SWFI_PATTERN.search(gn):
            return ComponentRole.SWFI

      # Lyophilized powder — requires reconstitution
      if _RE_LYOPHI.search(df):
            return ComponentRole.LYOPHILIZED_POWDER

      # Non-lyophilized reconstitutable powder
      if _RE_POWDER.search(df) and _RE_INJECTION.search(df):
            if _RE_SUSPENSION.search(df):
                  return ComponentRole.SUSPENSION
            return ComponentRole.POWDER_FOR_SOLUTION

      # Injectable suspension (no powder keyword)
      if _RE_SUSPENSION.search(df) and _RE_INJECTION.search(df):
            return ComponentRole.SUSPENSION

      # Concentrated solution — needs dilution
      if _RE_CONCENTRATE.search(df):
            return ComponentRole.CONCENTRATED_INJECTION

      # Plain injection solutions — differentiate carriers from drugs
      if _RE_INJECTION.search(df):
            if _IV_DILUENTS.search(gn):
                  return ComponentRole.IV_DILUENT
            return ComponentRole.READY_INJECTION

      return ComponentRole.UNKNOWN


# ---------------------------------------------------------------------------
# Inference result
# ---------------------------------------------------------------------------

@dataclass
class ProcessInferenceResult:
      """The inferred compounding process for a set of components."""
      needs_recon: bool
      needs_dilution: bool
      repack_only: bool
      steps: list[ProcessStep]
      roles: list[ComponentRole]  # one entry per input component (parallel list)
      notes: list[str] = field(default_factory=list)

      def describe(self) -> str:
            """Short human-readable summary of the inferred process."""
            if self.repack_only:
                  return "Repack-Only"
            parts = []
            if self.needs_recon:
                  parts.append("Reconstitution")
            if self.needs_dilution:
                  parts.append("Dilution")
            if ProcessStep.COMBINATION in self.steps and not parts:
                  parts.append("Combination Admixture")
            return " + ".join(parts) if parts else "Unknown Process"

      def to_dict(self) -> dict:
            return {
                  "description": self.describe(),
                  "needs_recon": self.needs_recon,
                  "needs_dilution": self.needs_dilution,
                  "repack_only": self.repack_only,
                  "steps": [s.value for s in self.steps],
                  "roles": [r.value for r in self.roles],
                  "notes": self.notes,
            }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class ProcessInferenceManager:
      """Infer compounding steps from a list of detected/expected components.

      Parameters
      ----------
      components:
          Each element may be a pipeline enrichment dict, NDCEntry,
          Product/Component from order.py, a (dosage_form, generic_name)
          tuple, or a plain dosage-form string.
      dosage_form:
          Expected final dosage form of the *prepared* product (optional).
          Used to confirm Repack-Only when the single input component's form
          matches the intended final form (e.g. drawing from an IM vial).
      """

      def __init__(
                self,
                components: list | None = None,
                dosage_form: str | None = None,
      ) -> None:
            self.components: list = components or []
            self.dosage_form: str | None = dosage_form
            self.limit_to_components: bool = True
            # Back-compat attributes — populated by infer()
            self.needs_recon: bool | None = None
            self.needs_dilute: bool | None = None
            self.repack_only: bool = False

      # ------------------------------------------------------------------
      # Component format normalisation
      # ------------------------------------------------------------------

      @staticmethod
      def _extract_form_and_generic(component: Any) -> tuple[str, str]:
            """Return (dosage_form, generic_name) from any supported component type."""
            # dict — pipeline enrichment record or arbitrary mapping
            if isinstance(component, dict):
                  df = component.get("dosage_form") or component.get("form") or ""
                  gn = (
                            component.get("generic")
                            or component.get("generic_name")
                            or component.get("drug")
                            or ""
                  )
                  return str(df), str(gn)

            # NDCEntry from ocr.ndc_directory
            if type(component).__name__ == "NDCEntry":
                  return (
                        str(getattr(component, "dosage_form", "") or ""),
                        str(getattr(component, "nonproprietary_name", "") or "")
                  )

            # Component (wraps a Product) from order.py
            product = getattr(component, "product", None)
            if product is not None:
                  return (
                        getattr(product, "dosage_form", "") or "",
                        getattr(product, "generic_name", "") or "",
                  )

            # Product from order.py (direct)
            if hasattr(component, "dosage_form") and hasattr(component, "generic_name"):
                  return (
                        getattr(component, "dosage_form", "") or "",
                        getattr(component, "generic_name", "") or "",
                  )

            # (dosage_form, generic_name) tuple or list
            if isinstance(component, (tuple, list)) and len(component) >= 2:
                  return str(component[0]), str(component[1])

            # Plain string — treat as dosage_form
            if isinstance(component, str):
                  return component, ""

            return "", ""

      # ------------------------------------------------------------------
      # Public API
      # ------------------------------------------------------------------

      def classify(self) -> list[ComponentRole]:
            """Return a ComponentRole for each component in self.components."""
            return [
                  classify_component(*self._extract_form_and_generic(c))
                  for c in self.components
            ]

      def infer(self) -> ProcessInferenceResult:
            """Analyse components and return a ProcessInferenceResult.

            Also sets self.needs_recon / self.needs_dilute / self.repack_only
            for backwards-compatible attribute access.
            """
            roles = self.classify()
            role_set = set(roles)
            n = len(roles)
            notes: list[str] = []
            steps: list[ProcessStep] = []

            has_lyo = ComponentRole.LYOPHILIZED_POWDER in role_set
            has_powder = ComponentRole.POWDER_FOR_SOLUTION in role_set
            has_swfi = ComponentRole.SWFI in role_set
            has_iv = ComponentRole.IV_DILUENT in role_set
            has_conc = ComponentRole.CONCENTRATED_INJECTION in role_set
            has_ready = ComponentRole.READY_INJECTION in role_set
            has_susp = ComponentRole.SUSPENSION in role_set
            any_powder = has_lyo or has_powder

            needs_recon = False
            needs_dilution = False
            repack_only = False

            # ── Single-component ────────────────────────────────────────────
            if n == 1:
                  sole = roles[0]
                  if sole in (ComponentRole.READY_INJECTION, ComponentRole.SUSPENSION):
                        repack_only = True
                        steps.append(ProcessStep.REPACK)
                        notes.append("Single ready-to-use vial/suspension — draw-up (repack) only.")
                  elif sole in (ComponentRole.LYOPHILIZED_POWDER, ComponentRole.POWDER_FOR_SOLUTION):
                        needs_recon = True
                        steps.append(ProcessStep.RECONSTITUTION)
                        notes.append(
                              "Single powder on tray — diluent not detected; "
                              "reconstitution required but diluent component is missing."
                        )
                  elif sole == ComponentRole.CONCENTRATED_INJECTION:
                        needs_dilution = True
                        steps.append(ProcessStep.DILUTION)
                        notes.append(
                              "Single concentrate on tray — diluent not detected; "
                              "dilution required but diluent component is missing."
                        )
                  elif sole == ComponentRole.IV_DILUENT:
                        notes.append("Single IV diluent bag only — no drug component detected.")
                  elif sole == ComponentRole.SWFI:
                        notes.append("Single SWFI vial only — no drug component detected.")
                  else:
                        notes.append("Single component; process steps could not be determined.")

            # ── Multi-component ─────────────────────────────────────────────
            else:
                  # Reconstitution: powder present with any aqueous diluent
                  if any_powder and (has_swfi or has_iv):
                        needs_recon = True
                        steps.append(ProcessStep.RECONSTITUTION)
                        if has_swfi and not has_iv:
                              notes.append("Powder + SWFI → Reconstitution only (no IV bag).")
                        elif has_swfi and has_iv:
                              notes.append("Powder + SWFI + IV bag → Reconstitute in SWFI, then dilute into bag.")
                        else:
                              notes.append(
                                    "Powder + IV bag (no SWFI) → Reconstitute directly in bag "
                                    "or via transfer."
                              )

                  # Dilution: any path that ends with a transfer into an IV bag.
                  # Powder + IV bag always implies the reconstituted drug goes into the bag.
                  # SWFI + IV bag: reconstitute in SWFI, then add to bag.
                  # Concentrate / ready vial + IV bag (no powder): straight dilution.
                  if any_powder and has_iv:
                        needs_dilution = True
                        if ProcessStep.DILUTION not in steps:
                              steps.append(ProcessStep.DILUTION)
                  elif has_swfi and has_iv and not any_powder:
                        needs_dilution = True
                        if ProcessStep.DILUTION not in steps:
                              steps.append(ProcessStep.DILUTION)
                        notes.append("SWFI + IV bag (no powder) → add SWFI to bag as diluent.")
                  elif not any_powder:
                        if has_conc and has_iv:
                              needs_dilution = True
                              steps.append(ProcessStep.DILUTION)
                              notes.append("Concentrated injection + IV bag → Dilution only.")
                        elif has_ready and has_iv:
                              needs_dilution = True
                              steps.append(ProcessStep.DILUTION)
                              notes.append(
                                    "Ready-to-use vial + IV bag → Draw drug from vial into bag "
                                    "(IV admixture / dilution)."
                              )

                  # Combination admixture: ≥2 drug components, no clear recon/dilution role
                  drug_roles = [
                        r for r in roles
                        if r not in (
                              ComponentRole.IV_DILUENT,
                              ComponentRole.SWFI,
                              ComponentRole.UNKNOWN,
                        )
                  ]
                  if len(drug_roles) >= 2 and not needs_recon and not needs_dilution:
                        steps.append(ProcessStep.COMBINATION)
                        notes.append(
                              f"{len(drug_roles)} drug components with no identified diluent — "
                              "likely a combination admixture."
                        )

                  if not steps:
                        notes.append(
                              "Multiple components detected but no inference rule matched; "
                              "manual review required."
                        )

            # Final-form confirmation for repack (single component)
            if self.dosage_form and n == 1 and not needs_recon and not needs_dilution:
                  comp_df, _ = self._extract_form_and_generic(self.components[0])
                  if comp_df and comp_df.upper() in self.dosage_form.upper():
                        repack_only = True
                        if ProcessStep.REPACK not in steps:
                              steps.append(ProcessStep.REPACK)
                        notes.append(
                              f"Component dosage form '{comp_df}' matches expected final form "
                              f"'{self.dosage_form}' — confirmed Repack-Only."
                        )

            # Write back legacy attributes
            self.needs_recon = needs_recon
            self.needs_dilute = needs_dilution
            self.repack_only = repack_only

            return ProcessInferenceResult(
                  needs_recon=needs_recon,
                  needs_dilution=needs_dilution,
                  repack_only=repack_only,
                  steps=steps,
                  roles=roles,
                  notes=notes,
            )

      # ------------------------------------------------------------------
      # Convenience constructors
      # ------------------------------------------------------------------

      @classmethod
      def from_pipeline_enrichment(
                cls,
                enrichment: dict[int, dict],
                dosage_form: str | None = None,
      ) -> "ProcessInferenceManager":
            """Build from the pipeline Stage-3 enrichment dict (keyed by instance_id)."""
            return cls(components=list(enrichment.values()), dosage_form=dosage_form)

      @classmethod
      def from_order_components(
                cls,
                components: list,
                dosage_form: str | None = None,
      ) -> "ProcessInferenceManager":
            """Build from a list of order.Component objects."""
            return cls(components=components, dosage_form=dosage_form)
