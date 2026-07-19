"""Unit tests for ``substance.py`` (the Substance / SubstanceQuantity / Mixture layer).

Modeled on the numbered demonstration suite in ``quantities.py`` (blocks 1-12) and
its Substance-layer counterpart (blocks 13-20): each ``TestCase`` here corresponds to
one theme from those demos — intrinsic-vs-bridge conversion, multi-hop routing,
mixtures, per-constituent concentration, aggregate chemistry, mixture algebra,
substance-identity enforcement, and the "missing bridges raise rather than guess"
guarantee.

No external I/O is involved (pure in-process dimensional arithmetic), so nothing is
mocked; the real bridge logic is exercised directly.

Run from the project root so the package-relative import inside substance.py resolves:

    cd <project_root> && PYTHONPATH=. python rxocrpl/substance.test.py -v

(The ``.test.py`` suffix is not matched by unittest's default discovery pattern; invoke
it directly, or use ``--pattern "*.test.py"``.)
"""

import unittest
from rxocrpl.consoleprint import _hdr, _test, _val, _sub, BOLD, DIM, RESET, CYAN, GREEN, YELLOW, RED, MAGENTA
from rxocrpl.quantities import (
      DimensionalityError,
      Quantity,
      MASS,
      SUBSTANCE,
      CHARGE,
      ACTIVITY,
      VOLUME,
      mg,
      g,
      mL,
      mEq,
      mmol,
      units,
      L,
)
from rxocrpl.substance import (
      Substance,
      SubstanceQuantity,
      Mixture,
      BridgeError,
      SubstanceMismatch,
      KCL,
      NACL,
      CACL2,
      MGSO4,
      DEXTROSE,
      ACETATE,
      HEPARIN,
      INSULIN,
      VANCOMYCIN,
      STERILE_WATER,
)


class TestSubstanceConversions(unittest.TestCase):
      """Block 13: substances unlock the bridges Quantity refuses (mEq <-> mmol <-> mg)."""

      def setUp(self):
            _hdr("TestSubstanceConversions KCl additive at 20 mEq")
            self.kcl = KCL(mEq(20))

      def test_kcl_bridges_charge_substance_mass(self):
            _test("test_kcl_bridges_charge_substance_mass", "20 mEq across every axis")
            cases = {
                  "mEq": 20.0,
                  "mmol": 20.0,  # valence 1 Eq/mol
                  "mg": 1491.0,  # 20 mmol * 74.55 g/mol
                  "g": 1.491,
            }
            for unit, expected in cases.items():
                  with self.subTest(unit=unit):
                        got = self.kcl.to(unit).value
                        _sub(f"subTest unit={unit!r} expected={expected} got={got}")
                        self.assertAlmostEqual(got, expected, places=6)

      def test_to_returns_substance_quantity_carrying_substance(self):
            _test("test_to_returns_substance_quantity_carrying_substance")
            result = self.kcl.to("mg")
            _val(f"result={result!r} substance={result.substance}")
            self.assertIsInstance(result, SubstanceQuantity)
            self.assertEqual(result.substance, KCL)

      def test_convenience_views(self):
            _test("test_convenience_views", ".moles/.charge/.mass properties")
            _val(
                  f"moles={self.kcl.moles.value} charge={self.kcl.charge.value} mass={self.kcl.mass.value}"
            )
            self.assertAlmostEqual(self.kcl.moles.value, 20.0, places=6)
            self.assertAlmostEqual(self.kcl.charge.value, 20.0, places=6)
            self.assertAlmostEqual(self.kcl.mass.value, 1491.0, places=6)

      def test_osmoles_uses_dissociation_factor(self):
            _test("test_osmoles_uses_dissociation_factor", "i=2 for KCl")
            osm = self.kcl.osmoles
            _val(f"osmoles={osm.value} unit={osm.unit}")
            self.assertAlmostEqual(osm.value, 40.0, places=6)
            self.assertEqual(osm.unit, "mmol")

      def test_call_accepts_string_quantity(self):
            _test("test_call_accepts_string_quantity", "KCL('20 mEq')")
            from_str = KCL("20 mEq")
            _val(f"from_str={from_str!r}")
            self.assertAlmostEqual(from_str.to("mmol").value, 20.0, places=6)
            self.assertEqual(from_str.substance, KCL)

      def test_bare_quantity_still_cannot_bridge(self):
            _test("test_bare_quantity_still_cannot_bridge", "mEq(20).to('mg') raises")
            with self.assertRaises(DimensionalityError):
                  mEq(20).to("mg")


