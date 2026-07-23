"""rxgraph: build the RxNorm concept graph (BN/IN/PIN/SCD/SBD/SCDG/SCDC) from RxNav.

Run a live pull and printout against the real RxNav API::

    python rxgraph/__init__.py            # live pull of the demo rxcui (313782)
    python rxgraph/__init__.py 1191       # live pull of any rxcui
    python rxgraph/__init__.py --offline  # deterministic fixture, no network

Live mode needs network egress to ``rxnav.nlm.nih.gov`` (no API key required).
"""

from __future__ import annotations

# Allow direct execution (`python rxgraph/__init__.py`) in addition to the
# normal `import rxgraph`. When run as a script, __package__ is unset and the
# relative imports below would fail, so we make the parent dir importable and
# re-root this module inside its package first.
if __name__ == "__main__" and not __package__:
      import os
      import sys

      sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
      __package__ = "rxgraph"

from .client import RxNavClient, RxNavError
from .pipeline import (
      DEFAULT_TTYS,
      EDGE_RULES,
      ConceptGraph,
      MaterializeResult,
      add_concept,
      add_concept_by_name,
      fetch_family,
      materialize_concept,
      materialize_concept_graph,
)

__all__ = [
      "RxNavClient",
      "RxNavError",
      "DEFAULT_TTYS",
      "EDGE_RULES",
      "ConceptGraph",
      "MaterializeResult",
      "add_concept",
      "add_concept_by_name",
      "fetch_family",
      "materialize_concept",
      "materialize_concept_graph",
]

DEMO_RXCUI = "313782"  # Acetaminophen 325 MG Oral Tablet


# --------------------------------------------------------------------------- #
# Offline fixture client (used only by --offline).
# --------------------------------------------------------------------------- #
class _FakeRxNav:
      """Canned RxNav responses for one single-ingredient drug family."""

      _props = {
            "313782": {"rxcui": "313782", "tty": "SCD", "suppress": "N",
                       "name": "Acetaminophen 325 MG Oral Tablet"},
      }
      _allrelated = {
            "313782": [
                  {"tty": "IN", "conceptProperties": [
                        {"rxcui": "161", "tty": "IN", "name": "Acetaminophen", "suppress": "N"}]},
                  {"tty": "PIN", "conceptProperties": []},
                  {"tty": "BN", "conceptProperties": [
                        {"rxcui": "202433", "tty": "BN", "name": "Tylenol", "suppress": "N"}]},
                  {"tty": "SCDC", "conceptProperties": [
                        {"rxcui": "314051", "tty": "SCDC", "name": "Acetaminophen 325 MG", "suppress": "N"}]},
                  {"tty": "SCDG", "conceptProperties": [
                        {"rxcui": "1156291", "tty": "SCDG", "name": "Acetaminophen Pill", "suppress": "N"}]},
                  # SBD is the branded counterpart of the SCD and is now kept,
                  # linked back via SCD.has_tradename.
                  {"tty": "SBD", "conceptProperties": [
                        {"rxcui": "209387", "tty": "SBD",
                         "name": "Acetaminophen 325 MG Oral Tablet [Tylenol]", "suppress": "N"}]},
            ],
      }
      _related = {
            ("161", "has_tradename"): [{"rxcui": "202433", "tty": "BN", "name": "Tylenol"}],
            ("161", "has_form"): [],
            ("314051", "has_ingredient"): [{"rxcui": "161", "tty": "IN", "name": "Acetaminophen"}],
            ("314051", "has_precise_ingredient"): [],
            ("313782", "consists_of"): [{"rxcui": "314051", "tty": "SCDC", "name": "Acetaminophen 325 MG"}],
            ("313782", "has_ingredient"): [{"rxcui": "161", "tty": "IN", "name": "Acetaminophen"}],
            ("313782", "has_tradename"): [
                  {"rxcui": "209387", "tty": "SBD",
                   "name": "Acetaminophen 325 MG Oral Tablet [Tylenol]"}],
            ("209387", "has_ingredient"): [{"rxcui": "161", "tty": "IN", "name": "Acetaminophen"}],
            ("1156291", "inverse_isa"): [
                  {"rxcui": "313782", "tty": "SCD", "name": "Acetaminophen 325 MG Oral Tablet"},
                  # SCDF also comes back here and must be filtered out by target TTY.
                  {"rxcui": "999999", "tty": "SCDF", "name": "Acetaminophen Oral Tablet"},
            ],
            ("1156291", "has_ingredient"): [{"rxcui": "161", "tty": "IN", "name": "Acetaminophen"}],
      }

      def properties(self, rxcui):
            return self._props.get(rxcui, {})

      def all_related(self, rxcui):
            return self._allrelated.get(rxcui, [])

      def related_by_rela(self, rxcui, rela):
            return self._related.get((rxcui, rela), [])


