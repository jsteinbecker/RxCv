from __future__ import annotations
import rxocrpl.consoleprint

from dataclasses import dataclass, field
from typing import Iterator

try:
      from .quantities import (
            DimensionalityError,
            Quantity,
            Dimension,
            MASS,
            SUBSTANCE,
            CHARGE,
            ACTIVITY,
            VOLUME,
            _resolve,
            _UNITS,
            define
      )
except ImportError:
      from rxocrpl.quantities import (
            DimensionalityError,
            Quantity,
            Dimension,
            MASS,
            SUBSTANCE,
            CHARGE,
            ACTIVITY,
            VOLUME,
            _resolve,
            _UNITS,
            define
      )


class BridgeError(DimensionalityError):
      pass


class SubstanceMismatch(TypeError):
      pass


def _base_unit_for (
          dim: Dimension,
          standard: str | None = None
) -> str:
      if dim == ACTIVITY and standard is not None:
            sym = f"units[{standard}]"
            if sym not in _UNITS:
                  define(sym, ACTIVITY, 1.0, standard=standard)
            return sym
      for d, sym in (
                  (MASS, "g"),
                  (VOLUME, "L"),
                  (SUBSTANCE, "mol"),
                  (CHARGE, "Eq"),
                  (ACTIVITY, "units"),
      ):
            if dim == d:
                  return sym
      raise BridgeError(f"no canonical base unit for dimension {dim}")