class TestMultiHopRouting(unittest.TestCase):
      """Block 14: multi-hop bridge routing and activity-standard scoping."""

      def setUp(self):
            _hdr("TestMultiHopRouting Heparin 25,000 units (standard='heparin')")
            self.hep = HEPARIN(units(25_000, standard="heparin"))

      def test_activity_to_mass_via_potency(self):
            _test("test_activity_to_mass_via_potency", "25000 u / 180 u/mg")
            result = self.hep.to("mg")
            _val(f"result={result.value} mg")
            self.assertAlmostEqual(result.value, 25_000 / 180.0, places=6)

      def test_heparin_cannot_reach_substance_axis(self):
            _test("test_heparin_cannot_reach_substance_axis", "no molar mass")
            with self.assertRaises(BridgeError):
                  self.hep.to("mmol")

      def test_mass_substance_drug_cannot_reach_activity(self):
            _test("test_mass_substance_drug_cannot_reach_activity", "KCl -> units raises")
            with self.assertRaises(BridgeError):
                  KCL(mg(500)).to("units")

      def test_density_bridges_volume_to_mass(self):
            _test("test_density_bridges_volume_to_mass", "1.2 g/mL base")
            base = Substance("Base", density=Quantity(1.2, "g/mL"))
            result = base(mL(10)).to("g")
            _val(f"10 mL -> {result.value} g")
            self.assertAlmostEqual(result.value, 12.0, places=6)


class TestBridgeIntrospection(unittest.TestCase):
      """has_bridge / can_convert_to report reachability without raising."""

      def test_has_bridge_true_and_false(self):
            print()
            _test("test_has_bridge_true_and_false")
            cases = [
                  (KCL, MASS, SUBSTANCE, True),
                  (KCL, MASS, CHARGE, True),
                  (KCL, MASS, ACTIVITY, False),
                  (HEPARIN, MASS, ACTIVITY, True),
                  (HEPARIN, MASS, SUBSTANCE, False),
            ]
            for subst, frm, to, expected in cases:
                  with self.subTest(substance=subst.name, frm=str(frm), to=str(to)):
                        got = subst.has_bridge(frm, to)
                        _sub(f"subTest {subst.name} {frm}->{to} expected={expected} got={got}")
                        self.assertEqual(got, expected)

      def test_can_convert_to_matches_reachability(self):
            _test("test_can_convert_to_matches_reachability")
            kcl = KCL(mEq(20))
            _val(
                  f"->MASS={kcl.can_convert_to(MASS)} ->ACTIVITY={kcl.can_convert_to(ACTIVITY)}"
            )
            self.assertTrue(kcl.can_convert_to(MASS))
            self.assertTrue(kcl.can_convert_to(CHARGE))
            self.assertFalse(kcl.can_convert_to(ACTIVITY))

      def test_can_convert_to_same_dimension_is_true(self):
            _test("test_can_convert_to_same_dimension_is_true")
            self.assertTrue(KCL(mEq(20)).can_convert_to(CHARGE))


