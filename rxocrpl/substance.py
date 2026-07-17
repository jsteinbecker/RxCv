"""
Substance Layer — bridging dimensions and tracking mixtures
===========================================================

`Quantity` is deliberately substance-blind: it will not convert `mg -> mmol`,
`mmol -> mEq`, or `mg -> units`, because those conversions require knowledge
that lives on the *substance*, not on the number.

This layer supplies that knowledge.

`Substance`
    A named chemical/biological entity carrying the bridge factors that make
    cross-dimensional conversion well-defined:

    * ``molar_mass``   MASS / SUBSTANCE   (e.g. 74.55 g/mol for KCl)
    * ``valence``      CHARGE / SUBSTANCE (e.g. 1 Eq/mol for K+)
    * ``potency``      ACTIVITY / MASS    (e.g. 180 units/mg for heparin)
    * ``density``      MASS / VOLUME      (for liquids/ointment bases)
    * ``standard``     activity reference standard tag

    Only the bridges you supply exist. A `Substance` with no ``molar_mass``
    still cannot go from `mg` to `mmol` — it raises, loudly, rather than
    guessing.

`SubstanceQuantity`
    A `Quantity` bound to a `Substance`. Adding two of them is only legal when
    the substances match. Conversion may now cross dimensions, routing through
    the substance's bridge graph (mass <-> substance <-> charge, mass <->
    activity, mass <-> volume).

`Mixture`
    A bag of `SubstanceQuantity` constituents plus an optional total volume.
    This models the thing pharmacy actually cares about: a bag, a vial, a
    syringe, a TPN — a *container of several things at once*. Mixtures add,
    scale, and can be queried for the amount or concentration of any single
    constituent, in any unit that constituent's bridges permit.

    Total osmolarity, total charge balance, and per-constituent concentrations
    fall out of the constituent list rather than being tracked by hand.

Design rule preserved from the quantity layer: **nothing converts silently
across a bridge it was not given.** A missing molar mass is an error, not a 1.0.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Iterator, Mapping

try:
      from .quantities import (DimensionalityError, Quantity, Dimension, MASS, SUBSTANCE,
                               CHARGE, ACTIVITY, VOLUME, _resolve, _UNITS, define,
                               mg, g, mEq, mmol, units, L)
except ImportError:
      from quantities import (DimensionalityError, Quantity, Dimension, MASS, SUBSTANCE,
                              CHARGE, ACTIVITY, VOLUME, _resolve, _UNITS, define,
                              mg, g, mEq, mmol, units, L)


class BridgeError(DimensionalityError):
      """Raised when a substance lacks the bridge factor a conversion needs."""


class SubstanceMismatch(TypeError):
      """Raised when operations mix different substances."""


def _base_unit_for(dim: Dimension, standard: str | None = None) -> str:
      """The canonical single-dimension unit symbol for `dim`.

      Used to renormalise a quantity after a bridge walk, whose intermediate
      unit symbols are composites. For ACTIVITY, a `standard` tag reproduces the
      standard-scoped unit (``units[heparin]``) so that incommensurable
      activities stay incommensurable.
      """
      if dim == ACTIVITY and standard is not None:
            sym = f"units[{standard}]"
            if sym not in _UNITS:
                  define(sym, ACTIVITY, 1.0, standard=standard)
            return sym
      for d, sym in ((MASS, "g"), (VOLUME, "L"), (SUBSTANCE, "mol"),
                     (CHARGE, "Eq"), (ACTIVITY, "units")):
            if dim == d:
                  return sym
      raise BridgeError(f"no canonical base unit for dimension {dim}")


# --------------------------------------------------------------------------
# Substance
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Substance:
      """A named entity carrying substance-specific bridge factors.

      Every bridge is optional. Supplying one enables exactly the conversions
      that bridge spans; omitting it makes those conversions raise.
      """
      name: str
      molar_mass: Quantity | None = None  # MASS / SUBSTANCE, e.g. g/mol
      valence: Quantity | None = None  # CHARGE / SUBSTANCE, e.g. Eq/mol
      potency: Quantity | None = None  # ACTIVITY / MASS, e.g. units/mg
      density: Quantity | None = None  # MASS / VOLUME, e.g. g/mL
      standard: str | None = None  # activity reference standard
      particles: float | None = None  # osmotic dissociation factor (i)
      salt_of: str | None = None  # base moiety, if this is a salt

      # -- bridge lookup ----------------------------------------------------
      def _factors(self) -> list[tuple[Dimension, Dimension, Quantity | None, str]]:
            """The (a, b, factor, label) rows this substance can bridge across.

            `factor` may be written in either orientation (``g/mol`` or
            ``mol/g``); `_bridge` derives the correct operation from the
            factor's actual dimension rather than assuming one.
            """
            return [
                  (MASS, SUBSTANCE, self.molar_mass, "molar_mass"),
                  (SUBSTANCE, CHARGE, self.valence, "valence"),
                  (MASS, ACTIVITY, self.potency, "potency"),
                  (VOLUME, MASS, self.density, "density"),
            ]

      def _bridge(self, frm: Dimension, to: Dimension) -> tuple[Quantity, int]:
            """Return (factor, exp) with exp=+1 to multiply, -1 to divide.

            The operation is derived from the factor's dimension: multiplying by
            a factor of dimension ``to/frm`` lands on `to`; dividing by a factor
            of dimension ``frm/to`` does the same. This keeps the table immune to
            how the caller chose to orient the units.
            """
            for a, b, factor, label in self._factors():
                  if {frm, to} != {a, b}:
                        continue
                  if factor is None:
                        raise BridgeError(
                              f"{self.name!r} has no {label}: cannot bridge "
                              f"{frm} -> {to}")
                  fdim = factor.dimension
                  if fdim == to / frm:
                        return factor, +1
                  if fdim == frm / to:
                        return factor, -1
                  raise BridgeError(
                        f"{self.name!r} {label} has dimension {fdim}, which cannot "
                        f"bridge {frm} -> {to}")
            raise BridgeError(f"no bridge {frm} -> {to} for {self.name!r}")

      def _path(self, frm: Dimension, to: Dimension) -> list[Dimension]:
            """Breadth-first search over the bridge graph."""
            nodes = [MASS, VOLUME, SUBSTANCE, CHARGE, ACTIVITY]
            edges = {
                  (MASS, SUBSTANCE), (SUBSTANCE, MASS),
                  (SUBSTANCE, CHARGE), (CHARGE, SUBSTANCE),
                  (MASS, ACTIVITY), (ACTIVITY, MASS),
                  (VOLUME, MASS), (MASS, VOLUME),
            }
            if frm not in nodes or to not in nodes:
                  raise BridgeError(f"{frm} -> {to} is not a bridgeable pair")
            queue: list[list[Dimension]] = [[frm]]
            seen = {frm}
            while queue:
                  path = queue.pop(0)
                  if path[-1] == to:
                        return path
                  for nxt in nodes:
                        if (path[-1], nxt) in edges and nxt not in seen:
                              seen.add(nxt)
                              queue.append(path + [nxt])
            raise BridgeError(f"no bridge path {frm} -> {to} for {self.name!r}")

      def convert(self, q: Quantity, target: str) -> Quantity:
            """Convert `q` to `target`, crossing bridges as needed."""
            tu = _resolve(target)
            if q.dimension == tu.dimension:
                  return q.to(target)
            path = self._path(q.dimension, tu.dimension)
            for step_from, step_to in zip(path, path[1:]):
                  factor, exp = self._bridge(step_from, step_to)
                  q = q * factor if exp > 0 else q / factor
            # The walk lands on the right dimension but carries a composite unit
            # symbol (e.g. "mEq/(Eq/mol)"). Renormalise through the base unit so
            # the final `.to()` sees a clean, single-dimension quantity, and so
            # any ACTIVITY standard tag comes from `target` rather than the
            # arithmetic.
            base = _base_unit_for(tu.dimension, self.standard)
            q = Quantity(q.to_base() / _resolve(base).scale, base)
            return q.to(target)

      def has_bridge(self, frm: Dimension, to: Dimension) -> bool:
            try:
                  path = self._path(frm, to)
            except BridgeError:
                  return False
            for a, b in zip(path, path[1:]):
                  try:
                        self._bridge(a, b)
                  except BridgeError:
                        return False
            return True

      def __call__(self, q: Quantity | str) -> "SubstanceQuantity":
            """Sugar: ``KCL(mEq(20))`` or ``KCL("20 mEq")``."""
            if isinstance(q, str):
                  q = Quantity(q)
            return SubstanceQuantity(q.value, q.unit, self)

      def __str__(self) -> str:
            return self.name


# --------------------------------------------------------------------------
# SubstanceQuantity
# --------------------------------------------------------------------------
@dataclass(eq=False)
class SubstanceQuantity(Quantity):
      """A `Quantity` bound to a `Substance`, unlocking cross-dimension conversion."""
      substance: Substance | None = None

      def __init__(self, value, unit=None, substance: Substance | None = None):
            Quantity.__init__(self, value, unit)
            if substance is None:
                  raise ValueError("SubstanceQuantity requires a substance")
            self.substance = substance

      # -- conversion now may cross bridges ---------------------------------
      def to(self, target: str) -> "SubstanceQuantity":
            q = self.substance.convert(Quantity(self.value, self.unit), target)
            return SubstanceQuantity(q.value, q.unit, self.substance)

      def _same(self, other) -> None:
            if isinstance(other, SubstanceQuantity) and other.substance != self.substance:
                  raise SubstanceMismatch(
                        f"cannot combine {self.substance} with {other.substance}")

      def _add_sub(self, other, sign: int):
            self._same(other)
            q = Quantity._add_sub(self, other, sign)
            return SubstanceQuantity(q.value, q.unit, self.substance)

      def __mul__(self, other):
            q = Quantity.__mul__(self, other)
            return (SubstanceQuantity(q.value, q.unit, self.substance)
                    if isinstance(q, Quantity) else q)

      __rmul__ = __mul__

      def __truediv__(self, other):
            if isinstance(other, SubstanceQuantity):
                  self._same(other)
            q = Quantity.__truediv__(self, other)
            return (SubstanceQuantity(q.value, q.unit, self.substance)
                    if isinstance(q, Quantity) else q)

      def _comparable(self, other) -> bool:
            return (Quantity._comparable(self, other)
                    and getattr(other, "substance", self.substance) == self.substance)

      def __hash__(self):
            return hash((Quantity.__hash__(self), self.substance.name))

      # -- introspection -----------------------------------------------------
      def can_convert_to(self, to: Dimension) -> bool:
            """Whether this quantity can reach dimension `to`, without raising."""
            if self.dimension == to:
                  return True
            return self.substance.has_bridge(self.dimension, to)

      # -- convenience views -------------------------------------------------
      @property
      def moles(self) -> "SubstanceQuantity":
            return self.to("mmol")

      @property
      def charge(self) -> "SubstanceQuantity":
            return self.to("mEq")

      @property
      def mass(self) -> "SubstanceQuantity":
            return self.to("mg")

      @property
      def activity(self) -> "SubstanceQuantity":
            return self.to("units")

      @property
      def osmoles(self) -> Quantity:
            """mOsm contributed, via the dissociation factor `particles`."""
            i = self.substance.particles
            if i is None:
                  raise BridgeError(f"{self.substance.name!r} has no `particles` factor")
            if i == 0:
                  # A non-dissociating vehicle (e.g. water) contributes nothing,
                  # so it needs no molar mass to say so.
                  return Quantity(0.0, "mmol")
            return Quantity(self.to("mmol").value * i, "mmol")

      def __str__(self) -> str:
            return f"{self.value:g} {self.unit} {self.substance}"


# --------------------------------------------------------------------------
# Mixture
# --------------------------------------------------------------------------
@dataclass
class Mixture:
      """A container holding several `SubstanceQuantity` constituents.

      `volume` is the total volume of the preparation (bag, vial, syringe). It
      is *not* the sum of the additive volumes unless you say so — diluent is
      implicit. Concentrations are computed against this total.
      """
      name: str = "mixture"
      constituents: list[SubstanceQuantity] = field(default_factory=list)
      volume: Quantity | None = None

      def __post_init__(self) -> None:
            merged: list[SubstanceQuantity] = []
            for c in self.constituents:
                  for m in merged:
                        if m.substance == c.substance:
                              merged[merged.index(m)] = m + c
                              break
                  else:
                        merged.append(c)
            self.constituents = merged
            if self.volume is not None and self.volume.dimension != VOLUME:
                  raise DimensionalityError(f"volume must be VOLUME, got {self.volume.dimension}")

      # -- construction ------------------------------------------------------
      def add(self, *items: SubstanceQuantity) -> "Mixture":
            return Mixture(self.name, self.constituents + list(items), self.volume)

      def in_volume(self, volume: Quantity) -> "Mixture":
            return Mixture(self.name, list(self.constituents), volume)

      def __add__(self, other: "Mixture | SubstanceQuantity") -> "Mixture":
            if isinstance(other, SubstanceQuantity):
                  return self.add(other)
            if not isinstance(other, Mixture):
                  return NotImplemented
            vol = None
            if self.volume is not None and other.volume is not None:
                  vol = self.volume + other.volume
            elif self.volume is not None or other.volume is not None:
                  raise DimensionalityError(
                        "cannot add a mixture with a known volume to one without")
            return Mixture(f"{self.name}+{other.name}",
                           self.constituents + other.constituents, vol)

      def __mul__(self, k: float) -> "Mixture":
            """Scale the whole preparation (e.g. batching x5)."""
            return Mixture(self.name, [c * k for c in self.constituents],
                           self.volume * k if self.volume is not None else None)

      __rmul__ = __mul__

      # -- queries -----------------------------------------------------------
      def __iter__(self) -> Iterator[SubstanceQuantity]:
            return iter(self.constituents)

      def __len__(self) -> int:
            return len(self.constituents)

      def __contains__(self, s: Substance | str) -> bool:
            key = s if isinstance(s, str) else s.name
            return any(c.substance.name == key for c in self.constituents)

      def __getitem__(self, s: Substance | str) -> SubstanceQuantity:
            key = s if isinstance(s, str) else s.name
            for c in self.constituents:
                  if c.substance.name == key:
                        return c
            raise KeyError(f"{key!r} not in {self.name}")

      def amount(self, s: Substance | str, unit: str) -> SubstanceQuantity:
            """Amount of one constituent, in any unit its bridges allow."""
            return self[s].to(unit)

      def _require_volume(self) -> Quantity:
            if self.volume is None:
                  raise DimensionalityError(f"{self.name!r} has no volume set; "
                                            f"use .in_volume(...) first")
            return self.volume

      def concentration(self, s: Substance | str, unit: str = "mg/mL") -> Quantity:
            """Concentration of one constituent against the total volume."""
            num, den = unit.split("/", 1)
            return self[s].to(num) / self._require_volume().to(den)

      def concentrations(self, unit: str = "mg/mL") -> dict[str, Quantity]:
            out = {}
            for c in self.constituents:
                  try:
                        out[c.substance.name] = self.concentration(c.substance, unit)
                  except BridgeError:
                        continue
            return out

      # -- aggregate chemistry ------------------------------------------------
      def total_osmolarity(self, unit: str = "mmol/L") -> Quantity:
            """Sum of osmoles per litre. mOsm/L reads off `mmol/L`.

            A constituent with no `particles` factor is an error, not a zero:
            silently omitting it would under-report osmolarity and could make an
            unsafe preparation look peripherally safe.
            """
            total = Quantity(0, "mmol")
            for c in self.constituents:
                  total = total + c.osmoles
            num, den = unit.split("/", 1)
            return total.to(num) / self._require_volume().to(den)

      def charge_balance(self, unit: str = "mEq") -> Quantity:
            """Signed sum of charge across constituents (valence carries sign).

            `.to("mEq")` yields a *magnitude*: the round-trip through valence
            cancels its sign, so an anion would read positive and the balance
            could never detect an imbalance. The sign is therefore reapplied
            explicitly from the valence itself.
            """
            total = Quantity(0, "mEq")
            for c in self.constituents:
                  if c.substance.valence is None:
                        continue
                  sign = -1.0 if c.substance.valence.value < 0 else 1.0
                  total = total + Quantity(sign * abs(c.to("mEq").value), "mEq")
            return total.to(unit)

      def cation_charge(self, unit: str = "mEq") -> Quantity:
            """Total positive charge only (the usual 'total cations' figure)."""
            total = Quantity(0, "mEq")
            for c in self.constituents:
                  v = c.substance.valence
                  if v is None or v.value < 0:
                        continue
                  total = total + Quantity(abs(c.to("mEq").value), "mEq")
            return total.to(unit)

      def total_mass(self, unit: str = "mg") -> Quantity:
            """Summed mass of every constituent that has a route to MASS.

            Only a genuinely absent mass bridge is skipped; any other bridge
            failure is a real error and is allowed to propagate rather than
            silently under-reporting the mass of the preparation.
            """
            total = Quantity(0, "mg")
            for c in self.constituents:
                  if not c.can_convert_to(MASS):
                        continue
                  total = total + Quantity(c.to("mg").value, "mg")
            return total.to(unit)

      def __str__(self) -> str:
            body = ", ".join(str(c) for c in self.constituents) or "empty"
            vol = f" in {self.volume}" if self.volume is not None else ""
            return f"{self.name}[{body}]{vol}"


# --------------------------------------------------------------------------
# A small starter formulary
# --------------------------------------------------------------------------
KCL = Substance("KCl", molar_mass=Quantity(74.55, "g/mol"),
                valence=Quantity(1, "Eq/mol"), particles=2)
NACL = Substance("NaCl", molar_mass=Quantity(58.44, "g/mol"),
                 valence=Quantity(1, "Eq/mol"), particles=2)
CACL2 = Substance("CaCl2", molar_mass=Quantity(110.98, "g/mol"),
                  valence=Quantity(2, "Eq/mol"), particles=3)
MGSO4 = Substance("MgSO4", molar_mass=Quantity(120.37, "g/mol"),
                  valence=Quantity(2, "Eq/mol"), particles=2)
DEXTROSE = Substance("Dextrose", molar_mass=Quantity(180.16, "g/mol"), particles=1)
# An anion: negative valence, so it subtracts in `charge_balance`.
ACETATE = Substance("Acetate", molar_mass=Quantity(59.04, "g/mol"),
                    valence=Quantity(-1, "Eq/mol"), particles=1)
HEPARIN = Substance("Heparin", potency=Quantity(180, "units/mg"), standard="heparin")
INSULIN = Substance("Insulin", potency=Quantity(28.8, "units/mg"), standard="insulin")
VANCOMYCIN = Substance("Vancomycin", molar_mass=Quantity(1449.3, "g/mol"), particles=1)
STERILE_WATER = Substance("Water", density=Quantity(1.0, "g/mL"), particles=0)

if __name__ == "__main__":
      # ── ANSI palette ──────────────────────────────────────────
      RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
      CYAN, GREEN, YELLOW, RED, MAGENTA, BLUE = (
            "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[35m", "\033[34m")


      def _hdr(title: str) -> None:
            print(f"\n{BOLD}{MAGENTA}{'─' * 62}{RESET}")
            print(f"{BOLD}{MAGENTA}{title}{RESET}")
            print(f"{BOLD}{MAGENTA}{'─' * 62}{RESET}")


      def _show(label: str, q, note: str = "") -> None:
            dim = f"{DIM}[{q.dimension}]{RESET}" if isinstance(q, Quantity) else ""
            tail = f"  {DIM}{note}{RESET}" if note else ""
            print(f"  {CYAN}{label:<28}{RESET} {BOLD}{GREEN}{q}{RESET} {dim}{tail}")


      def expect_error(label: str, fn) -> None:
            try:
                  fn()
                  print(f"  {RED}{BOLD}NO ERROR RAISED{RESET} {DIM}({label}){RESET}")
            except Exception as e:  # noqa: BLE001
                  print(f"  {CYAN}{label:<28}{RESET} {RED}{type(e).__name__}{RESET}"
                        f" {DIM}{e}{RESET}")


      # ---------------------------------------------------------------- 13
      _hdr("13. Substances unlock the bridges Quantity refuses")
      kcl = KCL(mEq(20))
      _show("KCl additive", kcl)
      for u in ("mEq", "mmol", "mg", "g"):
            _show(f"-> {u}", kcl.to(u), "via valence + molar mass")
      _show("as osmoles (i=2)", kcl.osmoles)
      expect_error("bare mEq -> mg", lambda: mEq(20).to("mg"))

      # ---------------------------------------------------------------- 14
      _hdr("14. Multi-hop routing: CHARGE -> SUBSTANCE -> MASS -> ACTIVITY")
      hep = HEPARIN(units(25_000, standard="heparin"))
      _show("heparin activity", hep)
      _show("-> mass", hep.to("mg"), "via potency 180 units/mg")
      expect_error("heparin -> mmol", lambda: hep.to("mmol"))
      expect_error("KCl -> units", lambda: KCL(mg(500)).to("units"))

      # ---------------------------------------------------------------- 15
      _hdr("15. A mixture: TPN-style electrolyte bag")
      tpn = Mixture("TPN", [
            KCL(mEq(30)),
            NACL(mEq(70)),
            CACL2(mEq(10)),
            MGSO4(mEq(16)),
            DEXTROSE(g(125)),
      ]).in_volume(L(1))
      print(f"  {DIM}{tpn}{RESET}\n")
      for c in tpn:
            _show(c.substance.name, c.to("mg"), f"= {c.to('mmol')}")

      # ---------------------------------------------------------------- 16
      _hdr("16. Per-constituent concentrations against total volume")
      for name, q in tpn.concentrations("mg/mL").items():
            print(f"  {CYAN}{name:<28}{RESET} {BOLD}{GREEN}{q.value:>10,.4g} mg/mL{RESET}")
      _show("K+ in mEq/L", tpn.concentration(KCL, "mEq/L"))
      _show("dextrose in g/L", tpn.concentration(DEXTROSE, "g/L"))

      # ---------------------------------------------------------------- 17
      _hdr("17. Aggregate chemistry falls out of the constituent list")
      _show("total osmolarity", tpn.total_osmolarity("mmol/L"), "read as mOsm/L")
      _show("total cation charge", tpn.cation_charge("mEq"))
      _show("signed charge balance", tpn.charge_balance("mEq"), "cations only -> unbalanced")
      balanced = tpn + ACETATE(mEq(126))
      _show("after 126 mEq acetate", balanced.charge_balance("mEq"), "electroneutral")
      _show("total additive mass", tpn.total_mass("g"))
      print(f"  {CYAN}{'peripherally safe?':<28}{RESET} "
            f"{BOLD}{YELLOW}{tpn.total_osmolarity().value < 900}{RESET} {DIM}(<900 mOsm/L){RESET}")

      # ---------------------------------------------------------------- 18
      _hdr("18. Mixture algebra: merge, scale, batch")
      merged = tpn + KCL(mEq(10))
      _show("KCl after top-up", merged[KCL], "auto-merged, not duplicated")
      batch = tpn * 6
      _show("batch x6 volume", batch.volume.to("L"))
      _show("batch x6 dextrose", batch[DEXTROSE].to("g"))
      _show("osmolarity unchanged", batch.total_osmolarity("mmol/L"), "intensive property")
      expect_error("bag + volumeless mix", lambda: tpn + Mixture("premix", [NACL(mEq(5))]))

      # ---------------------------------------------------------------- 19
      _hdr("19. Substance identity is enforced")
      expect_error("KCl + NaCl", lambda: KCL(mEq(10)) + NACL(mEq(10)))
      expect_error("heparin + insulin", lambda: HEPARIN(units(100, standard="heparin"))
                                                + INSULIN(units(100, standard="insulin")))
      _show("KCl + KCl (mixed units)", KCL(mEq(10)) + KCL(mg(745.5)), "converges")

      # ---------------------------------------------------------------- 20
      _hdr("20. Missing bridges raise rather than guess")
      unknown = Substance("MysteryDrug")
      _show("declared", mg(250), f"substance={unknown}")
      expect_error("no molar_mass", lambda: unknown(mg(250)).to("mmol"))
      expect_error("no potency", lambda: unknown(mg(250)).to("units"))
      expect_error("no particles", lambda: unknown(mg(250)).osmoles)
