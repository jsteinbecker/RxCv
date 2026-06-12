"""
Test suite for process_inference.

Covers:
  * classify_component  — every ComponentRole and the regex edge cases
  * ProcessInferenceManager._extract_form_and_generic — all input formats
  * ProcessInferenceManager.classify / infer — every documented rule plus quirks
  * ProcessInferenceResult.describe / to_dict
  * legacy attribute write-back and the alternate constructors

Run with:  python -m unittest test_process_inference
"""
from __future__ import annotations

import sys
import types
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from rxocrpl.detection.process_inference import (
      ComponentRole,
      ProcessStep,
      ProcessInferenceManager,
      ProcessInferenceResult,
      classify_component,
)


# ---------------------------------------------------------------------------
# Test doubles for the order.py Product / Component object shapes
# ---------------------------------------------------------------------------

@dataclass
class FakeProduct:
      """Mimics order.Product (has .dosage_form and .generic_name attrs)."""
      dosage_form: str = ""
      generic_name: str = ""


@dataclass
class FakeComponent:
      """Mimics order.Component (wraps a .product)."""
      product: FakeProduct


# ===========================================================================
# classify_component
# ===========================================================================

class TestClassifyComponent(unittest.TestCase):
      """classify_component — every ComponentRole and regex edge cases."""

      # ── SWFI ────────────────────────────────────────────────────────────────

      def test_swfi_sterile_water_for_injection(self):
            self.assertEqual(
                  classify_component("INJECTION, SOLUTION", "Sterile Water for Injection"),
                  ComponentRole.SWFI,
            )

      def test_swfi_bacteriostatic_water(self):
            self.assertEqual(
                  classify_component("INJECTION, SOLUTION", "Bacteriostatic Water for Injection"),
                  ComponentRole.SWFI,
            )

      def test_swfi_abbreviation(self):
            self.assertEqual(
                  classify_component("INJECTION, SOLUTION", "SWFI"),
                  ComponentRole.SWFI,
            )

      def test_swi_abbreviation(self):
            self.assertEqual(
                  classify_component("INJECTION, SOLUTION", "SWI"),
                  ComponentRole.SWFI,
            )

      # ── Lyophilized powder ───────────────────────────────────────────────────

      def test_lyophilized_powder_for_solution(self):
            self.assertEqual(
                  classify_component("POWDER, LYOPHILIZED, FOR SOLUTION", "Vancomycin"),
                  ComponentRole.LYOPHILIZED_POWDER,
            )

      def test_injection_lyophilized_powder(self):
            self.assertEqual(
                  classify_component("INJECTION, LYOPHILIZED POWDER", "Cefazolin"),
                  ComponentRole.LYOPHILIZED_POWDER,
            )

      # ── Non-lyophilized powder ───────────────────────────────────────────────

      def test_powder_for_solution(self):
            self.assertEqual(
                  classify_component("INJECTION, POWDER, FOR SOLUTION", "Piperacillin"),
                  ComponentRole.POWDER_FOR_SOLUTION,
            )

      # ── Suspension (from powder form) ────────────────────────────────────────

      def test_powder_for_suspension_is_suspension(self):
            self.assertEqual(
                  classify_component("INJECTION, POWDER, FOR SUSPENSION", "Ceftriaxone"),
                  ComponentRole.SUSPENSION,
            )

      # ── Suspension (no powder keyword) ───────────────────────────────────────

      def test_plain_injectable_suspension(self):
            self.assertEqual(
                  classify_component("INJECTION, SUSPENSION", "Methylprednisolone"),
                  ComponentRole.SUSPENSION,
            )

      # ── Concentrate ──────────────────────────────────────────────────────────

      def test_concentrate(self):
            self.assertEqual(
                  classify_component("INJECTION, SOLUTION, CONCENTRATE", "Potassium Chloride"),
                  ComponentRole.CONCENTRATED_INJECTION,
            )

      # ── Ready injection ──────────────────────────────────────────────────────

      def test_plain_injection_drug_is_ready(self):
            self.assertEqual(
                  classify_component("INJECTION, SOLUTION", "Vancomycin"),
                  ComponentRole.READY_INJECTION,
            )

      # ── IV diluents ──────────────────────────────────────────────────────────

      def test_iv_diluent_sodium_chloride(self):
            self.assertEqual(
                  classify_component("INJECTION, SOLUTION", "Sodium Chloride"),
                  ComponentRole.IV_DILUENT,
            )

      def test_iv_diluent_dextrose(self):
            self.assertEqual(
                  classify_component("INJECTION, SOLUTION", "Dextrose"),
                  ComponentRole.IV_DILUENT,
            )

      def test_iv_diluent_lactated_ringers(self):
            self.assertEqual(
                  classify_component("INJECTION, SOLUTION", "Lactated Ringer's"),
                  ComponentRole.IV_DILUENT,
            )

      def test_iv_diluent_nacl_abbreviation(self):
            self.assertEqual(
                  classify_component("INJECTION, SOLUTION", "0.9% NaCl"),
                  ComponentRole.IV_DILUENT,
            )

      def test_iv_diluent_d5w(self):
            self.assertEqual(
                  classify_component("INJECTION, SOLUTION", "D5W"),
                  ComponentRole.IV_DILUENT,
            )

      # ── Unknown ──────────────────────────────────────────────────────────────

      def test_tablet_is_unknown(self):
            self.assertEqual(
                  classify_component("TABLET", "Acetaminophen"),
                  ComponentRole.UNKNOWN,
            )

      def test_empty_strings_are_unknown(self):
            self.assertEqual(classify_component("", ""), ComponentRole.UNKNOWN)

      # ── Edge cases ───────────────────────────────────────────────────────────

      def test_generic_name_is_optional(self):
            self.assertEqual(
                  classify_component("INJECTION, LYOPHILIZED POWDER"),
                  ComponentRole.LYOPHILIZED_POWDER,
            )

      def test_classification_is_case_insensitive_form(self):
            self.assertEqual(
                  classify_component("injection, lyophilized powder"),
                  ComponentRole.LYOPHILIZED_POWDER,
            )

      def test_classification_is_case_insensitive_generic(self):
            self.assertEqual(
                  classify_component("INJECTION, SOLUTION", "sodium chloride"),
                  ComponentRole.IV_DILUENT,
            )

      def test_whitespace_is_stripped(self):
            self.assertEqual(
                  classify_component("  INJECTION, SOLUTION  ", "  Vancomycin  "),
                  ComponentRole.READY_INJECTION,
            )

      def test_none_inputs_do_not_raise(self):
            # None is coerced to empty string inside the function.
            self.assertEqual(classify_component(None, None), ComponentRole.UNKNOWN)  # type: ignore[arg-type]

      def test_bare_powder_without_injection_keyword_is_unknown(self):
            # POWDER alone (no INJECTION) must not satisfy the powder rule.
            self.assertEqual(classify_component("POWDER", "Something"), ComponentRole.UNKNOWN)

      def test_concentrate_beats_iv_diluent_generic(self):
            # CONCENTRAT is checked before the IV-diluent generic scan.
            self.assertEqual(
                  classify_component("INJECTION, CONCENTRATE", "Sodium Chloride"),
                  ComponentRole.CONCENTRATED_INJECTION,
            )

      def test_swfi_word_boundary_does_not_overmatch(self):
            # "swi" embedded in another word must not fire the SWFI rule.
            self.assertEqual(
                  classify_component("INJECTION, SOLUTION", "swirl agent"),
                  ComponentRole.READY_INJECTION,
            )