class TestSubstanceQuantityArithmetic(unittest.TestCase):
      """Adding, scaling, and dividing SubstanceQuantities; substance identity in ops."""

      def test_add_same_substance_mixed_units_converges(self):
            print(
                  "\n  \u2192 test_add_same_substance_mixed_units_converges: 10 mEq + 745.5 mg KCl"
            )
            total = KCL(mEq(10)) + KCL(mg(745.5))
            _val(f"total={total.to('mEq').value} mEq")
            self.assertAlmostEqual(total.to("mEq").value, 20.0, places=4)

      def test_scalar_multiply_preserves_substance(self):
            _test("test_scalar_multiply_preserves_substance")
            doubled = KCL(mEq(10)) * 2
            _val(f"doubled={doubled!r}")
            self.assertIsInstance(doubled, SubstanceQuantity)
            self.assertAlmostEqual(doubled.to("mEq").value, 20.0, places=6)
            self.assertEqual(doubled.substance, KCL)

      def test_add_different_substance_raises(self):
            _test("test_add_different_substance_raises", "KCl + NaCl")
            with self.assertRaises(SubstanceMismatch):
                  KCL(mEq(10)) + NACL(mEq(10))

      def test_constructor_requires_substance(self):
            _test("test_constructor_requires_substance", "None -> ValueError")
            with self.assertRaisesRegex(ValueError, "requires a substance"):
                  SubstanceQuantity(5, "mg")

      def test_hash_distinguishes_substance(self):
            _test("test_hash_distinguishes_substance")
            h_kcl = hash(KCL(mEq(20)))
            h_nacl = hash(NACL(mEq(20)))
            _val(f"hash(KCl)={h_kcl} hash(NaCl)={h_nacl}")
            self.assertNotEqual(h_kcl, h_nacl)


class TestMixtureConstruction(unittest.TestCase):
      """Block 15: a TPN-style bag; construction merges duplicate substances."""

      def setUp(self):
            _hdr("TestMixtureConstruction 1 L TPN electrolyte bag")
            self.tpn = Mixture(
                  "TPN",
                  [
                        KCL(mEq(30)),
                        NACL(mEq(70)),
                        CACL2(mEq(10)),
                        MGSO4(mEq(16)),
                        DEXTROSE(g(125)),
                  ],
            ).in_volume(L(1))

      def test_membership_and_length(self):
            _test("test_membership_and_length")
            _val(f"len={len(self.tpn)} contains KCl={'KCl' in self.tpn}")
            self.assertEqual(len(self.tpn), 5)
            self.assertIn("KCl", self.tpn)
            self.assertIn(KCL, self.tpn)
            self.assertNotIn("Insulin", self.tpn)

      def test_getitem_by_name_and_substance(self):
            _test("test_getitem_by_name_and_substance")
            by_str = self.tpn["KCl"]
            by_obj = self.tpn[KCL]
            _val(f"by_str={by_str!r} by_obj={by_obj!r}")
            self.assertEqual(by_str.substance, KCL)
            self.assertEqual(by_obj.substance, KCL)

      def test_getitem_missing_raises_keyerror(self):
            _test("test_getitem_missing_raises_keyerror")
            with self.assertRaises(KeyError):
                  self.tpn[HEPARIN]

      def test_duplicate_substances_merge_on_construction(self):
            _test("test_duplicate_substances_merge_on_construction", "10 + 5 mEq KCl")
            dup = Mixture("d", [KCL(mEq(10)), KCL(mEq(5))])
            _val(f"len={len(dup)} value={dup[KCL].to('mEq').value} mEq")
            self.assertEqual(len(dup), 1)
            self.assertAlmostEqual(dup[KCL].to("mEq").value, 15.0, places=6)

      def test_amount_converts_constituent(self):
            _test("test_amount_converts_constituent", "KCl in mmol")
            amt = self.tpn.amount(KCL, "mmol")
            _val(f"amount={amt.value} mmol")
            self.assertAlmostEqual(amt.value, 30.0, places=6)

      def test_non_volume_volume_rejected(self):
            _test("test_non_volume_volume_rejected", "volume=mg raises")
            with self.assertRaises(DimensionalityError):
                  Mixture("bad", [NACL(mEq(5))], mg(5))