@dataclass(frozen=True)
class Substance:
      name: str
      molar_mass: Quantity | None = None
      valence: Quantity | None = None
      potency: Quantity | None = None
      density: Quantity | None = None
      standard: str | None = None
      particles: float | None = None
      salt_of: str | None = None

      def _factors (self) -> list[tuple[Dimension, Dimension, Quantity | None, str]]:
            return [
                  (MASS, SUBSTANCE, self.molar_mass, "molar_mass"),
                  (SUBSTANCE, CHARGE, self.valence, "valence"),
                  (MASS, ACTIVITY, self.potency, "potency"),
                  (VOLUME, MASS, self.density, "density"),
            ]

      def _bridge (self, frm: Dimension, to: Dimension) -> tuple[Quantity, int]:
            for a, b, factor, label in self._factors():
                  if {frm, to} != {a, b}:
                        continue
                  if factor is None:
                        raise BridgeError(
                              f"{self.name!r} has no {label}: cannot bridge {frm} -> {to}"
                        )
                  fdim = factor.dimension
                  if fdim == to / frm:
                        return factor, +1
                  if fdim == frm / to:
                        return factor, -1
                  raise BridgeError(
                        f"{self.name!r} {label} has dimension {fdim}, which cannot "
                        f"bridge {frm} -> {to}"
                  )
            raise BridgeError(f"no bridge {frm} -> {to} for {self.name!r}")

      def _path (self, frm: Dimension, to: Dimension) -> list[Dimension]:
            nodes = [MASS, VOLUME, SUBSTANCE, CHARGE, ACTIVITY]
            edges = {
                  (MASS, SUBSTANCE),
                  (SUBSTANCE, MASS),
                  (SUBSTANCE, CHARGE),
                  (CHARGE, SUBSTANCE),
                  (MASS, ACTIVITY),
                  (ACTIVITY, MASS),
                  (VOLUME, MASS),
                  (MASS, VOLUME),
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

      def convert (self, q: Quantity, target: str) -> Quantity:
            tu = _resolve(target)
            if q.dimension == tu.dimension:
                  return q.to(target)
            path = self._path(q.dimension, tu.dimension)
            for step_from, step_to in zip(path, path[1:]):
                  factor, exp = self._bridge(step_from, step_to)
                  q = q * factor if exp > 0 else q / factor
            base = _base_unit_for(tu.dimension, self.standard)
            q = Quantity(q.to_base() / _resolve(base).scale, base)
            return q.to(target)

      def has_bridge (self, frm: Dimension, to: Dimension) -> bool:
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

      def __call__ (self, q: Quantity | str) -> "SubstanceQuantity":
            if isinstance(q, str):
                  q = Quantity(q)
            return SubstanceQuantity(q.value, q.unit, self)

      def __str__ (self) -> str:
            return self.name


@dataclass(eq=False)
class SubstanceQuantity(Quantity):
      substance: Substance = field(init=False)

      def __init__ (self, value, unit=None, substance: Substance | None = None):
            Quantity.__init__(self, value, unit)
            if substance is None:
                  raise ValueError("SubstanceQuantity requires a substance")
            self.substance = substance

      def to (self, target: str) -> "SubstanceQuantity":
            q = self.substance.convert(Quantity(self.value, self.unit), target)
            return SubstanceQuantity(q.value, q.unit, self.substance)

      def _same (self, other) -> None:
            if isinstance(other, SubstanceQuantity) and other.substance != self.substance:
                  raise SubstanceMismatch(
                        f"cannot combine {self.substance} with {other.substance}"
                  )

      def _add_sub (self, other, sign: int):
            self._same(other)
            q = Quantity._add_sub(self, other, sign)
            return SubstanceQuantity(q.value, q.unit, self.substance)

      def __mul__ (self, other):
            q = Quantity.__mul__(self, other)
            return (
                  SubstanceQuantity(q.value, q.unit, self.substance)
                  if isinstance(q, Quantity)
                  else q
            )

      __rmul__ = __mul__

      def __truediv__ (self, other):
            if isinstance(other, SubstanceQuantity):
                  self._same(other)
            q = Quantity.__truediv__(self, other)
            return (
                  SubstanceQuantity(q.value, q.unit, self.substance)
                  if isinstance(q, Quantity)
                  else q
            )

      def _comparable (self, other) -> bool:
            return (
                      Quantity._comparable(self, other)
                      and getattr(other, "substance", self.substance) == self.substance
            )

      def __hash__ (self):
            return hash((Quantity.__hash__(self), self.substance.name))

      def can_convert_to (self, to: Dimension) -> bool:
            if self.dimension == to:
                  return True
            return self.substance.has_bridge(self.dimension, to)

      @property
      def moles (self) -> "SubstanceQuantity":
            return self.to("mmol")

      @property
      def charge (self) -> "SubstanceQuantity":
            return self.to("mEq")

      @property
      def mass (self) -> "SubstanceQuantity":
            return self.to("mg")

      @property
      def activity (self) -> "SubstanceQuantity":
            return self.to("units")

      @property
      def osmoles (self) -> Quantity:
            i = self.substance.particles
            if i is None:
                  raise BridgeError(f"{self.substance.name!r} has no `particles` factor")
            if i == 0:
                  return Quantity(0.0, "mmol")
            return Quantity(self.to("mmol").value * i, "mmol")

      def __str__ (self) -> str:
            return f"{self.value:g} {self.unit} {self.substance}"


@dataclass
class Mixture:
      name: str = "mixture"
      constituents: list[SubstanceQuantity] = field(default_factory=list)
      volume: Quantity | None = None

      def __post_init__ (self) -> None:
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
                  raise DimensionalityError(
                        f"volume must be VOLUME, got {self.volume.dimension}"
                  )

      def add (self, *items: SubstanceQuantity) -> "Mixture":
            return Mixture(self.name, self.constituents + list(items), self.volume)

      def in_volume (self, volume: Quantity) -> "Mixture":
            return Mixture(self.name, list(self.constituents), volume)

      def __add__ (self, other: "Mixture | SubstanceQuantity") -> "Mixture":
            if isinstance(other, SubstanceQuantity):
                  return self.add(other)
            if not isinstance(other, Mixture):
                  return NotImplemented
            vol = None
            if self.volume is not None and other.volume is not None:
                  vol = self.volume + other.volume
            elif self.volume is not None or other.volume is not None:
                  raise DimensionalityError(
                        "cannot add a mixture with a known volume to one without"
                  )
            return Mixture(
                  f"{self.name}+{other.name}", self.constituents + other.constituents, vol
            )

      def __mul__ (self, k: float) -> "Mixture":
            return Mixture(
                  self.name,
                  [c * k for c in self.constituents],
                  self.volume * k if self.volume is not None else None,
            )

      __rmul__ = __mul__

      def __iter__ (self) -> Iterator[SubstanceQuantity]:
            return iter(self.constituents)

      def __len__ (self) -> int:
            return len(self.constituents)

      def __contains__ (self, s: Substance | str) -> bool:
            key = s if isinstance(s, str) else s.name
            return any(c.substance.name == key for c in self.constituents)

      def __getitem__ (self, s: Substance | str) -> SubstanceQuantity:
            key = s if isinstance(s, str) else s.name
            for c in self.constituents:
                  if c.substance.name == key:
                        return c
            raise KeyError(f"{key!r} not in {self.name}")

      def amount (self, s: Substance | str, unit: str) -> SubstanceQuantity:
            return self[s].to(unit)

      def _require_volume (self) -> Quantity:
            if self.volume is None:
                  raise DimensionalityError(
                        f"{self.name!r} has no volume set; use .in_volume(...) first"
                  )
            return self.volume

      def concentration (self, s: Substance | str, unit: str = "mg/mL") -> Quantity:
            num, den = unit.split("/", 1)
            return self[s].to(num) / self._require_volume().to(den)

      def concentrations (self, unit: str = "mg/mL") -> dict[str, Quantity]:
            out = {}
            for c in self.constituents:
                  try:
                        out[c.substance.name] = self.concentration(c.substance, unit)
                  except BridgeError:
                        continue
            return out

      def total_osmolarity (self, unit: str = "mmol/L") -> Quantity:
            total = Quantity(0, "mmol")
            for c in self.constituents:
                  total = total + c.osmoles
            num, den = unit.split("/", 1)
            return total.to(num) / self._require_volume().to(den)

      def charge_balance (self, unit: str = "mEq") -> Quantity:
            total = Quantity(0, "mEq")
            for c in self.constituents:
                  if c.substance.valence is None:
                        continue
                  sign = -1.0 if c.substance.valence.value < 0 else 1.0
                  total = total + Quantity(sign * abs(c.to("mEq").value), "mEq")
            return total.to(unit)

      def cation_charge (self, unit: str = "mEq") -> Quantity:
            total = Quantity(0, "mEq")
            for c in self.constituents:
                  v = c.substance.valence
                  if v is None or v.value < 0:
                        continue
                  total = total + Quantity(abs(c.to("mEq").value), "mEq")
            return total.to(unit)

      def total_mass (self, unit: str = "mg") -> Quantity:
            total = Quantity(0, "mg")
            for c in self.constituents:
                  if not c.can_convert_to(MASS):
                        continue
                  total = total + Quantity(c.to("mg").value, "mg")
            return total.to(unit)

      def __str__ (self) -> str:
            body = ", ".join(str(c) for c in self.constituents) or "empty"
            vol = f" in {self.volume}" if self.volume is not None else ""
            return f"{self.name}[{body}]{vol}"


# --------------------------------------------------------------------------
# A small starter formulary
# --------------------------------------------------------------------------
KCL = Substance(
      "KCl",
      molar_mass=Quantity(74.55, "g/mol"),
      valence=Quantity(1, "Eq/mol"),
      particles=2,
)
NACL = Substance(
      "NaCl",
      molar_mass=Quantity(58.44, "g/mol"),
      valence=Quantity(1, "Eq/mol"),
      particles=2,
)
CACL2 = Substance(
      "CaCl2",
      molar_mass=Quantity(110.98, "g/mol"),
      valence=Quantity(2, "Eq/mol"),
      particles=3,
)
MGSO4 = Substance(
      "MgSO4",
      molar_mass=Quantity(120.37, "g/mol"),
      valence=Quantity(2, "Eq/mol"),
      particles=2,
)
DEXTROSE = Substance("Dextrose", molar_mass=Quantity(180.16, "g/mol"), particles=1)
ACETATE = Substance(
      "Acetate",
      molar_mass=Quantity(59.04, "g/mol"),
      valence=Quantity(-1, "Eq/mol"),
      particles=1,
)
HEPARIN = Substance("Heparin", potency=Quantity(180, "units/mg"), standard="heparin")
INSULIN = Substance("Insulin", potency=Quantity(28.8, "units/mg"), standard="insulin")
VANCOMYCIN = Substance("Vancomycin", molar_mass=Quantity(1449.3, "g/mol"), particles=1)
STERILE_WATER = Substance("Water", density=Quantity(1.0, "g/mL"), particles=0)

if __name__ == "__main__":
      from rxocrpl.consoleprint import _hdr as h, _sub as s, _val as v

      h("Substance.py test")
      s(KCL)
      v(KCL.__dict__)
      s(NACL)
      s(CACL2)
      s(MGSO4)
      s(DEXTROSE)
      s(ACETATE)
      s(HEPARIN)
      s(INSULIN)
      s(VANCOMYCIN)
      s(STERILE_WATER)