# ===========================================================================
# _extract_form_and_generic — input format normalisation
# ===========================================================================

class TestExtractFormAndGeneric(unittest.TestCase):
      """ProcessInferenceManager._extract_form_and_generic — all input formats."""

      extract = staticmethod(ProcessInferenceManager._extract_form_and_generic)

      def test_pipeline_enrichment_dict_dosage_form_and_generic(self):
            comp = {"dosage_form": "INJECTION, SOLUTION", "generic": "Vancomycin"}
            self.assertEqual(self.extract(comp), ("INJECTION, SOLUTION", "Vancomycin"))

      def test_dict_alternate_keys_form_and_drug(self):
            comp = {"form": "INJECTION, SOLUTION", "drug": "Cefazolin"}
            self.assertEqual(self.extract(comp), ("INJECTION, SOLUTION", "Cefazolin"))

      def test_dict_generic_name_key(self):
            comp = {"dosage_form": "TABLET", "generic_name": "Aspirin"}
            self.assertEqual(self.extract(comp), ("TABLET", "Aspirin"))

      def test_dict_missing_keys_returns_empty_strings(self):
            self.assertEqual(self.extract({}), ("", ""))

      def test_component_wrapping_product(self):
            comp = FakeComponent(FakeProduct("INJECTION, SOLUTION", "Vancomycin"))
            self.assertEqual(self.extract(comp), ("INJECTION, SOLUTION", "Vancomycin"))

      def test_product_directly(self):
            comp = FakeProduct("INJECTION, LYOPHILIZED POWDER", "Cefazolin")
            self.assertEqual(self.extract(comp), ("INJECTION, LYOPHILIZED POWDER", "Cefazolin"))

      def test_tuple(self):
            self.assertEqual(
                  self.extract(("INJECTION, SOLUTION", "Heparin")),
                  ("INJECTION, SOLUTION", "Heparin"),
            )

      def test_list(self):
            self.assertEqual(
                  self.extract(["INJECTION, SOLUTION", "Heparin"]),
                  ("INJECTION, SOLUTION", "Heparin"),
            )

      def test_plain_string_is_dosage_form_only(self):
            self.assertEqual(self.extract("INJECTION, SOLUTION"), ("INJECTION, SOLUTION", ""))

      def test_integer_returns_empty_strings(self):
            self.assertEqual(self.extract(42), ("", ""))

      def test_none_returns_empty_strings(self):
            self.assertEqual(self.extract(None), ("", ""))

      def test_dict_coerces_non_string_values(self):
            self.assertEqual(self.extract({"dosage_form": 5, "generic": 7}), ("5", "7"))

      def test_ndc_entry_branch(self):
            """Cover the NDCEntry branch by injecting a stub into sys.modules."""

            @dataclass
            class NDCEntry:
                  dosage_form: str
                  nonproprietary_name: str

            ocr_pkg = types.ModuleType("ocr")
            ndc_mod = types.ModuleType("ocr.ndc_directory")
            ndc_mod.NDCEntry = NDCEntry
            ocr_pkg.ndc_directory = ndc_mod

            with patch.dict(sys.modules, {"ocr": ocr_pkg, "ocr.ndc_directory": ndc_mod}):
                  entry = NDCEntry("INJECTION, LYOPHILIZED POWDER", "Cefazolin")
                  self.assertEqual(
                        self.extract(entry),
                        ("INJECTION, LYOPHILIZED POWDER", "Cefazolin"),
                  )


