"""Materialize one or more RxNorm concepts from the command line.

    python manage.py rxnorm_pull 197589
    python manage.py rxnorm_pull 1191 --release 2026-06 --no-edges
    python manage.py rxnorm_pull 197589 1155862 --max-nodes 5000
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the project root is in sys.path BEFORE any project imports
project_root = str(Path(__file__).resolve().parent.parent.parent)
if project_root not in sys.path:
      sys.path.insert(0, project_root)

from django.core.management.base import BaseCommand, CommandError

# Use absolute imports to avoid ModuleNotFoundError when run as a script
from rxocrpl.rxgraph.client import RxNavClient
from rxocrpl.rxgraph.pipeline import DEFAULT_TTYS, materialize_concept


class Command(BaseCommand):
      help = "Fetch RxNorm concept(s) and build their BN/IN/PIN/SCD/SCDG/SCDC graph."

      def add_arguments(self, parser):
            parser.add_argument("identifiers", nargs="+", help="One or more RXCUIs or drug names.")
            parser.add_argument("--release", default="", help="RxNorm release tag, e.g. 2026-06.")
            parser.add_argument("--no-edges", action="store_true", help="Create nodes only.")
            parser.add_argument("--max-nodes", type=int, default=2000)
            parser.add_argument(
                  "--ttys",
                  default=",".join(sorted(DEFAULT_TTYS)),
                  help="Comma-separated TTYs to materialize.",
            )

      def handle(self, *args, **opts):
            ttys = frozenset(t.strip() for t in opts["ttys"].split(",") if t.strip())
            client = RxNavClient()
            for ident in opts["identifiers"]:
                  # If it's all digits, treat as RXCUI; else treat as name.
                  if ident.isdigit():
                        rxcui = ident
                  else:
                        self.stdout.write(f"Resolving {ident!r}...")
                        rxcui = client.find_rxcui_by_name(ident)
                        if not rxcui:
                              self.stdout.write(self.style.ERROR(f"Could not resolve {ident!r} to an RXCUI."))
                              continue
                        self.stdout.write(f"  → {rxcui}")

                  try:
                        result = materialize_concept(
                              rxcui,
                              client=client,
                              release=opts["release"],
                              tty_filter=ttys,
                              build_edges=not opts["no_edges"],
                              max_nodes=opts["max_nodes"],
                        )
                  except Exception as exc:  # noqa: BLE001
                        raise CommandError(f"{ident} ({rxcui}): {exc}") from exc

                  if result.anchor is None:
                        self.stdout.write(self.style.WARNING(
                              f"{ident} ({rxcui}): no RxNorm normalized name (nothing anchored)."
                        ))
                        continue

                  self.stdout.write(self.style.SUCCESS(str(result)))


if __name__ == "__main__":
      import django
      from pathlib import Path

      # Load environment variables from .env file if it exists
      env_path = Path(__file__).resolve().parent / ".env"
      if env_path.exists():
            with env_path.open() as f:
                  for line in f:
                        if line.strip() and not line.startswith("#"):
                              try:
                                    key, value = line.strip().split("=", 1)
                                    os.environ[key] = value.strip("\"'")
                              except ValueError:
                                    continue

      django.setup()  # requires DJANGO_SETTINGS_MODULE in the environment
      from django.core.management import call_command

      call_command("rxnorm_pull", *(sys.argv[1:] or ["acetaminophen oral pill"]))