# --------------------------------------------------------------------------- #
# Printing helpers.
# --------------------------------------------------------------------------- #
def _print_config() -> None:
      print(f"  materialized TTYs : {', '.join(sorted(DEFAULT_TTYS))}")
      print("  edge rules        :")
      for src, rules in EDGE_RULES.items():
            for rela, targets in rules:
                  print(f"    {src:5} -[{rela}]-> {{{', '.join(sorted(targets))}}}")


def _print_graph(graph: ConceptGraph) -> None:
      print(f"\n  nodes ({len(graph.nodes)}):")
      for node in sorted(graph.nodes.values(), key=lambda n: (n.tty, n.rxcui)):
            print(f"    {node.tty:5} {node.rxcui:8} active={node.active!s:5} {node.name}")
      print(f"\n  edges ({len(graph.edges)}):")
      for edge in graph.edges:
            src, tgt = graph.nodes[edge.source], graph.nodes[edge.target]
            print(f"    {src.tty}:{src.rxcui} -[{edge.rela}]-> {tgt.tty}:{tgt.rxcui}")


# --------------------------------------------------------------------------- #
# Self-tests.
# --------------------------------------------------------------------------- #
def _self_test_offline() -> None:
      """Deterministic fixture run with asserts -- no network, no database."""
      print("rxgraph self-test (offline fixture)")
      _print_config()
      # pyrefly: ignore [bad-argument-type]
      graph = fetch_family(DEMO_RXCUI, client=_FakeRxNav())
      print(f"\n  Acetaminophen 325 MG Oral Tablet ({DEMO_RXCUI})")
      _print_graph(graph)

      # SBD is now kept (branded counterpart of the SCD); the stray SCDF must
      # still not survive the TTY filter.
      assert len(graph.nodes) == 6, f"expected 6 nodes, got {len(graph.nodes)}"
      assert "209387" in graph.nodes, "SBD should have been materialized"
      assert "999999" not in graph.nodes, "SCDF should have been filtered out"
      built = {(e.source, e.rela, e.target) for e in graph.edges}
      expected = {
            ("161", "has_tradename", "202433"),
            ("314051", "has_ingredient", "161"),
            ("313782", "consists_of", "314051"),
            ("313782", "has_ingredient", "161"),
            ("313782", "has_tradename", "209387"),
            ("209387", "has_ingredient", "161"),
            ("1156291", "inverse_isa", "313782"),
            ("1156291", "has_ingredient", "161"),
      }
      assert built == expected, f"edge mismatch:\n  got {built}\n  exp {expected}"
      print("\n  self-test OK \u2713")


def _self_test_live(rxcui: str, *, client: RxNavClient | None = None) -> None:
      """Live pull from the real RxNav API. Counts are not asserted (real data)."""
      client = client or RxNavClient()
      print(f"rxgraph live RxNav pull (rxcui {rxcui})")
      _print_config()
      graph = fetch_family(rxcui, client=client)
      anchor = graph.nodes.get(rxcui)
      print(f"\n  anchor: {anchor.tty if anchor else '?'} {rxcui} "
            f"{anchor.name if anchor else '(no RxNorm normalized name)'}")
      _print_graph(graph)
      print("\n  live pull complete \u2713  (run rxnorm_pull to persist to the DB)")


if __name__ == "__main__":
      import sys

      argv = sys.argv[1:]
      if "--offline" in argv:
            _self_test_offline()
            print("OFFLINE **************")
      else:
            target = next((a for a in argv if not a.startswith("-")), DEMO_RXCUI)
            client = RxNavClient()
            if not target.isdigit():
                  print(f"Resolving {target!r}...")
                  target = client.find_rxcui_by_name(target)
                  if not target:
                        print(f"Could not resolve {sys.argv[1]!r}")
                        sys.exit(1)
                  print(f"  → {target}")
            try:
                  _self_test_live(target, client=client)
                  _self_test_offline()
            except RxNavError as exc:
                  print(f"\nLive RxNav request failed: {exc}")
                  print("Check network egress to rxnav.nlm.nih.gov, or run "
                        "`python rxgraph/__init__.py --offline` for the offline fixture.")
                  sys.exit(1)