# ===========================================================================
# classify() — per-component role list
# ===========================================================================

class TestManagerClassify(unittest.TestCase):
      """ProcessInferenceManager.classify — roles parallel to input list."""

      def test_mixed_component_formats(self):
            mgr = ProcessInferenceManager(components=[
                  {"dosage_form": "INJECTION, LYOPHILIZED POWDER", "generic": "Cefazolin"},
                  ("INJECTION, SOLUTION", "Sterile Water for Injection"),
                  FakeProduct("INJECTION, SOLUTION", "Sodium Chloride"),
            ])
            self.assertEqual(mgr.classify(), [
                  ComponentRole.LYOPHILIZED_POWDER,
                  ComponentRole.SWFI,
                  ComponentRole.IV_DILUENT,
            ])

      def test_empty_components_returns_empty_list(self):
            self.assertEqual(ProcessInferenceManager().classify(), [])


# ===========================================================================
# infer() — single component
# ===========================================================================

class TestInferSingleComponent(unittest.TestCase):
      """infer() — single-component tray rules."""

      def test_single_ready_vial_is_repack_only(self):
            res = ProcessInferenceManager([("INJECTION, SOLUTION", "Vancomycin")]).infer()
            self.assertTrue(res.repack_only)
            self.assertFalse(res.needs_recon)
            self.assertFalse(res.needs_dilution)
            self.assertEqual(res.steps, [ProcessStep.REPACK])
            self.assertEqual(res.describe(), "Repack-Only")

      def test_single_suspension_is_repack_only(self):
            res = ProcessInferenceManager([("INJECTION, SUSPENSION", "Methylprednisolone")]).infer()
            self.assertTrue(res.repack_only)
            self.assertEqual(res.steps, [ProcessStep.REPACK])

      def test_single_lyophilized_powder_needs_recon_missing_diluent(self):
            res = ProcessInferenceManager([("INJECTION, LYOPHILIZED POWDER", "Cefazolin")]).infer()
            self.assertTrue(res.needs_recon)
            self.assertFalse(res.needs_dilution)
            self.assertFalse(res.repack_only)
            self.assertEqual(res.steps, [ProcessStep.RECONSTITUTION])
            self.assertTrue(any("diluent component is missing" in n for n in res.notes))
            self.assertEqual(res.describe(), "Reconstitution")

      def test_single_powder_for_solution_needs_recon(self):
            res = ProcessInferenceManager([("INJECTION, POWDER, FOR SOLUTION", "Piperacillin")]).infer()
            self.assertTrue(res.needs_recon)
            self.assertEqual(res.steps, [ProcessStep.RECONSTITUTION])

      def test_single_concentrate_needs_dilution_missing_diluent(self):
            res = ProcessInferenceManager([("INJECTION, CONCENTRATE", "Potassium Chloride")]).infer()
            self.assertTrue(res.needs_dilution)
            self.assertFalse(res.needs_recon)
            self.assertEqual(res.steps, [ProcessStep.DILUTION])
            self.assertTrue(any("diluent component is missing" in n for n in res.notes))
            self.assertEqual(res.describe(), "Dilution")

      def test_single_iv_diluent_only_no_drug(self):
            res = ProcessInferenceManager([("INJECTION, SOLUTION", "Sodium Chloride")]).infer()
            self.assertEqual(res.steps, [])
            self.assertFalse(res.repack_only)
            self.assertTrue(any("no drug component" in n for n in res.notes))
            self.assertEqual(res.describe(), "Unknown Process")

      def test_single_swfi_only_no_drug(self):
            res = ProcessInferenceManager(
                  [("INJECTION, SOLUTION", "Sterile Water for Injection")]
            ).infer()
            self.assertEqual(res.steps, [])
            self.assertTrue(any("no drug component" in n for n in res.notes))

      def test_single_unknown_component_undetermined(self):
            res = ProcessInferenceManager([("TABLET", "Acetaminophen")]).infer()
            self.assertEqual(res.steps, [])
            self.assertFalse(res.repack_only)
            self.assertTrue(any("could not be determined" in n for n in res.notes))


