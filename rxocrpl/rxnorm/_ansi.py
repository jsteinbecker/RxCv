"""ANSI colorization helpers for RxNorm demo output.

Colors are emitted only when stdout is a TTY (and ``NO_COLOR`` is unset), so
piping to a file or paging through ``less`` stays clean.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Iterable

_ENABLED = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str) -> str:
      return code if _ENABLED else ""


RESET, BOLD, DIM = _c("\033[0m"), _c("\033[1m"), _c("\033[2m")
RED, GREEN, YELLOW = _c("\033[31m"), _c("\033[32m"), _c("\033[33m")
BLUE, MAGENTA, CYAN = _c("\033[34m"), _c("\033[35m"), _c("\033[36m")

# TTY -> color, so a term type reads the same everywhere it appears.
_TTY_COLORS = {
      "SCD": GREEN, "SCSD": GREEN, "SBD": BLUE,
      "SCDC": YELLOW, "SCDF": YELLOW, "SCDG": YELLOW,
      "IN": MAGENTA, "PIN": MAGENTA, "MIN": MAGENTA,
      "BN": CYAN, "BPCK": CYAN, "GPCK": CYAN,
}

_STATUS_COLORS = {
      "Active": GREEN, "Quantified": CYAN, "Remapped": YELLOW,
      "Obsolete": RED, "Retired": RED, "NotCurrent": RED, "Unknown": DIM,
}


def hdr(title: str, width: int = 68) -> None:
      """Print a section banner."""
      print(f"\n{BOLD}{MAGENTA}{'─' * width}{RESET}")
      print(f"{BOLD}{MAGENTA}{title}{RESET}")
      print(f"{BOLD}{MAGENTA}{'─' * width}{RESET}")


def tty(term_type: str | None) -> str:
      """Colorize a term type by family."""
      if not term_type:
            return f"{DIM}—{RESET}"
      return f"{_TTY_COLORS.get(term_type.upper(), '')}{term_type}{RESET}"


def status(value: Any) -> str:
      """Colorize a ConceptStatus (or its raw string) by severity."""
      if value is None:
            return f"{DIM}none{RESET}"
      raw = getattr(value, "value", value)
      return f"{BOLD}{_STATUS_COLORS.get(str(raw), '')}{raw}{RESET}"


def cui(rxcui: str | None) -> str:
      """Colorize an RxCUI."""
      return f"{YELLOW}{rxcui}{RESET}" if rxcui else f"{DIM}—{RESET}"


def concept(c: Any) -> str:
      """Render an RxConcept as ``cui [tty] name``."""
      if c is None:
            return f"{DIM}None{RESET}"
      return f"{cui(c.rxcui)} [{tty(c.tty)}] {c.name}"


def field(label: str, value: Any, note: str = "", pad: int = 22) -> None:
      """Print an aligned ``label  value  (note)`` row."""
      tail = f"  {DIM}{note}{RESET}" if note else ""
      print(f"  {CYAN}{label:<{pad}}{RESET} {value}{tail}")


def listing(label: str, items: Iterable[Any], render=concept) -> None:
      """Print a labelled list of concepts, or a dim ``(none)``."""
      items = list(items)
      count = f"{DIM}({len(items)}){RESET}"
      print(f"  {BOLD}{CYAN}{label}{RESET} {count}")
      if not items:
            print(f"    {DIM}(none){RESET}")
            return
      for it in items:
            print(f"    {DIM}·{RESET} {render(it)}")


def ok(passed: bool, message: str) -> None:
      """Print a pass/fail check line."""
      mark = f"{GREEN}✓{RESET}" if passed else f"{RED}✗{RESET}"
      print(f"  {mark} {message}")


def error(exc: BaseException, label: str = "") -> None:
      """Print a caught exception."""
      tag = f"{CYAN}{label:<22}{RESET} " if label else "  "
      print(f"  {tag}{RED}{type(exc).__name__}{RESET} {DIM}{exc}{RESET}")
