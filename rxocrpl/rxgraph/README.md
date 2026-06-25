# rxgraph

Auto-build the RxNorm concept graph — **BN, IN, PIN, SCD, SCDG, SCDC** — into
your existing `RxNormConcept` + `RxNormConceptRelation` tables whenever you add
an `RxNormConcept`. RxNav is queried for the family and for each edge, so the graph
direction and existence come from RxNorm itself, not from a hardcoded guess.

```
rxgraph/
├── client.py        # dependency-free RxNav REST client (urllib; pluggable)
├── pipeline.py      # fetch family -> upsert nodes + edges (core)
├── signals.py       # optional: auto-materialize on RxNormConcept creation
├── traversal.py     # scd.ingredients() / .components() / .brand_names() / ...
└── management/commands/rxnorm_pull.py
```

## Why no per-TTY models

You already have the concept+relation schema (mirroring RXNCONSO/RXNREL). BN,
IN, PIN, SCD, SCDG and SCDC are **rows** of `RxNormConcept` distinguished by
`tty`, linked through `RxNormConceptRelation.rela`. That is exactly how RxNorm
models it, so the pipeline populates those tables instead of adding six classes.
`traversal.py` gives you the typed-accessor ergonomics on top.

## Install

1. Drop the `rxgraph/` package next to your Django apps (importable as `rxgraph`).
2. In `rxgraph/pipeline.py`, confirm the schema mapping at the top:
    * `RELATION_SOURCE_FIELD` / `RELATION_TARGET_FIELD` — your relation's two FK
      field names (default `source` / `target`).
    * `RELATION_RELA_FIELD` — default `rela`.
    * `APP_LABEL` — leave `None` to auto-detect, or set it explicitly.
      The pipeline relies on these `RxNormConcept` fields, which match your
      migration: `rxcui`, `tty`, `name`, `active`, `attributes`, `synced_at`,
      `rxnorm_release`. Nodes upsert on `rxcui`; edges `get_or_create` on
      `(source, target, rela)`.

## Use it

```python
from rxgraph import add_concept, materialize_concept

add_concept("313782")  # returns the anchor RxNormConcept
result = materialize_concept("313782", release="2026-06")
print(result)  # 313782: 5 concepts (+5 new), 6 relations (+6 new)
```

CLI:

```
python manage.py rxnorm_pull 313782 --release 2026-06
python manage.py rxnorm_pull 161 --max-nodes 5000      # ingredient = big family
```

### Auto-run when an RxNormConcept is added (optional)

```python
# settings.py
RXGRAPH_AUTO_MATERIALIZE = True


# yourapp/apps.py
class YourAppConfig(AppConfig):
      def ready(self):
            from rxgraph import signals
            signals.connect()
```

On a *new* `RxNormConcept`, the pipeline runs in `transaction.on_commit` (so no
network call inside the triggering transaction) and is protected by a
thread-local guard so the child concepts it creates don't re-trigger it.

### Strength on edges

Pass `strength_resolver=` to write numerator/denominator onto the SCDC→IN
`has_ingredient` edge. The hook receives the source `Node` (it has `.name`,
e.g. `"Acetaminophen 325 MG"`); return a dict of relation defaults or `None`.
Wire it to your existing `StrengthParser` / `parse_strength`. Map the returned
keys to whatever your relation calls them (`numerator_value`, `numerator_unit`,
`denominator_value`, `denominator_unit`).

## How edges are built

`getAllRelatedInfo` discovers the family (one call), filtered to the six TTYs.
Then for each node, `getRelatedByRelationship` is called **one rela at a time**
and a target is kept only if it is in the family *and* its TTY matches the rule:

| source | rela                     | target |
|--------|--------------------------|--------|
| IN     | `has_tradename`          | BN     |
| IN     | `has_form`               | PIN    |
| SCDC   | `has_ingredient`         | IN     |
| SCDC   | `has_precise_ingredient` | PIN    |
| SCD    | `consists_of`            | SCDC   |
| SCD    | `has_ingredient`         | IN     |
| SCDG   | `inverse_isa`            | SCD    |
| SCDG   | `has_ingredient`         | IN     |

Each clinically meaningful edge is recorded once in a canonical forward
direction; inverses (`constitutes`, `ingredient_of`, `tradename_of`, …) are
derivable and not duplicated. Filtering by target TTY means an over-broad rule
can drop noise but never invent a wrong edge (verified: `SCDG inverse_isa` also
returns SCDF, which is dropped).

## Caveats

* **Ingredient anchors pull large families.** Anchoring on an IN materializes
  every SCD/SCDC/SCDG/BN for that ingredient and probes edges for each — many
  REST calls. `max_nodes` (default 2000) guards against runaways; for bulk
  loads a RXNREL.RRF import is more appropriate than the REST API.
* **SUPPRESS handling.** `active` is set `False` for SUPPRESS in {Y, O, E}; the
  raw flag is stored in `attributes["suppress"]`. Note the SUPPRESS=`E`
  (quantified) gotcha you hit before: the unquantified base SCD that groups
  volume variants is filtered by active-scoped endpoints, so it won't appear in
  `allrelated` and won't be linked here. If you need that grouping edge, resolve
  it via `historystatus` / RRF separately.
* **SCDG→IN rela.** The dose-form-group→ingredient edge is probed as
  `has_ingredient`. If a release uses a different rela there, the IN node is
  still created and still linked through `SCD has_ingredient IN`; only the
  direct SCDG→IN convenience edge would be absent.
* **Network egress.** `rxnav.nlm.nih.gov` must be reachable from wherever the
  pipeline runs (web/worker process), and not just the DB host.