# ===========================================================================
# infer() — multi-component documented rules
# ===========================================================================

class TestInferMultiComponent(unittest.TestCase):
      """infer() — multi-component tray rules per module docstring examples."""

      def test_powder_plus_swfi_reconstitution_only(self):
            res = ProcessInferenceManager([
                  ("INJECTION, LYOPHILIZED POWDER", "Cefazolin"),
                  ("INJECTION, SOLUTION", "Sterile Water for Injection"),
            ]).infer()
            self.assertTrue(res.needs_recon)
            self.assertFalse(res.needs_dilution)
            self.assertEqual(res.steps, [ProcessStep.RECONSTITUTION])
            self.assertEqual(res.describe(), "Reconstitution")
            self.assertTrue(any("Reconstitution only" in n for n in res.notes))

      def test_powder_plus_swfi_plus_iv_recon_then_dilution(self):
            res = ProcessInferenceManager([
                  ("INJECTION, LYOPHILIZED POWDER", "Cefazolin"),
                  ("INJECTION, SOLUTION", "Sterile Water for Injection"),
                  ("INJECTION, SOLUTION", "Sodium Chloride"),
            ]).infer()
            self.assertTrue(res.needs_recon)
            self.assertTrue(res.needs_dilution)
            self.assertEqual(res.steps, [ProcessStep.RECONSTITUTION, ProcessStep.DILUTION])
            self.assertEqual(res.describe(), "Reconstitution + Dilution")

      def test_powder_plus_iv_no_swfi_recon_and_dilution(self):
            res = ProcessInferenceManager([
                  ("INJECTION, LYOPHILIZED POWDER", "Zosyn"),
                  ("INJECTION, SOLUTION", "Dextrose"),
            ]).infer()
            self.assertTrue(res.needs_recon)
            self.assertTrue(res.needs_dilution)
            self.assertEqual(res.steps, [ProcessStep.RECONSTITUTION, ProcessStep.DILUTION])
            self.assertTrue(any("no SWFI" in n for n in res.notes))

      def test_concentrate_plus_iv_dilution_only(self):
            res = ProcessInferenceManager([
                  ("INJECTION, CONCENTRATE", "Potassium Chloride"),
                  ("INJECTION, SOLUTION", "Sodium Chloride"),
            ]).infer()
            self.assertFalse(res.needs_recon)
            self.assertTrue(res.needs_dilution)
            self.assertEqual(res.steps, [ProcessStep.DILUTION])
            self.assertEqual(res.describe(), "Dilution")
            self.assertTrue(any("Dilution only" in n for n in res.notes))

      def test_ready_vial_plus_iv_admixture_dilution(self):
            res = ProcessInferenceManager([
                  ("INJECTION, SOLUTION", "Vancomycin"),
                  ("INJECTION, SOLUTION", "Sodium Chloride"),
            ]).infer()
            self.assertTrue(res.needs_dilution)
            self.assertEqual(res.steps, [ProcessStep.DILUTION])
            self.assertTrue(any("IV admixture" in n for n in res.notes))

      def test_swfi_plus_iv_no_powder_dilution(self):
            res = ProcessInferenceManager([
                  ("INJECTION, SOLUTION", "Sterile Water for Injection"),
                  ("INJECTION, SOLUTION", "Sodium Chloride"),
            ]).infer()
            self.assertFalse(res.needs_recon)
            self.assertTrue(res.needs_dilution)
            self.assertEqual(res.steps, [ProcessStep.DILUTION])
            self.assertTrue(any("add SWFI to bag" in n for n in res.notes))

      def test_two_ready_drugs_no_diluent_is_combination(self):
            res = ProcessInferenceManager([
                  ("INJECTION, SOLUTION", "Vancomycin"),
                  ("INJECTION, SOLUTION", "Gentamicin"),
            ]).infer()
            self.assertFalse(res.needs_recon)
            self.assertFalse(res.needs_dilution)
            self.assertEqual(res.steps, [ProcessStep.COMBINATION])
            self.assertEqual(res.describe(), "Combination Admixture")
            self.assertTrue(any("combination admixture" in n for n in res.notes))

      def test_two_powders_no_diluent_falls_to_combination(self):
            # Documents current behaviour: two powders with no diluent do not
            # trigger reconstitution (no diluent is present) and fall through to
            # the combination rule.  This may indicate a missing-diluent warning
            # is needed.
            res = ProcessInferenceManager([
                  ("INJECTION, LYOPHILIZED POWDER", "Cefazolin"),
                  ("INJECTION, LYOPHILIZED POWDER", "Vancomycin"),
            ]).infer()
            self.assertFalse(res.needs_recon)
            self.assertEqual(res.steps, [ProcessStep.COMBINATION])
            self.assertEqual(res.describe(), "Combination Admixture")

      def test_concentrate_branch_wins_over_ready_vial_branch(self):
            # has_conc is tested before has_ready (elif ordering in infer()).
            res = ProcessInferenceManager([
                  ("INJECTION, CONCENTRATE", "Potassium Chloride"),
                  ("INJECTION, SOLUTION", "Vancomycin"),
                  ("INJECTION, SOLUTION", "Sodium Chloride"),
            ]).infer()
            self.assertTrue(res.needs_dilution)
            self.assertEqual(res.steps, [ProcessStep.DILUTION])
            self.assertTrue(any("Dilution only" in n for n in res.notes))
            self.assertFalse(any("IV admixture" in n for n in res.notes))

      def test_two_unknown_components_no_rule_matches(self):
            res = ProcessInferenceManager([
                  ("TABLET", "Acetaminophen"),
                  ("CAPSULE", "Ibuprofen"),
            ]).infer()
            self.assertEqual(res.steps, [])
            self.assertTrue(any("no inference rule matched" in n for n in res.notes))
            self.assertEqual(res.describe(), "Unknown Process")

      def test_empty_component_list(self):
            res = ProcessInferenceManager([]).infer()
            self.assertEqual(res.steps, [])
            self.assertEqual(res.roles, [])
            self.assertFalse(res.repack_only)
            self.assertEqual(res.describe(), "Unknown Process")

      def test_roles_list_parallels_input_order(self):
            res = ProcessInferenceManager([
                  ("INJECTION, LYOPHILIZED POWDER", "Cefazolin"),
                  ("INJECTION, SOLUTION", "Sodium Chloride"),
            ]).infer()
            self.assertEqual(
                  res.roles,
                  [ComponentRole.LYOPHILIZED_POWDER, ComponentRole.IV_DILUENT],
            )


