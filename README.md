# Translator Performance Test Runner

This load tester drives a stepped ramp of concurrent users against a single
NCATS Translator component and reports the **max sustainable concurrency** (the
"knee") — the highest load where the service still meets a latency/error SLO.

The Translator stack cascades **ARS → ARAs → KPs**, so a run targets exactly
**one** layer at a time (testing a higher layer already loads everything beneath
it). Today the **Retriever (KP)** workflow is wired up; Shepherd (ARA) and ARS
are coming in a later phase.

## Install

```bash
pip install -e .          # Python >= 3.12; installs locust
```

## Run the Retriever (KP) workflow

```bash
run_performance_tests --targets kps \
    --host https://your-retriever-service.example.org \
    --csv-prefix run1
```

- `--targets kps` selects the Retriever layer. `aras` and `ars` are accepted but
  print "not yet implemented" and exit non-zero.
- `--host` is **required** — the base URL of the Retriever service. The endpoint
  path (`/query`) is appended automatically.
- `--csv-prefix` is optional; it falls back to the `LOCUST_CSV_PREFIX` env var,
  then to `trapi_run`.

The load profile (users, spawn rate, duration) is driven by the `StepLoad` shape
in `translator_load_tester/trapi_loadtest.py`, **not** by CLI flags — so there is
intentionally no `-u/-r/-t`. To change the ramp, edit the `STAGES` table.

## Outputs

Written to the working directory by the standalone/master node:

- `<prefix>_stages.csv` — one row per stage (overall metrics)
- `<prefix>_by_qtype.csv` — one row per (stage, query type)
- `<prefix>_summary.json` — config (including which `target` was measured), all
  stages, and the chosen knee
- a printed summary table ending in the headline **max sustainable concurrency**

## Tuning notes

- **Swap the CURIEs.** The corpus in `translator_load_tester/trapi_corpus.py`
  uses a few real MONDO/CHEBI entities; replace them with entities your Retriever
  actually knows about, or lookups return empty and won't reflect real cost.
- **Tier is per query.** Retriever exposes `parameters.tier` (0 or 1) to pick its
  backend graph. The Retriever corpus pairs multi-hop shapes with tier 0 and
  single-hop shapes with tier 1.
- **Adjust corpus weights** in `RETRIEVER_CORPUS` to match your traffic mix.

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