class TestConcentrations(unittest.TestCase):
      """Block 16: per-constituent concentration against total volume."""

      def setUp(self):
            _hdr("TestConcentrations 1 L TPN")
            self.tpn = Mixture(
                  "TPN",
                  [
                        KCL(mEq(30)),
                        NACL(mEq(70)),
                        CACL2(mEq(10)),
                        MGSO4(mEq(16)),
                        DEXTROSE(g(125)),
                  ],
            ).in_volume(L(1))

      def test_charge_concentration(self):
            _test("test_charge_concentration", "K+ in mEq/L")
            conc = self.tpn.concentration(KCL, "mEq/L")
            _val(f"{conc.value} mEq/L")
            self.assertAlmostEqual(conc.value, 30.0, places=6)

      def test_mass_concentration(self):
            _test("test_mass_concentration", "dextrose in g/L")
            conc = self.tpn.concentration(DEXTROSE, "g/L")
            _val(f"{conc.value} g/L")
            self.assertAlmostEqual(conc.value, 125.0, places=6)

      def test_concentrations_skips_unbridgeable(self):
            _test("test_concentrations_skips_unbridgeable in mg/mL")
            out = self.tpn.concentrations("mg/mL")
            _val(f"keys={sorted(out)}")
            # Every constituent here has a mass bridge, so all five appear.
            self.assertEqual(set(out), {"KCl", "NaCl", "CaCl2", "MgSO4", "Dextrose"})

      def test_concentration_without_volume_raises(self):
            _test("test_concentration_without_volume_raises")
            volumeless = Mixture("m", [KCL(mEq(30))])
            with self.assertRaisesRegex(DimensionalityError, "no volume"):
                  volumeless.concentration(KCL)


class TestAggregateChemistry(unittest.TestCase):
      """Block 17: osmolarity, cation charge, signed balance, and total mass."""

      def setUp(self):
            _hdr("TestAggregateChemistry 1 L TPN")
            self.tpn = Mixture(
                  "TPN",
                  [
                        KCL(mEq(30)),
                        NACL(mEq(70)),
                        CACL2(mEq(10)),
                        MGSO4(mEq(16)),
                        DEXTROSE(g(125)),
                  ],
            ).in_volume(L(1))

      def test_total_osmolarity(self):
            _test("test_total_osmolarity", "mmol/L read as mOsm/L")
            osm = self.tpn.total_osmolarity("mmol/L")
            _val(f"osmolarity={osm.value} mmol/L")
            self.assertAlmostEqual(osm.value, 924.8277087033748, places=4)

      def test_cation_charge_counts_positive_only(self):
            _test("test_cation_charge_counts_positive_only")
            cations = self.tpn.cation_charge("mEq")
            _val(f"cations={cations.value} mEq")
            self.assertAlmostEqual(cations.value, 126.0, places=6)

      def test_charge_balance_cations_only_is_unbalanced(self):
            _test("test_charge_balance_cations_only_is_unbalanced")
            bal = self.tpn.charge_balance("mEq")
            _val(f"balance={bal.value} mEq")
            self.assertAlmostEqual(bal.value, 126.0, places=6)

      def test_charge_balance_neutralized_by_acetate(self):
            _test("test_charge_balance_neutralized_by_acetate", "+126 mEq acetate anion")
            balanced = self.tpn + ACETATE(mEq(126))
            bal = balanced.charge_balance("mEq")
            _val(f"balance after acetate={bal.value} mEq")
            self.assertAlmostEqual(bal.value, 0.0, places=6)

      def test_total_mass(self):
            _test("test_total_mass in g")
            total = self.tpn.total_mass("g")
            _val(f"total mass={total.value} g")
            self.assertAlmostEqual(total.value, 132.84516, places=5)

      def test_osmolarity_requires_particles_factor(self):
            _test("test_osmolarity_requires_particles_factor", "missing -> raises")
            no_i = Substance("NoI", molar_mass=Quantity(100, "g/mol"))
            mix = Mixture("m", [no_i(g(1))]).in_volume(L(1))
            with self.assertRaises(BridgeError):
                  mix.total_osmolarity("mmol/L")

      def test_water_contributes_zero_osmoles(self):
            _test("test_water_contributes_zero_osmoles", "i=0 vehicle")
            osm = STERILE_WATER(mL(10)).osmoles
            _val(f"water osmoles={osm.value}")
            self.assertEqual(osm.value, 0.0)