# ===========================================================================
# infer() — final-form repack confirmation
# ===========================================================================

class TestRepackConfirmation(unittest.TestCase):
      """infer() — dosage_form argument confirms repack for single components."""

      def test_matching_final_form_confirms_repack(self):
            mgr = ProcessInferenceManager([("TABLET", "Acetaminophen")], dosage_form="TABLET")
            res = mgr.infer()
            self.assertTrue(res.repack_only)
            self.assertIn(ProcessStep.REPACK, res.steps)
            self.assertTrue(any("confirmed Repack-Only" in n for n in res.notes))

      def test_iv_diluent_with_matching_final_form_becomes_repack(self):
            mgr = ProcessInferenceManager(
                  [("INJECTION, SOLUTION", "Sodium Chloride")],
                  dosage_form="INJECTION, SOLUTION",
            )
            res = mgr.infer()
            self.assertTrue(res.repack_only)
            self.assertIn(ProcessStep.REPACK, res.steps)

      def test_no_repack_when_final_form_mismatches(self):
            mgr = ProcessInferenceManager([("TABLET", "Acetaminophen")], dosage_form="CAPSULE")
            res = mgr.infer()
            self.assertFalse(res.repack_only)
            self.assertNotIn(ProcessStep.REPACK, res.steps)

      def test_final_form_does_not_override_reconstitution(self):
            # Repack confirmation is guarded by `not needs_recon`.
            mgr = ProcessInferenceManager(
                  [("INJECTION, LYOPHILIZED POWDER", "Cefazolin")],
                  dosage_form="INJECTION, LYOPHILIZED POWDER",
            )
            res = mgr.infer()
            self.assertTrue(res.needs_recon)
            self.assertFalse(res.repack_only)


