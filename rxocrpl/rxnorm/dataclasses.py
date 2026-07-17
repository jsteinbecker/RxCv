"""RxNorm domain models.

Data classes and enumerations used throughout the rxnorm package.
All types here are pure Python with no I/O or network dependencies.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Console formatting
# ---------------------------------------------------------------------------


class Ansi:
      """ANSI terminal formatting codes."""

      RESET = "\033[0m"

      BOLD = "\033[1m"
      DIM = "\033[2m"

      BLACK = "\033[30m"
      RED = "\033[31m"
      GREEN = "\033[32m"
      YELLOW = "\033[33m"
      BLUE = "\033[34m"
      MAGENTA = "\033[35m"
      CYAN = "\033[36m"
      WHITE = "\033[37m"

      BRIGHT_BLACK = "\033[90m"
      BRIGHT_RED = "\033[91m"
      BRIGHT_GREEN = "\033[92m"
      BRIGHT_YELLOW = "\033[93m"
      BRIGHT_BLUE = "\033[94m"
      BRIGHT_MAGENTA = "\033[95m"
      BRIGHT_CYAN = "\033[96m"
      BRIGHT_WHITE = "\033[97m"


def console_colors_enabled() -> bool:
      """Return whether ANSI terminal colors should be emitted.

      Colors are disabled when:

      - Standard output is not attached to a terminal.
      - The ``NO_COLOR`` environment variable is present.
      - ``RXNORM_COLOR`` is set to ``0``, ``false``, ``no``, or ``off``.
      - The terminal type is ``dumb``.

      Set ``RXNORM_COLOR=1`` to force colors even when stdout is not a TTY.
      """
      setting = os.environ.get("RXNORM_COLOR", "").strip().lower()

      if setting in {"1", "true", "yes", "on", "force"}:
            return True

      if setting in {"0", "false", "no", "off"}:
            return False

      if "NO_COLOR" in os.environ:
            return False

      if os.environ.get("TERM", "").lower() == "dumb":
            return False

      return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def colorize(value: object, *styles: str) -> str:
      """Return *value* wrapped in ANSI formatting codes when enabled."""
      text = str(value)

      if not styles or not console_colors_enabled():
            return text

      return f"{''.join(styles)}{text}{Ansi.RESET}"


# ---------------------------------------------------------------------------
# Base enumerations
# ---------------------------------------------------------------------------


class StrEnum(str, Enum):
      """Enum whose string representation is its underlying string value."""

      def __str__(self) -> str:
            return self.value


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class RelatedLevel(StrEnum):
      """Scope at which an NDC is resolved to an RxCUI."""

      CONCEPT = "concept"
      DRUG = "drug"
      PRODUCT = "product"


class ConceptStatus(StrEnum):
      """Possible values of the RxNorm historystatus ``status`` field."""

      ACTIVE = "Active"
      OBSOLETE = "Obsolete"
      QUANTIFIED = "Quantified"
      REMAPPED = "Remapped"
      NOT_CURRENT = "NotCurrent"
      UNKNOWN = "Unknown"

      @property
      def color(self) -> str:
            """ANSI color associated with the status."""
            return {
                  ConceptStatus.ACTIVE: Ansi.BRIGHT_GREEN,
                  ConceptStatus.OBSOLETE: Ansi.BRIGHT_RED,
                  ConceptStatus.QUANTIFIED: Ansi.BRIGHT_CYAN,
                  ConceptStatus.REMAPPED: Ansi.BRIGHT_YELLOW,
                  ConceptStatus.NOT_CURRENT: Ansi.BRIGHT_MAGENTA,
                  ConceptStatus.UNKNOWN: Ansi.BRIGHT_BLACK,
            }[self]

      def __str__(self) -> str:
            return colorize(self.value, Ansi.BOLD, self.color)


# ---------------------------------------------------------------------------
# Volume-prefix helpers
# ---------------------------------------------------------------------------

_VOLUME_PREFIX = re.compile(
      r"^\d+(?:\.\d+)?\s+ML\s+",
      re.IGNORECASE,
)


def strip_volume_prefix(name: str) -> str:
      """Remove a leading ``<n> ML `` segment from *name*, if present."""
      return _VOLUME_PREFIX.sub("", name, count=1)


# ---------------------------------------------------------------------------
# RxNorm concepts
# ---------------------------------------------------------------------------


@dataclass
class RxConcept:
      """A single RxNorm concept: identifier, name, and term type.

      ``tty`` and ``name`` may be ``None`` when the source payload omits them.

      :var rxcui: RxCUI identifier string.
      :var name: Human-readable concept name.
      :var tty: RxNorm term type, such as ``"SCD"`` or ``"SCDC"``.
      """

      rxcui: str
      name: str | None = None
      tty: str | None = None

      @classmethod
      def from_props(
                cls,
                props: dict[str, Any],
                default_tty: str | None = None,
      ) -> RxConcept:
            """Build from a conceptProperties, minConcept, or quantifiedConcept dict."""
            rxcui = props.get("rxcui")

            if not rxcui:
                  raise ValueError("rxcui is required in props")

            return cls(
                  rxcui=str(rxcui),
                  name=props.get("name"),
                  tty=props.get("tty") or default_tty,
            )

      def enrich(self) -> None:
            """Enrich the concept with its name and TTY from the API."""
            from rxocrpl.rxnorm.client import fetch_rxcui_name

            if not self.name:
                  self.name = fetch_rxcui_name(self.rxcui)

      @property
      def is_quantified(self) -> bool:
            """Return whether the name carries a leading volume prefix."""
            return (
                      bool(self.name)
                      and _VOLUME_PREFIX.match(self.name) is not None
            )

      @property
      def unquantified_name(self) -> str | None:
            """Return the name with any leading volume prefix removed."""
            return strip_volume_prefix(self.name) if self.name else None

      def plain_string(self) -> str:
            """Return an uncolored string representation."""
            tty = f" [{self.tty}]" if self.tty else ""
            name = self.name or "<unnamed>"

            return f"<{self.rxcui}{tty}> {name}"

      def __str__(self) -> str:
            rxcui = colorize(
                  self.rxcui,
                  Ansi.BOLD,
                  Ansi.BRIGHT_CYAN,
            )

            tty = ""

            if self.tty:
                  tty = colorize(
                        f" [{self.tty}]",
                        Ansi.BRIGHT_MAGENTA,
                  )

            if not self.name:
                  name = colorize(
                        "<unnamed>",
                        Ansi.DIM,
                        Ansi.BRIGHT_BLACK,
                  )
            elif self.is_quantified:
                  name = colorize(
                        self.name,
                        Ansi.BRIGHT_GREEN,
                  )
            else:
                  name = colorize(
                        self.name,
                        Ansi.BRIGHT_WHITE,
                  )

            return f"<{rxcui}{tty}> {name}"


# ---------------------------------------------------------------------------
# Volume grouping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolumeGroupKey:
      """A handle linking volume variants of the same drug.

      Background
      ----------
      Quantified SCDs, such as ``5 ML Sodium Chloride 0.9% Injection``,
      share an unquantified SCD base such as
      ``Sodium Chloride 0.9% Injection``.

      The unquantified base can carry ``SUPPRESS="E"`` and a ``Quantified``
      history status. Active-scoped endpoints may therefore omit it.

      Two grouping strategies are supported:

      ``kind == "scd"``
          ``base`` is the unquantified SCD resolved from the API or by name
          lookup. This is the preferred handle when available.

      ``kind == "scdc"``
          ``components`` is the frozenset of SCDC ingredient-and-strength CUIs
          shared by the volume variants. This is used when the base SCD cannot
          be resolved.

          SCDC identity does not encode dose form, so it will not distinguish
          an injection from an oral solution with identical ingredient
          strengths.

      Use :attr:`key` as the hashable grouping value.
      """

      kind: Literal["scd", "scdc"]
      base: RxConcept | None = None
      components: frozenset[str] = frozenset()

      def __post_init__(self) -> None:
            if self.kind == "scd" and self.base is None:
                  raise ValueError(
                        "base is required when kind is 'scd'"
                  )

            if self.kind == "scdc" and not self.components:
                  raise ValueError(
                        "components cannot be empty when kind is 'scdc'"
                  )

      @property
      def key(self) -> str | frozenset[str]:
            """Return the base SCD RxCUI or SCDC component set."""
            if self.kind == "scd":
                  if self.base is None:
                        raise ValueError(
                              "base is required when kind is 'scd'"
                        )

                  return self.base.rxcui

            return self.components

      def plain_string(self) -> str:
            """Return an uncolored string representation."""
            if self.kind == "scd":
                  if self.base is None:
                        return "scd:<missing>"

                  return f"scd:{self.base.plain_string()}"

            return f"scdc:{sorted(self.components)}"

      def __str__(self) -> str:
            if self.kind == "scd":
                  prefix = colorize(
                        "scd:",
                        Ansi.BOLD,
                        Ansi.BRIGHT_BLUE,
                  )

                  if self.base is None:
                        missing = colorize(
                              "<missing>",
                              Ansi.BOLD,
                              Ansi.BRIGHT_RED,
                        )
                        return f"{prefix}{missing}"

                  return f"{prefix}{self.base}"

            prefix = colorize(
                  "scdc:",
                  Ansi.BOLD,
                  Ansi.BRIGHT_YELLOW,
            )

            components = ", ".join(
                  colorize(
                        component,
                        Ansi.BRIGHT_CYAN,
                  )
                  for component in sorted(self.components)
            )

            return f"{prefix}[{components}]"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RxNavError(Exception):
      """Raised when an RxNav API call returns an unexpected HTTP status."""
