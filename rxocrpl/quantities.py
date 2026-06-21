"""Dimensional quantity system for pharmacy modeling.

Quantities are represented as vectors over a deliberately small set of base
dimensions chosen for compounding workflows, rather than the full seven SI base
dimensions. Most SI dimensions do not appear directly in pharmacy calculations,
so this system uses pharmacy-native dimensions instead.

## Base dimensions

`MASS`
Base unit: gram.

```
Supported units include ``mcg``, ``mg``, ``g``, and ``kg``.
```

`VOLUME`
Base unit: litre.

```
Supported units include ``mL`` and ``L``.

Volume is treated as a first-class pharmacy dimension, not as ``L**3`` or
as a derived length-cubed dimension.
```

`SUBSTANCE`
Base unit: mole.

```
Supported units include ``mmol`` and ``mol``.
```

`CHARGE`
Base unit: equivalent.

```
Supported units include ``mEq`` and ``Eq``.

This represents moles of ionic charge, not electrical charge in coulombs.
```

`TIME`
Base unit: second.

```
Supported units include ``s``, ``min``, ``hr``, and ``day``.
```

`COUNT`
Base unit: each.

```
Used for discrete dosage units, packages, containers, vials, tablets,
capsules, syringes, bags, and other countable pharmacy objects.
```

`ACTIVITY`
Base unit: unit.

```
Used for USP Units, International Units, and other biological activity
units tied to a reference standard.
```

## Design rules

Intrinsic conversions

```

Conversions within a single dimension are intrinsic to the quantity system.

Examples:

* ``mg`` to ``g``
* ``mcg`` to ``mg``
* ``mL`` to ``L``
* ``hr`` to ``s``

These conversions do not require substance-specific knowledge.

Bridge conversions
~~~~~~~~~~~~~~~~~~

Conversions between dimensions are not automatic.

Examples:

* ``mg`` to ``mmol``
* ``mmol`` to ``mEq``
* ``mg`` to ``Units``

These conversions require substance-specific bridge factors, such as:

* molar mass
* valence
* salt form
* hydration state
* concentration basis
* specific activity
* biological reference standard

Those bridge factors belong to the ``Substance`` or bridge-conversion layer,
not to the generic ``Quantity`` type.

Activity standards
~~~~~~~~~~~~~~~~~~

Biological activity units are not universally commensurable.

For example:

* ``1 IU`` of heparin is not equivalent to ``1 IU`` of insulin.
* ``1 USP Unit`` of one drug is not necessarily comparable to ``1 USP Unit``
  of another drug.

For this reason, quantities on the ``ACTIVITY`` axis may carry an optional
``standard`` tag identifying the biological reference standard.

Activities may only be added, compared, or converted when their standards are
compatible. Combining activities with different standards is an error, even
though they share the same ``ACTIVITY`` dimension.
```
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


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
define("", DIMENSIONLESS, 1.0)


def _resolve(symbol: str) -> Unit:
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

      def __hash__(self):
            return hash((round(self.to_base(), 12), self.dimension, self.standard))

      def __str__(self) -> str:
            return f"{self.value} {self.unit}".rstrip()

      def __init__(self, value: float | int, unit: str):
            object.__setattr__(self, "value", value)
            object.__setattr__(self, "unit", unit)


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