# ===========================================================================
# Legacy attribute write-back
# ===========================================================================

class TestLegacyAttributes(unittest.TestCase):
      """needs_recon / needs_dilute / repack_only attributes on the manager."""

      def test_attributes_are_none_before_infer(self):
            mgr = ProcessInferenceManager([("INJECTION, SOLUTION", "Vancomycin")])
            self.assertIsNone(mgr.needs_recon)
            self.assertIsNone(mgr.needs_dilute)
            self.assertFalse(mgr.repack_only)

      def test_attributes_are_written_after_infer(self):
            mgr = ProcessInferenceManager([
                  ("INJECTION, LYOPHILIZED POWDER", "Cefazolin"),
                  ("INJECTION, SOLUTION", "Sodium Chloride"),
            ])
            mgr.infer()
            self.assertTrue(mgr.needs_recon)
            self.assertTrue(mgr.needs_dilute)  # note: legacy attr is needs_dilute
            self.assertFalse(mgr.repack_only)


# ===========================================================================
# ProcessInferenceResult helpers
# ===========================================================================

class TestProcessInferenceResult(unittest.TestCase):
      """ProcessInferenceResult.describe() and to_dict()."""

      def _make(self, **kwargs):
            defaults = dict(
                  needs_recon=False, needs_dilution=False, repack_only=False,
                  steps=[], roles=[],
            )
            defaults.update(kwargs)
            return ProcessInferenceResult(**defaults)

      def test_describe_repack_takes_precedence_over_other_flags(self):
            res = self._make(
                  needs_recon=True, needs_dilution=True, repack_only=True,
                  steps=[ProcessStep.REPACK],
            )
            self.assertEqual(res.describe(), "Repack-Only")

      def test_describe_reconstitution_only(self):
            res = self._make(needs_recon=True, steps=[ProcessStep.RECONSTITUTION])
            self.assertEqual(res.describe(), "Reconstitution")

      def test_describe_dilution_only(self):
            res = self._make(needs_dilution=True, steps=[ProcessStep.DILUTION])
            self.assertEqual(res.describe(), "Dilution")

      def test_describe_reconstitution_and_dilution(self):
            res = self._make(
                  needs_recon=True, needs_dilution=True,
                  steps=[ProcessStep.RECONSTITUTION, ProcessStep.DILUTION],
            )
            self.assertEqual(res.describe(), "Reconstitution + Dilution")

      def test_describe_combination_admixture(self):
            res = self._make(steps=[ProcessStep.COMBINATION])
            self.assertEqual(res.describe(), "Combination Admixture")

      def test_describe_unknown_process(self):
            self.assertEqual(self._make().describe(), "Unknown Process")

      def test_notes_default_to_empty_list(self):
            self.assertEqual(self._make().notes, [])

      def test_to_dict_has_all_keys(self):
            res = ProcessInferenceManager([
                  ("INJECTION, LYOPHILIZED POWDER", "Cefazolin"),
                  ("INJECTION, SOLUTION", "Sodium Chloride"),
            ]).infer()
            self.assertEqual(
                  set(res.to_dict()),
                  {"description", "needs_recon", "needs_dilution", "repack_only",
                   "steps", "roles", "notes"},
            )

      def test_to_dict_serialises_enums_to_strings(self):
            res = ProcessInferenceManager([
                  ("INJECTION, LYOPHILIZED POWDER", "Cefazolin"),
                  ("INJECTION, SOLUTION", "Sodium Chloride"),
            ]).infer()
            d = res.to_dict()
            self.assertEqual(d["steps"], ["reconstitution", "dilution"])
            self.assertEqual(d["roles"], ["lyophilized_powder", "iv_diluent"])
            self.assertTrue(all(isinstance(s, str) for s in d["steps"]))
            self.assertTrue(all(isinstance(r, str) for r in d["roles"]))
            self.assertEqual(d["description"], "Reconstitution + Dilution")


