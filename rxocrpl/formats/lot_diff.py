"""Highlight the minimal distinguishing portion of each lot number in a set.

For every lot in a collection, find the shortest contiguous substring that
appears in *that* lot but in *no other* lot, then render the lot with that
substring emphasized. This is useful when many lot numbers share long common
prefixes/suffixes (e.g. manufacturer or product codes) and only a small
segment actually distinguishes one from another.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable


# --------------------------------------------------------------------------- #
# Result container
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LotHighlight:
      """The outcome of differentiating a single lot number.

      Attributes:
          lot:   The original, unmodified lot string.
          start: Index where the distinguishing substring begins.
          end:   Index one past where it ends (i.e. lot[start:end] is the segment).
                 If no unique substring exists, start == end == -1.
      """

      lot: str
      start: int
      end: int

      @property
      def is_unique(self) -> bool:
            """True if a distinguishing substring was found."""
            return self.start != -1

      @property
      def segment(self) -> str:
            """The distinguishing substring, or '' if none exists."""
            return self.lot[self.start:self.end] if self.is_unique else ""

      def render(self, on: str = "[", off: str = "]") -> str:
            """Return the lot with the distinguishing segment wrapped in markers.

            Args:
                on:  Marker inserted before the segment.
                off: Marker inserted after the segment.

            Returns:
                The lot string with markers, or the plain lot if not unique.
            """
            if not self.is_unique:
                  return self.lot
            return f"{self.lot[:self.start]}{on}{self.segment}{off}{self.lot[self.end:]}"


# --------------------------------------------------------------------------- #
# Core class
# --------------------------------------------------------------------------- #
class LotDifferentiator:
      """Compute and display minimal distinguishing substrings across lots.

      Given a collection of lot-number strings, each lot's minimal distinguishing
      substring is the shortest contiguous run of characters that occurs in that
      lot and in none of the others. Ties in length are broken by the earliest
      starting position (left-most wins).

      Example:
          >>> d = LotDifferentiator(["ABC123", "ABC124", "ABC999"])
          >>> print(d)
          ABC12[3]
          ABC12[4]
          ABC[9]99
      """

      def __init__(self, lots: Iterable[str]) -> None:
            """Store lots, preserving first-seen order and dropping duplicates.

            Args:
                lots: Any iterable of lot-number strings.

            Raises:
                TypeError:  If any element is not a string.
                ValueError: If the collection is empty.
            """
            seen: dict[str, None] = {}
            for lot in lots:
                  if not isinstance(lot, str):
                        raise TypeError(f"Lot numbers must be strings, got {type(lot).__name__!r}")
                  seen.setdefault(lot, None)  # de-dupe, keep order

            if not seen:
                  raise ValueError("At least one lot number is required.")

            self._lots: tuple[str, ...] = tuple(seen)
            self._highlights: tuple[LotHighlight, ...] | None = None  # computed lazily

      # ------------------------------------------------------------------ #
      # Public API
      # ------------------------------------------------------------------ #
      @property
      def lots(self) -> tuple[str, ...]:
            """The de-duplicated lot numbers, in first-seen order."""
            return self._lots

      def highlights(self) -> tuple[LotHighlight, ...]:
            """Return the LotHighlight for every lot (computed once, then cached)."""
            if self._highlights is None:
                  self._highlights = tuple(self._differentiate(lot) for lot in self._lots)
            return self._highlights

      def render(self, on: str = "[", off: str = "]") -> list[str]:
            """Return each lot rendered with its distinguishing segment emphasized."""
            return [h.render(on, off) for h in self.highlights()]

      def __str__(self) -> str:
            """Newline-joined rendering using default bracket markers."""
            return "\n".join(self.render())

      def __repr__(self) -> str:
            return f"{type(self).__name__}({list(self._lots)!r})"

      # ------------------------------------------------------------------ #
      # Internal machinery
      # ------------------------------------------------------------------ #
      def _differentiate(self, target: str) -> LotHighlight:
            """Find the minimal distinguishing substring for a single lot.

            Strategy: scan candidate substrings of `target` by increasing length.
            The first substring (shortest, then left-most) that appears in no other
            lot is the answer. Membership across the other lots is tested against a
            set of *their* substrings of the same length, so the whole method runs
            without repeated linear scans of every other string.
            """
            others = [lot for lot in self._lots if lot is not target]

            # A lot identical in content to none-but-itself is trivially unique.
            # If some other lot equals target's full content, no substring can
            # distinguish them; fall through and return "not unique".
            n = len(target)

            for length in range(1, n + 1):
                  # All substrings of this length found anywhere in the other lots.
                  forbidden = self._substrings_of_length(others, length)

                  # Walk target's substrings left-to-right so ties favor earlier starts.
                  for start in range(0, n - length + 1):
                        candidate = target[start:start + length]
                        if candidate not in forbidden:
                              return LotHighlight(target, start, start + length)

            # No substring is exclusive to this lot (e.g. an exact duplicate content).
            return LotHighlight(target, -1, -1)

      @staticmethod
      def _substrings_of_length(strings: list[str], length: int) -> set[str]:
            """Collect every substring of the given length across `strings`."""
            out: set[str] = set()
            for s in strings:
                  for i in range(0, len(s) - length + 1):
                        out.add(s[i:i + length])
            return out


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
      sample = ["ABC123", "ABC124", "ABC999", "XYZ001", "ABB-O"]
      differ = LotDifferentiator(sample)

      for h in differ.highlights():
            tag = h.segment if h.is_unique else "<none>"
            print(f"{h.render():<12}  distinguishing: {tag!r}")
