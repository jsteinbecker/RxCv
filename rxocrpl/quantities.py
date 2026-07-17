"""
Dimensional Quantity System for Pharmacy Modeling
=================================================

Quantities are represented as vectors over a deliberately small set of base dimensions chosen for compounding workflows,
rather than the full seven SI base dimensions. Most SI dimensions do not appear directly in pharmacy calculations, so
this system uses **pharmacy-native dimensions** instead.

---

Base Dimensions
---------------

The following pharmacy-native dimensions are supported.


| Dimension | Base Unit | Supported Units | Implementation Notes |
| --- | --- | --- | --- |
| **`MASS`** | gram | `mcg`, `mg`, `g`, `kg` |  |
| **`VOLUME`** | litre | `mL`, `L` | Treated as a first-class pharmacy dimension, not as `L**3` or as a derived length-cubed dimension. |
| **`SUBSTANCE`** | mole | `mmol`, `mol` |  |
| **`CHARGE`** | equivalent | `mEq`, `Eq` | Represents moles of ionic charge, *not* electrical charge in coulombs. |
| **`TIME`** | second | `s`, `min`, `hr`, `day` |  |
| **`COUNT`** | each | *(Discrete)* | Used for countable pharmacy objects (e.g., dosage units, packages, containers, vials, tablets, capsules, syringes, bags). |
| **`ACTIVITY`** | unit | *(Variable)* | Used for USP Units, International Units, and other biological activity units tied to a reference standard. |


## Design Rules

### Intrinsic Conversions

Conversions within a single dimension are **intrinsic** to the quantity system. They operate universally and do not
require substance-specific knowledge.

**Examples:**

* `mg` to `g`
* `mcg` to `mg`
* `mL` to `L`
* `hr` to `s`

---

### Bridge Conversions

Conversions between different dimensions are **not automatic**.

**Examples:**

* `mg` to `mmol`
* `mmol` to `mEq`
* `mg` to `Units`

> **Architectural Rule:** Bridge conversions require substance-specific bridge factors. These factors belong strictly to
the `Substance` or bridge-conversion layer, *not* to the generic `Quantity` type.

**Common Bridge Factors Include:**

* Molar mass
* Valence
* Salt form
* Hydration state
* Concentration basis
* Specific activity
* Biological reference standard

---

### Activity Standards

Biological activity units are not universally commensurable.

**Examples of Incompatibility:**

* `1 IU` of heparin is **not equivalent** to `1 IU` of insulin.
* `1 USP Unit` of one drug is **not comparable** to `1 USP Unit` of another drug.

> **Important:** For this reason, quantities on the `ACTIVITY` axis may carry an optional `standard` tag identifying the
biological reference standard. Activities may *only* be added, compared, or converted when their standards are compatible.
Combining activities with different standards is an error, even though they share the same `ACTIVITY` dimension.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Union


class Base(IntEnum):
      MASS = 0  # gram
      VOLUME = 1  # litre
      SUBSTANCE = 2  # mole
      CHARGE = 3  # equivalent (mole of charge)
      TIME = 4  # second
      COUNT = 5  # each
      ACTIVITY = 6  # unit (biological activity)


_NDIM = len(Base)
_SYMBOLS = {
      Base.MASS: "M", Base.VOLUME: "V", Base.SUBSTANCE: "N",
      Base.CHARGE: "Q", Base.TIME: "T", Base.COUNT: "#", Base.ACTIVITY: "A",
}


class DimensionalityError(TypeError):
      """Raised when an operation mixes incompatible dimensions."""


@dataclass(frozen=True)
class Dimension:
      """An exponent vector over the base dimensions, e.g. mass/volume."""
      exponents: tuple[int, ...] = (0,) * _NDIM

      def __post_init__(self) -> None:
            if len(self.exponents) != _NDIM:
                  raise ValueError(f"expected {_NDIM} exponents, got {len(self.exponents)}")

      def __mul__(self, other: "Dimension") -> "Dimension":
            return Dimension(tuple(a + b for a, b in zip(self.exponents, other.exponents)))

      def __truediv__(self, other: "Dimension") -> "Dimension":
            return Dimension(tuple(a - b for a, b in zip(self.exponents, other.exponents)))

      def __pow__(self, n: int) -> "Dimension":
            return Dimension(tuple(a * n for a in self.exponents))

      @property
      def is_dimensionless(self) -> bool:
            return all(e == 0 for e in self.exponents)

      def __str__(self) -> str:
            if self.is_dimensionless:
                  return "1"
            num, den = [], []
            for b in Base:
                  e = self.exponents[b]
                  if e == 0:
                        continue
                  tok = _SYMBOLS[b] + (f"^{abs(e)}" if abs(e) != 1 else "")
                  (num if e > 0 else den).append(tok)
            s = "\u00b7".join(num) or "1"
            if den:
                  s += "/" + "\u00b7".join(den)
            return s


def _basis(b: Base) -> Dimension:
      exps = [0] * _NDIM
      exps[b] = 1
      return Dimension(tuple(exps))


DIMENSIONLESS = Dimension()
MASS = _basis(Base.MASS)
VOLUME = _basis(Base.VOLUME)
SUBSTANCE = _basis(Base.SUBSTANCE)
CHARGE = _basis(Base.CHARGE)
TIME = _basis(Base.TIME)
COUNT = _basis(Base.COUNT)
ACTIVITY = _basis(Base.ACTIVITY)


@dataclass
class Unit:
      """A named scale on a dimension. ``scale`` converts a magnitude to the base
      unit of its dimension (e.g., mg -> g uses scale 1e-3).

      ``standard`` tags the biological reference standard for ACTIVITY units so
      that incommensurable Units (heparin vs. insulin IU) cannot be combined.
      """
      symbol: str
      dimension: Dimension
      scale: float
      standard: str | None = None


_UNITS: dict[str, Unit] = {}
_ALIASES: dict[str, str] = {}


def _register(u: Unit) -> Unit:
      _UNITS[u.symbol] = u
      return u


def define(symbol: str, dimension: Dimension, scale: float,
           standard: str | None = None, *, aliases: tuple[str, ...] = ()) -> Unit:
      u = _register(Unit(symbol, dimension, scale, standard))
      for a in aliases:
            _ALIASES[a] = symbol
      return u


# --- base + prefixed units -------------------------------------------------
define("g", MASS, 1.0)
define("mg", MASS, 1e-3)
define("mcg", MASS, 1e-6, aliases=("ug", "\u00b5g"))
define("ng", MASS, 1e-9)
define("kg", MASS, 1e3)

define("L", VOLUME, 1.0, aliases=("l",))
define("mL", VOLUME, 1e-3, aliases=("ml", "ML", "cc"))

define("mol", SUBSTANCE, 1.0)
define("mmol", SUBSTANCE, 1e-3)

define("Eq", CHARGE, 1.0)
define("mEq", CHARGE, 1e-3)

define("s", TIME, 1.0, aliases=("sec",))
define("min", TIME, 60.0)
define("hr", TIME, 3600.0, aliases=("h", "hour"))
define("day", TIME, 86400.0)

define("each", COUNT, 1.0, aliases=("ea", "unit_count"))

# Generic biological activity (standard unspecified). Per-drug IU should be
# registered with their own standard tag, e.g. define("IU", ACTIVITY, 1.0,
# standard="heparin"), which then will not combine with a different standard.
define("units", ACTIVITY, 1.0, aliases=("u", "U", "unit"))

# Dimensionless. NB: pharmacy "%" is context-dependent (w/v == g/100 mL,
# w/w, v/v); it is registered as a bare ratio and must NOT be silently
# coerced to a concentration without knowing which percent is meant.
define("%", DIMENSIONLESS, 0.01)
define("", DIMENSIONLESS, 1.0, aliases=("1",))


def _resolve(symbol: str) -> Unit:
      """
      Resolves a given unit symbol to a corresponding unit definition. This function searches
      for the symbol in predefined sets of units and aliases. If the symbol corresponds to
      a simple composite unit in the form "a/b", it will be parsed, and the resulting
      composite unit will be registered and returned. If the symbol cannot be resolved,
      an exception is raised.

      :param symbol: The unit symbol to resolve. It can be a predefined symbol, an alias,
          or a composite unit in the form "a/b".
      :type symbol: str
      :return: The resolved unit object corresponding to the given symbol.
      :rtype: Unit
      :raises KeyError: If the symbol cannot be resolved to a known unit or alias.
      """
      if symbol in _UNITS:
            return _UNITS[symbol]
      if symbol in _ALIASES:
            return _UNITS[_ALIASES[symbol]]
      # Best-effort parse of a simple composite "a/b" produced elsewhere.
      if "/" in symbol:
            top, bot = symbol.split("/", 1)
            tu, bu = _resolve(top.strip()), _resolve(bot.strip())
            return _register(Unit(symbol, tu.dimension / bu.dimension,
                                  tu.scale / bu.scale, tu.standard or bu.standard))
      raise KeyError(f"unknown unit {symbol!r}")


def _composite(a: Unit, b: Unit, op: str) -> Unit:
      sym = f"{a.symbol}{op}{b.symbol}"
      if sym in _UNITS:
            return _UNITS[sym]
      dim = a.dimension * b.dimension if op == "\u00b7" else a.dimension / b.dimension
      scale = a.scale * b.scale if op == "\u00b7" else a.scale / b.scale
      std = a.standard or b.standard
      return _register(Unit(sym, dim, scale, std))


@dataclass(eq=False)
class Quantity:
      """A magnitude in a given unit. Stored as ``(value, unit-symbol)`` so it
      stays JSON/asdict-friendly; dimensional behaviour comes from the registry.
      """
      value: float | int
      unit: str

      # -- introspection ----------------------------------------------------
      @property
      def _u(self) -> Unit:
            return _resolve(self.unit)

      @property
      def dimension(self) -> Dimension:
            return self._u.dimension

      @property
      def standard(self) -> str | None:
            return self._u.standard

      def to_base(self) -> float:
            return self.value * self._u.scale

      # -- conversion -------------------------------------------------------
      def to(self, target: str) -> "Quantity":
            tu = _resolve(target)
            su = self._u
            if su.dimension != tu.dimension:
                  raise DimensionalityError(
                        f"cannot convert {su.dimension} -> {tu.dimension} "
                        f"({self.unit} -> {target}); needs a substance bridge")
            self._check_standard(su, tu)
            return Quantity(self.to_base() / tu.scale, tu.symbol)

      @staticmethod
      def _check_standard(a: Unit, b: Unit) -> None:
            if a.dimension.exponents[Base.ACTIVITY] != 0:
                  if a.standard != b.standard:
                        raise DimensionalityError(
                              f"incommensurable activity standards: "
                              f"{a.standard!r} vs {b.standard!r}")

      # -- additive (same dimension) ---------------------------------------
      def _add_sub(self, other: "Quantity", sign: int) -> "Quantity":
            if not isinstance(other, Quantity):
                  return NotImplemented
            o = other.to(self.unit)  # raises if dimensions/standards differ
            return Quantity(self.value + sign * o.value, self.unit)

      def __add__(self, other):
            return self._add_sub(other, +1)

      def __sub__(self, other):
            return self._add_sub(other, -1)

      # -- multiplicative ---------------------------------------------------
      def __mul__(self, other):
            if isinstance(other, (int, float)):
                  return Quantity(self.value * other, self.unit)
            if isinstance(other, Quantity):
                  u = _composite(self._u, other._u, "\u00b7")
                  return Quantity(self.value * other.value, u.symbol)
            return NotImplemented

      __rmul__ = __mul__

      def __truediv__(self, other):
            if isinstance(other, (int, float)):
                  return Quantity(self.value / other, self.unit)
            if isinstance(other, Quantity):
                  u = _composite(self._u, other._u, "/")
                  q = Quantity(self.value / other.value, u.symbol)
                  return q
            return NotImplemented

      def __rtruediv__(self, other):
            if isinstance(other, (int, float)):
                  inv = _composite(_resolve(""), self._u, "/")
                  return Quantity(other / self.value, inv.symbol)
            return NotImplemented

      # -- ordering / equality ---------------------------------------------
      def _comparable(self, other: "Quantity") -> bool:
            return (isinstance(other, Quantity)
                    and self.dimension == other.dimension
                    and self.standard == other.standard)

      def __eq__(self, other):
            if not self._comparable(other):
                  return NotImplemented
            return abs(self.to_base() - other.to_base()) < 1e-12

      def __lt__(self, other):
            if not self._comparable(other):
                  return NotImplemented
            return self.to_base() < other.to_base()

      def __le__(self, other):
            return self < other or self == other

      def __gt__(self, other):
            return not self <= other

      def __ge__(self, other):
            return not self < other

      def __round__(self, ndigits: int = 0) -> Quantity:
            return Quantity(round(self.value, ndigits), self.unit)

      def __hash__(self):
            return hash((round(self.to_base(), 12), self.dimension, self.standard))

      def __str__(self) -> str:
            return f"{self.value} {self.unit}".rstrip()

      def __init__(self, value: float | int | str, unit: str | Unit = None):
            if isinstance(value, str):
                  # Parse a string like "5.5mg" or "7000 mcg" or "2 mg / mL" into a Quantity.
                  mag_regex = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(.+)\s*$")
                  match = mag_regex.match(value)
                  if not match:
                        raise ValueError(f"Invalid quantity string: {value!r}")
                  mag, unit = match.groups()
                  mag = float(mag)
                  unit = _resolve(unit.strip()).symbol
            else:
                  mag = float(value)
                  if isinstance(unit, Unit):
                        unit = unit.symbol
            self.value = mag
            self.unit = unit


# Backwards-compatible alias for existing imports/annotations.
PhysicalQuantity = Quantity


# --- ergonomic constructors ------------------------------------------------
def mcg(v): return Quantity(v, "mcg")


def mg(v): return Quantity(v, "mg")


def g(v): return Quantity(v, "g")


def kg(v): return Quantity(v, "kg")


def mL(v): return Quantity(v, "mL")


def L(v): return Quantity(v, "L")


def mmol(v): return Quantity(v, "mmol")


def mol(v): return Quantity(v, "mol")


def mEq(v): return Quantity(v, "mEq")


def units(v, standard=None):
      if standard is None:
            return Quantity(v, "units")
      sym = f"units[{standard}]"
      if sym not in _UNITS:
            define(sym, ACTIVITY, 1.0, standard=standard)
      return Quantity(v, sym)


def percent(v): return Quantity(v, "%")


def each(v): return Quantity(v, "each")


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


      def show_conc(conc: "Quantity", units=("mg/mL", "mcg/mL", "g/L", "mg/L", "ng/mL"),
                    prec: int = 6) -> None:
            print(f"{BOLD}{CYAN}CONC:{RESET} {BOLD}{conc}{RESET} {DIM}[{conc.dimension}]{RESET}")
            w = max(map(len, units))
            for u in units:
                  q = conc.to(u)
                  print(f"  -> {GREEN}{q.value:>14,.{prec}g}{RESET} {YELLOW}{u:<{w}}{RESET}")


      def expect_error(label: str, fn) -> None:
            try:
                  fn()
                  print(f"  {RED}{BOLD}NO ERROR RAISED{RESET} {DIM}({label}){RESET}")
            except Exception as e:  # noqa: BLE001
                  print(f"  {CYAN}{label:<28}{RESET} {RED}{type(e).__name__}{RESET}"
                        f" {DIM}{e}{RESET}")


      if __name__ == "__main__":
            # ---------------------------------------------------------------- 1
            _hdr("1. Intrinsic conversion within a dimension")
            dose = Quantity("6500 mg")
            _show("parsed from string", dose)
            for u in ("mcg", "mg", "g", "kg"):
                  _show(f"-> {u}", dose.to(u))

            # ---------------------------------------------------------------- 2
            _hdr("2. Derived concentration (MASS / VOLUME)")
            bag = Quantity("1 L")
            conc = dose / bag
            show_conc(conc)

            # ---------------------------------------------------------------- 3
            _hdr("3. Vancomycin 1.5 g in a 250 mL bag")
            vanc = g(1.5)
            bag250 = mL(250)
            show_conc(vanc / bag250, units=("mg/mL", "mcg/mL", "g/L"))

            # ---------------------------------------------------------------- 4
            _hdr("4. Rate arithmetic: infusion over time")
            rate: Quantity = mL(250) / Quantity(90, "min")
            _show("infusion rate", round(rate, 2), "volume per time")
            _show("in mL/hr", round(rate.to("mL/hr"), 2))
            drug_rate = vanc / Quantity(90, "min")
            _show("drug delivery rate", round(drug_rate.to("mg/hr"), 2))

            # ---------------------------------------------------------------- 5
            _hdr("5. Volume back-calculation from a vial concentration")
            vial = Quantity(100, "mg/mL")
            needed = mg(375)
            draw = needed / vial
            _show("need", needed)
            _show("vial strength", vial)
            _show("volume to draw", draw.to("mL"))

            # ---------------------------------------------------------------- 6
            _hdr("6. Weight-based dosing (mg/kg)")
            wt = kg(78.4)
            per_kg = Quantity(4.5, "mg/kg")
            total = per_kg * wt
            _show("patient weight", wt)
            _show("ordered dose", per_kg)
            _show("total dose", total.to("mg"))
            _show("rounded to g", total.to("g"))

            # ---------------------------------------------------------------- 7
            _hdr("7. Additive arithmetic + ordering")
            additives = [mL(20), mL(0.5), Quantity(0.004, "L"), mL(1.2)]
            overfill = sum(additives[1:], additives[0])
            _show("summed additive volume", overfill)
            _show("as L", overfill.to("L"))
            print(f"  {CYAN}{'20 mL > 0.004 L?':<28}{RESET} "
                  f"{BOLD}{YELLOW}{mL(20) > Quantity(0.004, 'L')}{RESET}")
            print(f"  {CYAN}{'1000 mcg == 1 mg?':<28}{RESET} "
                  f"{BOLD}{YELLOW}{mcg(1000) == mg(1)}{RESET}")
            print(f"  {CYAN}{'sorted doses':<28}{RESET} "
                  f"{BOLD}{GREEN}{[str(q) for q in sorted([g(0.5), mcg(900000), mg(250)])]}{RESET}")

            # ---------------------------------------------------------------- 8
            _hdr("8. Electrolytes: CHARGE is its own axis")
            kcl = mEq(40)
            _show("KCl additive", kcl)
            _show("as Eq", kcl.to("Eq"))
            _show("mEq per litre", (kcl / L(1)).to("mEq/L"))
            _show("mEq per hour", (kcl / Quantity(8, "hr")).to("mEq/hr"))

            # ---------------------------------------------------------------- 9
            _hdr("9. ACTIVITY standards are not interchangeable")
            hep = units(25_000, standard="heparin")
            ins = units(100, standard="insulin")
            _show("heparin", hep)
            _show("insulin", ins)
            _show("heparin conc", (hep / mL(250)).to("units[heparin]/mL"))
            expect_error("heparin + insulin", lambda: hep + ins)

            # ---------------------------------------------------------------- 10
            _hdr("10. Dimensional guardrails (bridges are NOT automatic)")
            expect_error("mg -> mmol", lambda: mg(100).to("mmol"))
            expect_error("mmol -> mEq", lambda: mmol(20).to("mEq"))
            expect_error("mg -> units", lambda: mg(1).to("units"))
            expect_error("mg + mL", lambda: mg(50) + mL(50))
            expect_error("mg/mL -> mg", lambda: Quantity(10, "mg/mL").to("mg"))

            # ---------------------------------------------------------------- 11
            _hdr("11. Counts and per-container math")
            vials = each(6)
            per_vial = mg(500)
            _show("vials on hand", vials)
            _show("strength each", per_vial)
            _show("total drug", (per_vial * vials).to("mg·each"))
            _show("cost-style ratio", mL(10) / vials)

            # ---------------------------------------------------------------- 12
            _hdr("12. Dimensionless algebra")
            ratio = mg(250) / mg(1000)
            _show("dose fraction", ratio, "cancels to 1")
            print(f"  {CYAN}{'is_dimensionless':<28}{RESET} "
                  f"{BOLD}{YELLOW}{ratio.dimension.is_dimensionless}{RESET}")
            _show("reciprocal of 4 hr", 1 / Quantity(4, "hr"))
            _show("2% (bare ratio)", percent(2), "NOT silently w/v")