# ===========================================================================
# Alternate constructors
# ===========================================================================

class TestConstructors(unittest.TestCase):
      """from_pipeline_enrichment and from_order_components."""

      def test_from_pipeline_enrichment_infers_correctly(self):
            enrichment = {
                  0: {"dosage_form": "INJECTION, LYOPHILIZED POWDER", "generic": "Cefazolin"},
                  1: {"dosage_form": "INJECTION, SOLUTION", "generic": "Sterile Water for Injection"},
            }
            res = ProcessInferenceManager.from_pipeline_enrichment(enrichment).infer()
            self.assertTrue(res.needs_recon)
            self.assertFalse(res.needs_dilution)
            self.assertEqual(res.roles, [ComponentRole.LYOPHILIZED_POWDER, ComponentRole.SWFI])

      def test_from_pipeline_enrichment_passes_dosage_form(self):
            enrichment = {0: {"dosage_form": "TABLET", "generic": "Acetaminophen"}}
            mgr = ProcessInferenceManager.from_pipeline_enrichment(enrichment, dosage_form="TABLET")
            self.assertEqual(mgr.dosage_form, "TABLET")
            self.assertTrue(mgr.infer().repack_only)

      def test_from_order_components_infers_dilution(self):
            components = [
                  FakeComponent(FakeProduct("INJECTION, CONCENTRATE", "Potassium Chloride")),
                  FakeComponent(FakeProduct("INJECTION, SOLUTION", "Sodium Chloride")),
            ]
            res = ProcessInferenceManager.from_order_components(components).infer()
            self.assertTrue(res.needs_dilution)
            self.assertEqual(res.steps, [ProcessStep.DILUTION])


if __name__ == "__main__":
      unittest.main()
