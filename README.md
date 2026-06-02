# Translator Performance Test Runner

This load tester drives a stepped ramp of concurrent users against a single
NCATS Translator component and reports the **max sustainable concurrency** (the
"knee") — the highest load where the service still meets a latency/error SLO.

The Translator stack cascades **ARS → ARAs → KPs**, so a run targets exactly
**one** layer at a time (testing a higher layer already loads everything beneath
it). All three workflows are wired up: **Retriever (KP)**, **Shepherd (ARA)**,
and the asynchronous **ARS**.

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

# ARS — async submit/poll/merge of inferred queries (host = the ARS API base)
run_performance_tests --targets ars \
    --host https://ars.ci.transltr.io/ars/api \
    --csv-prefix run1
```

- `--targets` selects the layer: `kps` (Retriever), `aras` (Shepherd), or `ars`.
- `--host` is **required** — the base URL of the target service. For `kps`/`aras`
  the `/query` path is appended; for `ars` the host is the API base and the tool
  uses `/submit` then `/messages/{pk}`.
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
- `<prefix>_ars_health.csv` — **ARS only**: per-stage health signals (see below)
- a printed summary table ending in the headline **max sustainable concurrency**

### ARS async workflow and health signals

The `ars` target is asynchronous: each logical query is `POST /submit` → poll
`GET /messages/{pk}?trace=y` (every `poll_interval_s`, capped at `max_poll_s`,
default 15 min) until `status` is `Done`/`Error` → fetch `GET /messages/{merged_pk}`
and count `fields.data.message.results`. Latency is the wall-clock submit→terminal
time; one measurement is recorded per logical query (the submit/poll/merge calls
appear separately in Locust's own table but aren't double-counted).

Because the ARS is what real users hit, the run also captures health signals to
flag silent downstream breakage, written to `<prefix>_ars_health.csv` and
`summary.json` (`ars_health` + a human-readable `red_flags` list):

- **result-count variation** (min/mean/max + coefficient of variation) across
  identical queries;
- **zero-result `Done`** count — a `Done` with 0 results is treated as a
  **failure** (counts against the error rate and the knee) and flagged;
- **response size** (merged-message bytes, mean/max);
- **result drop under load** — flagged when the mean result count falls sharply
  as concurrency rises across stages.

## Tuning notes

- **Swap the CURIEs.** The corpus in `translator_load_tester/trapi_corpus.py`
  uses a few real MONDO/CHEBI entities; replace them with entities your target
  service actually knows about, or queries return empty and won't reflect real
  cost.
- **Tier is per query (Retriever only).** Retriever exposes `parameters.tier`
  (0 or 1) to pick its backend graph. `RETRIEVER_CORPUS` pairs multi-hop shapes
  with tier 0 and single-hop shapes with tier 1. ARA queries carry no tier.
- **Shepherd/ARS send inferred + bypass_cache, mixing MVP1 and MVP2.**
  `SHEPHERD_CORPUS` (also used for `ars`) holds creative-mode queries
  (`knowledge_type: "inferred"`, `bypass_cache: true`) split evenly between two
  Translator templates, with entities varied per request to spread load and avoid
  cache-warming. `by_qtype.csv` breaks out latency per template/tier.
  - **MVP1 — "what treats disease X?"** (`chemical -[treats]-> disease`): the
    pinned disease is sampled from size-tiered pools (heavy/medium/light), so
    cost tracks answer-set size. Heavy is a curated list of common disease hubs;
    the long-tail pool is ~1000 real MONDO CURIEs in `curie_list.json`.
  - **MVP2 — chemical⇄gene "affects"** (`biolink:affects`, `inferred`, with
    `object_aspect`/`object_direction` qualifiers): both edge directions
    (chemical→gene and gene→chemical), with the gene (curated `GENES` pool) and
    qualifier combo varied per request.

  These are far heavier than KP lookups, so the `aras` target ships a gentler
  ramp and a looser `p99_slo_ms` (see `config.py`). Tune the per-template weights
  and entity pools (`HEAVY_DISEASES`, `GENES`, the tiers) to your real traffic.
- **ARS reuses the Shepherd corpus** (`ARS_CORPUS = SHEPHERD_CORPUS`) — the same
  inferred query the ARS fans out to its ARAs. Its poll cadence and per-query
  timeout (`poll_interval_s`, `max_poll_s`) are tunable in `config.py`.
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
