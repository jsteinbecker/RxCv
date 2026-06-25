"""RxNorm domain models.

Data classes and enumerations used throughout the rxnorm package.
All types here are pure Python with no I/O or network dependencies.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from enum import Enum


class StrEnum(str, Enum):
      pass


from typing import Literal, Any


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class RelatedLevel(StrEnum):
      """Scope at which an NDC is resolved to an RxCUI."""
      CONCEPT = "concept"
      DRUG = "drug"
      PRODUCT = "product"


class ConceptStatus(StrEnum):
      """Possible values of the ``status`` field in an RxNorm historystatus response."""
      ACTIVE = "Active"
      OBSOLETE = "Obsolete"
      QUANTIFIED = "Quantified"
      REMAPPED = "Remapped"
      NOT_CURRENT = "NotCurrent"
      UNKNOWN = "Unknown"


# ---------------------------------------------------------------------------
# Volume-prefix helpers
# ---------------------------------------------------------------------------

_VOLUME_PREFIX = re.compile(r"^\d+(?:\.\d+)?\s+ML\s+", re.IGNORECASE)


def strip_volume_prefix(name: str) -> str:
      """Remove a leading ``<n> ML `` segment from *name*, if present."""
      return _VOLUME_PREFIX.sub("", name, count=1)


@dataclass()
class RxConcept:
      """A single RxNorm concept: identifier, name, and term type.

      Frozen and slotted so instances are hashable — they can be set members,
      dict keys, or cached return values.  ``tty`` and ``name`` may be ``None``
      when the source payload omits them.

      :var rxcui: RxCUI identifier string.
      :var name:  Human-readable concept name.
      :var tty:   RxNorm term type (e.g. ``"SCD"``, ``"SCDC"``).
      """
      rxcui: str
      name: str | None = None
      tty: str | None = None

      @classmethod
      def from_props(cls, props: dict[str, Any], default_tty: str | None = None) -> RxConcept:
            """Build from a ``conceptProperties`` / ``minConcept`` / ``quantifiedConcept`` dict."""
            rxcui = props.get("rxcui")
            if not rxcui:
                  raise ValueError("rxcui is required in props")
            return cls(
                  rxcui=rxcui,
                  name=props.get("name"),
                  tty=props.get("tty") or default_tty,
            )

      def enrich(self) -> None:
            """Enrich with name and tty from the API."""
            from rxocrpl.rxnorm.client import fetch_rxcui_name
            if not self.name:
                  self.name = fetch_rxcui_name(self.rxcui)

      @property
      def is_quantified(self) -> bool:
            """True if the name carries a volume prefix (a quantified normal form)."""
            return bool(self.name) and _VOLUME_PREFIX.match(self.name) is not None

      @property
      def unquantified_name(self) -> str | None:
            """The name with any leading volume prefix removed."""
            return strip_volume_prefix(self.name) if self.name else None

      def __str__(self) -> str:
            tty = f" [{self.tty}]" if self.tty else ""
            return f"<{self.rxcui}{tty}> {self.name}"


@dataclass(frozen=True)
class VolumeGroupKey:
      """A handle that links volume variants (5 ML, 15 ML, …) of one drug.

      Background
      ----------
      Quantified SCDs (e.g. *5 ML Sodium Chloride 0.9% Injection*) share an
      unquantified SCD base (*Sodium Chloride 0.9% Injection*) that carries
      ``SUPPRESS="E"`` ("Quantified" status in historystatus).  Because active-
      scoped endpoints silently omit ``SUPPRESS="E"`` atoms, the base is not
      always resolvable via the live API.

      Two grouping strategies therefore exist:

      ``kind == "scd"``
          ``base`` is the unquantified SCD resolved from the API or by name
          look-up.  This is the preferred handle when available.

      ``kind == "scdc"``
          ``components`` is the frozenset of SCDC (ingredient+strength) CUIs
          shared by all volume variants.  Used when the base SCD cannot be
          resolved.  Note that SCDC identity does not encode dose form, so it
          will *not* separate an Injection from an oral solution at identical
          strengths.

      Use ``VolumeGroupKey.key`` as the hashable value for grouping or dict
      keying.
      """

      kind: Literal["scd", "scdc"]
      base: RxConcept | None = None
      components: frozenset[str] = frozenset()

      @property
      def key(self) -> str | frozenset[str]:
            """Hashable group handle: the base SCD's rxcui, or the SCDC frozenset."""
            if self.kind == "scd":
                  if self.base is None:
                        raise ValueError("base is required when kind is 'scd'")
                  return self.base.rxcui
            return self.components

      def __str__(self) -> str:
            if self.kind == "scd":
                  return f"scd:{self.base}"
            return f"scdc:{sorted(self.components)}"


class RxNavError(Exception):
      """Raised when an RxNav API call fails with an unexpected HTTP status."""
