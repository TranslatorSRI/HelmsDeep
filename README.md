# Translator Performance Test Runner

This load tester drives a stepped ramp of concurrent users against a single
NCATS Translator component and reports the **max sustainable concurrency** (the
"knee") — the highest load where the service still meets a latency/error SLO.

The Translator stack cascades **ARS → ARAs → KPs**, so a run targets exactly
**one** layer at a time (testing a higher layer already loads everything beneath
it). Today the **Retriever (KP)** and **Shepherd (ARA)** workflows are wired up;
ARS is coming in a later phase.

## Install

```bash
pip install -e .          # Python >= 3.12; installs locust
```

## Run a workflow

```bash
# Retriever (KP) — sync lookup queries, scalar parameters.tier per query
run_performance_tests --targets kps \
    --host https://your-retriever-service.example.org \
    --csv-prefix run1

# Shepherd (ARA) — sync creative-mode (inferred) queries, cache bypassed
run_performance_tests --targets aras \
    --host https://your-ara-service.example.org \
    --csv-prefix run1
```

- `--targets` selects the layer: `kps` (Retriever) or `aras` (Shepherd). `ars`
  is accepted but prints "not yet implemented" and exits non-zero.
- `--host` is **required** — the base URL of the target service. The endpoint
  path (`/query`) is appended automatically.
- `--csv-prefix` is optional; it falls back to the `LOCUST_CSV_PREFIX` env var,
  then to `trapi_run`.

The load profile (users, spawn rate, duration) is driven by the `StepLoad` shape,
**not** by CLI flags — so there is intentionally no `-u/-r/-t`. The ramp and knee
threshold are **per target** in `translator_load_tester/config.py` (`stages` and
`p99_slo_ms`), since cost profiles differ wildly by layer; edit them there.

## Outputs

Written to the working directory by the standalone/master node:

- `<prefix>_stages.csv` — one row per stage (overall metrics)
- `<prefix>_by_qtype.csv` — one row per (stage, query type)
- `<prefix>_summary.json` — config (including which `target` was measured), all
  stages, and the chosen knee
- a printed summary table ending in the headline **max sustainable concurrency**

## Tuning notes

- **Swap the CURIEs.** The corpus in `translator_load_tester/trapi_corpus.py`
  uses a few real MONDO/CHEBI entities; replace them with entities your target
  service actually knows about, or queries return empty and won't reflect real
  cost.
- **Tier is per query (Retriever only).** Retriever exposes `parameters.tier`
  (0 or 1) to pick its backend graph. `RETRIEVER_CORPUS` pairs multi-hop shapes
  with tier 0 and single-hop shapes with tier 1. ARA queries carry no tier.
- **Shepherd sends inferred + bypass_cache.** `SHEPHERD_CORPUS` holds creative
  "what treats disease X?" queries with `knowledge_type: "inferred"` and
  `bypass_cache: true`, so the run measures reasoning cost rather than cache
  hits. These are far heavier than KP lookups, so the `aras` target ships a
  gentler ramp and a looser `p99_slo_ms` than `kps` (see `config.py`).
- **Adjust corpus weights** in `RETRIEVER_CORPUS` / `SHEPHERD_CORPUS` to match
  your traffic mix.

## Running the engine directly

You can also invoke the locustfile without the CLI (defaults to the `kps`
target):

```bash
LOCUST_CSV_PREFIX=run1 \
locust -f translator_load_tester/trapi_loadtest.py --headless \
    --host https://your-retriever-service.example.org
```

The output-file prefix comes from the `LOCUST_CSV_PREFIX` env var (locust has no
`--csv-prefix` flag). Set `LOADTEST_TARGET` to choose a different (implemented)
layer.