class TestMixtureAlgebra(unittest.TestCase):
      """Block 18: merge, scale, and batch operations on mixtures."""

      def setUp(self):
            _hdr("TestMixtureAlgebra 1 L TPN")
            self.tpn = Mixture(
                  "TPN",
                  [
                        KCL(mEq(30)),
                        NACL(mEq(70)),
                        CACL2(mEq(10)),
                        MGSO4(mEq(16)),
                        DEXTROSE(g(125)),
                  ],
            ).in_volume(L(1))

      def test_add_constituent_merges_not_duplicates(self):
            _test("test_add_constituent_merges_not_duplicates", "+10 mEq KCl")
            merged = self.tpn + KCL(mEq(10))
            _val(f"KCl now {merged[KCL].to('mEq').value} mEq, len={len(merged)}")
            self.assertAlmostEqual(merged[KCL].to("mEq").value, 40.0, places=6)
            self.assertEqual(len(merged), 5)

      def test_batch_scales_amounts_and_volume(self):
            _test("test_batch_scales_amounts_and_volume", "x6")
            batch = self.tpn * 6
            self.assertIsNotNone(batch.volume)
            vol = batch.volume
            assert vol is not None  # narrows Quantity | None for type checkers
            _val(f"vol={vol.to('L').value} L, dextrose={batch[DEXTROSE].to('g').value} g")
            self.assertAlmostEqual(vol.to("L").value, 6.0, places=6)
            self.assertAlmostEqual(batch[DEXTROSE].to("g").value, 750.0, places=6)

      def test_osmolarity_is_intensive_under_batching(self):
            _test("test_osmolarity_is_intensive_under_batching")
            base = self.tpn.total_osmolarity("mmol/L").value
            batched = (self.tpn * 6).total_osmolarity("mmol/L").value
            _val(f"base={base} batched={batched}")
            self.assertAlmostEqual(base, batched, places=6)

      def test_rmul_matches_mul(self):
            _test("test_rmul_matches_mul", "6 * tpn == tpn * 6")
            self.assertAlmostEqual(
                  (6 * self.tpn)[DEXTROSE].to("g").value,
                  (self.tpn * 6)[DEXTROSE].to("g").value,
                  places=6,
            )

      def test_add_two_mixtures_sums_volumes(self):
            _test("test_add_two_mixtures_sums_volumes")
            other = Mixture("premix", [NACL(mEq(5))]).in_volume(L(1))
            combined = self.tpn + other
            self.assertIsNotNone(combined.volume)
            vol = combined.volume
            assert vol is not None  # narrows Quantity | None for type checkers
            _val(f"combined volume={vol.to('L').value} L")
            self.assertAlmostEqual(vol.to("L").value, 2.0, places=6)
            # NaCl present in both -> merged.
            self.assertAlmostEqual(combined[NACL].to("mEq").value, 75.0, places=6)

      def test_add_bag_to_volumeless_raises(self):
            _test("test_add_bag_to_volumeless_raises")
            with self.assertRaisesRegex(DimensionalityError, "known volume"):
                  self.tpn + Mixture("premix", [NACL(mEq(5))])


class TestSubstanceIdentityEnforced(unittest.TestCase):
      """Block 19: substance identity and activity standards are enforced."""

      def test_kcl_plus_nacl_rejected(self):
            print()
            _test("test_kcl_plus_nacl_rejected")
            with self.assertRaises(SubstanceMismatch):
                  KCL(mEq(10)) + NACL(mEq(10))

      def test_heparin_plus_insulin_rejected(self):
            _test("test_heparin_plus_insulin_rejected", "distinct standards")
            with self.assertRaises(SubstanceMismatch):
                  HEPARIN(units(100, standard="heparin")) + INSULIN(
                        units(100, standard="insulin")
                  )

      def test_same_substance_mixed_units_allowed(self):
            _test("test_same_substance_mixed_units_allowed")
            total = KCL(mEq(10)) + KCL(mg(745.5))
            _val(f"total={total.to('mEq').value} mEq")
            self.assertAlmostEqual(total.to("mEq").value, 20.0, places=4)


class TestMissingBridgesRaise(unittest.TestCase):
      """Block 20: missing bridges raise rather than silently guessing a factor of 1."""

      def setUp(self):
            _hdr("TestMissingBridgesRaise Substance with no bridge factors")
            self.unknown = Substance("MysteryDrug")

      def test_no_molar_mass_blocks_mass_to_substance(self):
            _test("test_no_molar_mass_blocks_mass_to_substance")
            with self.assertRaises(BridgeError):
                  self.unknown(mg(250)).to("mmol")

      def test_no_potency_blocks_mass_to_activity(self):
            _test("test_no_potency_blocks_mass_to_activity")
            with self.assertRaises(BridgeError):
                  self.unknown(mg(250)).to("units")

      def test_no_particles_blocks_osmoles(self):
            _test("test_no_particles_blocks_osmoles")
            with self.assertRaises(BridgeError):
                  self.unknown(mg(250)).osmoles

      def test_bridge_error_is_dimensionality_error(self):
            _test("test_bridge_error_is_dimensionality_error", "subclass relationship")
            self.assertTrue(issubclass(BridgeError, DimensionalityError))

      def test_partial_bridges_only_unlock_their_span(self):
            _test("test_partial_bridges_only_unlock_their_span")
            # Vancomycin has molar mass (mass<->substance) but no potency (no activity).
            vanc = VANCOMYCIN(mg(1449.3))
            _val(f"vanc -> mmol = {vanc.to('mmol').value}")
            self.assertAlmostEqual(vanc.to("mmol").value, 1.0, places=4)
            with self.assertRaises(BridgeError):
                  vanc.to("units")


if __name__ == "__main__":
      print(f"\n{BOLD}{MAGENTA}{'─' * 62}{RESET}")
      print(f"{BOLD}{MAGENTA}Substance layer test suite{RESET}")
      print(f"{BOLD}{MAGENTA}{'─' * 62}{RESET}")

      result = unittest.main(verbosity=2, exit=False).result

      passed = result.testsRun - len(result.failures) - len(result.errors)
      print(f"\n{BOLD}{MAGENTA}{'─' * 62}{RESET}")
      print(
            f"  {CYAN}{'ran':<10}{RESET}{BOLD}{result.testsRun}{RESET}   "
            f"{CYAN}{'passed':<10}{RESET}{BOLD}{GREEN}{passed}{RESET}   "
            f"{CYAN}{'failed':<10}{RESET}{BOLD}{RED}{len(result.failures)}{RESET}   "
            f"{CYAN}{'errors':<10}{RESET}{BOLD}{RED}{len(result.errors)}{RESET}"
      )
      if result.wasSuccessful():
            print(
                  f"  {BOLD}{GREEN}ALL GREEN{RESET} {DIM}(every bridge, mixture, and guardrail holds){RESET}"
            )
      else:
            print(
                  f"  {BOLD}{RED}FAILURES PRESENT{RESET} {DIM}(see tracebacks above){RESET}"
            )
      print(f"{BOLD}{MAGENTA}{'─' * 62}{RESET}")

      raise SystemExit(0 if result.wasSuccessful() else 1)
