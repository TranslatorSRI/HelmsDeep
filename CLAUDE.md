# CLAUDE.md

Guidance for working in this repo. Keep claims here grounded in the actual code —
if you change behavior, update this file.

## Purpose

`HelmsDeep` (HTTP Endpoint Load Measurement System, Determining Each Endpoint's
Performance) measures, **for each NCATS Translator service, the maximum
sustainable concurrency** — how many concurrent users the service can feasibly
handle before it violates a latency/error SLO. The Translator stack has three
classes of service (Retriever/**KPs**, Shepherd/**ARAs**, and the **ARS**), each
of which speaks a slightly different protocol and expects a different kind of
TRAPI query. This tool sends each component the query type it expects and reports
a single headline number per run: the *knee*.

> The architecture below is now **implemented**: all three layers (KPs/ARAs/ARS)
> are runnable through a real `helmsdeep` CLI, config is per-target,
> and the corpus is segmented per component. A few refinements remain — see
> **Status & what's left**. Check the **Current code map** for exactly what each
> file does before assuming behavior.

## The metric we produce: the "knee"

A run drives a **stepped ramp** of concurrent users (the per-target `stages`
table) and, for each stage, records RPS, mean/p50/p95/p99 latency, and error
rate. The **knee** is the highest stage where **both**:

- `p99 <= P99_SLO_MS` (per-target `p99_slo_ms`; e.g. 60 000 ms for KPs, larger
  for the slower ARA/ARS layers), **and**
- `error_rate <= MAX_ERROR_RATE` (shared, default 0.01 = 1%)

The knee's *effective concurrency* — computed via Little's Law,
`concurrency = rps * mean_latency_seconds` — is the deliverable: the max
sustainable concurrency for that service.

Implementation: `_stage_stats()` computes per-stage metrics and the Little's-Law
concurrency; the knee-selection loop lives in `on_test_stop()` in
`helmsdeep/trapi_loadtest.py`. If no stage meets the SLO, the service
saturated below the first stage (or the SLO is too strict).

## Translator component model

Each component class has its own (a) endpoint + protocol and (b) expected query
subset. The tool must route the correct corpus + protocol per target.

| Component | Role | Protocol it expects | Query it expects |
|-----------|------|---------------------|------------------|
| **Retriever / KP** | Knowledge Provider (one service: **Retriever**) | sync `POST /query` (TRAPI) | `lookup`-mode queries; set `parameters.tier` to 0 or 1 (see below). Cheapest layer. |
| **Shepherd / ARAs** | Reasoning agents | sync `POST /query` (or `/asyncquery`) | `inferred`/creative-mode queries. Expensive. |
| **ARS** | Autonomous Relay System | **async**: `POST /submit` → poll `GET /messages/{pk}` until `Done`/`Error` → fetch merged results | `inferred` query; very long-running (minutes–~1 hr). |

**Pathfinder** is an additional, heavier query class for the **ARA and ARS layers
only** (never KPs). It pins **two** endpoint entities and asks for connecting
paths via a `paths` map in the query_graph (not `edges`; no `knowledge_type`). It
gets its **own run types** — `aras_pathfinder` (sync, via the ARA `/query`) and
`ars_pathfinder` (async, via the ARS submit/poll/merge) — so it's never mixed into
the inferred corpus. Same protocols/endpoints as `aras`/`ars`, just a different
corpus + a gentler ramp / looser SLO.

### Retriever and the `tier` parameter

In the **current** system the KP layer is a single service, **Retriever** — not the
~40+ independent KP endpoints of the old architecture (those survive only in git
history; see **Reusable assets**). Retriever exposes a
`message["parameters"]["tier"]` field that selects the backend graph it queries:

- **Tier 0** — backend graph that can handle **arbitrary multi-hop** queries.
- **Tier 1** — backend graph that handles **mostly single-hop** queries.

So when characterizing Retriever, `tier` is a first-class load dimension: a Tier 0
multi-hop query is a heavier cost profile than a Tier 1 single-hop. A KP corpus
should set `tier` **deliberately per query** (and pair multi-hop shapes with Tier 0,
single-hop shapes with Tier 1) rather than sending one fixed value for everything.

### Layering rule — a run targets exactly ONE layer

The stack **cascades: ARS → ARAs → KPs.** A query to the ARS fans out to the ARAs,
which fan out to the KPs. So load-testing the ARS already exercises everything
downstream.

**Therefore the tool hits one component class per run, mutually exclusively —
never all layers at once.** Running an ARA test *and* an ARS test at the same time
would double-load the ARAs (and the KPs beneath them) and corrupt both
measurements. The operator picks the single layer they want to characterize; the
lower layers are loaded only as a side effect of that one run. There is no
"test everything simultaneously" mode, and there should never be one.

The ARS contract is **fundamentally different** from KP/ARA: it is asynchronous
(submit → poll → merge), not a single blocking `POST /query`. Any ARS support must
model that submit/poll loop rather than reuse the sync request path.

## Architecture

```
target registry   →  per-component dispatch  →  Locust step-load engine  →  per-service report
(config.TARGETS:     (TRAPIUser picks protocol   (stages ramp, knee         (stages.csv,
 endpoint/protocol/   + corpus for the chosen     detection — shared         by_qtype.csv,
 corpus/stages/SLO)   layer via LOADTEST_TARGET)   across layers)            summary.json[, ars_health])
```

The Locust step-load engine in `trapi_loadtest.py` is the **reusable core** and
is shared across layers. What varies by component is only (1) the request
protocol (sync `POST` vs ARS submit/poll/merge) and (2) which corpus subset is
sent (`lookup` for KPs, `inferred` for ARAs/ARS) — both selected from
`config.TARGETS` by the `LOADTEST_TARGET` the CLI sets.

## Current code map (what exists today)

Package `helmsdeep/`:

- **`config.py`** — the target registry. `TARGETS` maps each `--targets` value
  (`kps`/`aras`/`ars` plus the Pathfinder run types `aras_pathfinder`/
  `ars_pathfinder`) to a component config: `label`, `endpoint`, `corpus`
  (key into the corpus module), `protocol` (`sync`/`async`), `implemented`, and
  per-target `stages` + `p99_slo_ms` + an optional `cooldown_s` (a quiet gap
  between stages — users ramp to 0 so slow in-flight queries drain into the stage
  that launched them instead of bleeding into the next; defaults to 0, set on the
  expensive ARA/ARS/Pathfinder targets). Async targets (`ars`, `ars_pathfinder`) add
  `messages_endpoint`, `poll_interval_s`, `max_poll_s`, and an optional
  `completion_max_poll_s` (>= `max_poll_s`; the extended cap for the completion
  sidecar — how long a background poller keeps watching a timed-out query to see if
  it *eventually* finishes; defaults to `max_poll_s` = off). The `*_pathfinder` targets
  reuse the ARA/ARS endpoints + protocols but point at the `pathfinder` corpus and
  ship a gentler ramp + looser SLO (pinned-two-endpoint path-finding is the
  heaviest query class). `MAX_ERROR_RATE` is shared; `DEFAULT_TARGET="kps"`.
  Per-target ramps/SLOs live here because cost profiles differ wildly by layer.
- **`cli.py`** — the `helmsdeep` entry point (registered in
  `setup.py` `console_scripts`). Parses `--targets` (required, one layer),
  `--host` (required), `--csv-prefix`; rejects not-yet-`implemented` targets;
  sets `LOADTEST_TARGET` (+ `LOCUST_CSV_PREFIX`) and launches
  `python -m locust -f trapi_loadtest.py --headless --host …`.
- **`trapi_loadtest.py`** — the measurement engine (Locust). The component under
  test is chosen by `LOADTEST_TARGET`; `ENDPOINT`, `CORPUS`, `STAGES`,
  `P99_SLO_MS`, and the async knobs (`PROTOCOL`, `MESSAGES_PATH`,
  `POLL_INTERVAL_S`, `MAX_POLL_S`) plus `COOLDOWN_S` are all derived from
  `config.TARGETS[...]`.
  - `StepLoad(LoadTestShape)` — drives the `stages` ramp and marks the active stage.
    When `COOLDOWN_S` is set it inserts a drain gap between stages (`tick()`
    returns 0 users in the gap and calls `COLLECTOR.end_active_stage()` to freeze
    the just-finished stage's end time); an `@events.init` listener sets
    `stop_timeout = REQUEST_TIMEOUT` so a slow in-flight query finishes rather than
    being killed when users ramp to 0.
  - `StageCollector` / `COLLECTOR` — buckets every completed request into the
    stage active when it *finished* (per-stage, per-`qtype`). Records each stage's
    wall-clock `stage_started` / `stage_ended` (the latter is `setdefault`, so a
    cooldown freeze isn't overwritten by the next `mark_stage`). `record()` also
    takes optional ARS signals: `status`, `result_count`, `response_bytes`.
  - `_stage_stats()` — per-stage RPS, percentiles, error rate, Little's-Law
    concurrency. `_ars_stage_health()` — per-stage ARS health row.
  - `TRAPIUser(HttpUser)` — dispatches on `PROTOCOL`: `_run_sync()` (KP/ARA: one
    blocking `POST`) or `_run_ars()` (submit → poll `/messages/{pk}` with
    `gevent.sleep` until `Done`/`Error`/timeout → `_fetch_merged()` counts
    `fields.data.message.results`). One `COLLECTOR.record` per logical query. When a
    query blows `MAX_POLL_S` (already recorded as a Timeout failure — main stats
    unchanged), `_run_ars` spawns a detached `_extended_poll` greenlet that keeps
    polling to `COMPLETION_MAX_POLL_S` and appends one `COLLECTOR.record_completion`
    row (end-to-end time + whether it `finished`); queries that finish within
    `MAX_POLL_S` record their completion row inline.
  - `on_test_stop()` — drains any in-flight completion greenlets (bounded), finds
    the knee, writes `stages.csv` (incl. a `stage_start` ISO-8601-UTC column) /
    `by_qtype.csv` / `summary.json`, and (async only) `ars_health.csv`, the
    `ars_completion.csv` sidecar, plus `red_flags` + a `completion` roll-up in the
    summary + printed block.
- **`trapi_corpus.py`** — the per-component query corpuses:
  - `_qg(nodes, edges, tier=None, bypass_cache=None)` — TRAPI envelope; adds
    scalar `parameters.tier` (KP-only) or top-level `bypass_cache` (ARA/ARS) only
    when supplied.
  - **`RETRIEVER_CORPUS`** — `lookup`-mode KP builders, each pinning its own
    `tier` (multi-hop→0, single-hop→1): `one_hop_lookup_pinned`,
    `one_hop_lookup_open`, `one_hop_no_predicate`, `two_hop_lookup`,
    `batch_lookup`, `malformed_query`.
  - **`SHEPHERD_CORPUS`** (also used as **`ARS_CORPUS`**) — `inferred` +
    `bypass_cache` creative queries, an even MVP1/MVP2 split (50/50), entity
    varied per request:
    - **MVP1** "what treats disease X?" (`chemical-[treats]->disease`), disease
      sampled from size-tiered pools via `mvp1_heavy`/`mvp1_medium`/`mvp1_light`
      (10/15/25 = the 50% MVP1 half, tiered 20/30/50 within it).
    - **MVP2** chemical `biolink:affects` gene, with object aspect/direction
      qualifiers on the gene. The edge is **always** oriented chemical(subject)→
      gene(object) (matching the Translator TestHarness `generate_query.py`); the
      two variants differ only by which endpoint is pinned —
      `mvp2_chem_affects_gene` (pinned gene, open chemical) /
      `mvp2_chem_affects_open_gene` (pinned chemical, open gene) (25/25). Aspect is
      the canonical `activity_or_abundance`; direction + entity sampled per request.
  - **`PATHFINDER_CORPUS`** — the Pathfinder run type (ARA/ARS only, selected by
    `aras_pathfinder`/`ars_pathfinder`). `pathfinder_drug_disease` pins **two**
    endpoints (a drug + a disease, sampled per request from `CHEM_DISEASE_PAIRS`)
    and asks for connecting paths via a `paths` map in the query_graph — built by
    `_pathfinder_qg(nodes, paths)` (`nodes` + empty `edges` + `paths`; no
    `knowledge_type`, no `tier`; `bypass_cache=True`). Most intensive query class.
  - Entity pools: `HEAVY_DISEASES` (curated hubs) + `LONG_TAIL_DISEASES` from
    **`curie_list.json`** (~1000 real MONDO CURIEs, shipped via `package_data`),
    a curated `GENES` pool (NCBIGene), and curated drug↔disease `CHEM_DISEASE_PAIRS`.
    `corpus_for(name)` returns the right list.

For KPs, cost is driven by query-graph **shape** (hops, mode, pinned vs open,
batch, predicate). For ARA/ARS creative queries, the dominant cost driver is the
**pinned disease's answer-set size**, which is why that corpus varies the entity.

## Status & what's left

Implemented: component awareness, the `helmsdeep` CLI + entry point,
per-target stages/SLO, segmented corpuses, scalar `parameters.tier` per KP query,
the ARS async submit/poll/merge user, ARS health metrics + red flags, and the
tiered inferred disease mix.

Remaining refinements (not yet done — don't assume these exist):

- **Medium vs light tiers aren't calibrated.** `inferred_medium` and
  `inferred_light` currently draw from the same `LONG_TAIL_DISEASES` pool; split
  it by measured answer-set size (a one-time profiling pass) for true separation.
- **No per-ARA child-result breakdown.** ARS health treats the merged message as
  a whole; the `trace=y` response exposes children, so per-agent health is possible.
- **No full multi-endpoint registry.** Retriever is a single service today; the
  old per-KP/ARA URL registries survive only in git history (see below).
- **Config is env/registry-driven, not file-driven.** Stages/SLO are edited in
  `config.py`; there's no external config-file or full CLI override yet.
- **No `results/` output convention** — outputs land in the working directory.

## Reusable assets in git history

The "Start the rewrite" commit (`9fe6cd0`) deleted the old per-service scripts, but
they contain assets worth recovering for the roadmap below. Retrieve with
`git show <commit>:<path>`:

- `git show b912968:kps.json` — registry of ~40+ KP endpoints (`*.ci.transltr.io`),
  some with per-KP `predicates` overrides.
- `git show b912968:aras.json` — registry of ARA endpoints (Aragorn, ARAX, BTE,
  mediKanren, CQS, imProving Agent).
- `git show b912968:ars_stress_test.py` — the ARS submit/poll/merge workflow
  (base URL `https://ars.ci.transltr.io/ars/api`).
- `git show b912968:generate_message.py` — TRAPI message builders, including batch
  handling (`set_interpretation: "BATCH"`) and `inferred` ARA queries.
- `git show b912968:curie_list.json` — ~1000 MONDO disease CURIEs. **Already
  restored** into the package as `helmsdeep/curie_list.json` (the
  long-tail disease pool for the inferred corpus).

## How to run

```bash
pip install -e .          # Python >= 3.12; installs locust

# One layer per run (kps | aras | ars); --host required.
helmsdeep --targets kps --host https://your-retriever.example.org --csv-prefix run1
helmsdeep --targets aras --host https://your-ara.example.org --csv-prefix run1
helmsdeep --targets ars  --host https://ars.ci.transltr.io/ars/api --csv-prefix run1

# Pathfinder is its own (heavier) run type, ARA/ARS only:
helmsdeep --targets aras_pathfinder --host https://your-ara.example.org --csv-prefix pf1
helmsdeep --targets ars_pathfinder  --host https://ars.ci.transltr.io/ars/api --csv-prefix pf1
```

- The `LoadTestShape` (`StepLoad`) **drives users, spawn rate, and duration**, so
  there is no `-u` / `-r` / `-t`. Tune the ramp via the per-target `stages` in
  `config.py`.
- `--csv-prefix` is optional; it falls back to the `LOCUST_CSV_PREFIX` env var,
  then to `trapi_run`.
- You can also run the locustfile directly (`locust -f helmsdeep/
  trapi_loadtest.py --headless --host …`), selecting the layer with the
  `LOADTEST_TARGET` env var (defaults to `kps`). Note locust has no
  `--csv-prefix` flag — set `LOCUST_CSV_PREFIX` instead.
- Outputs (written by the master/standalone node only):
  `<prefix>_stages.csv`, `<prefix>_by_qtype.csv`, `<prefix>_summary.json` (+
  `<prefix>_ars_health.csv`, the `<prefix>_ars_completion.csv` sidecar, and a
  `red_flags` list + `completion` roll-up for the `ars` target), plus a printed
  summary table with the knee.

## Conventions & gotchas

- **The shape owns concurrency.** Tune load by editing the per-target `stages`
  in `config.py`, not CLI flags.
- **Cooldown drains, it doesn't bleed.** With `cooldown_s` set, the gap between
  stages ramps users to 0; the just-finished stage's end time is frozen so its
  `duration_s`/RPS reflect the active window, and a slow query still running
  drains into *that* stage (via `stop_timeout`), keeping the next stage clean.
- **Closed-loop load.** `TRAPIUser.wait_time = constant(0)` — no think time; users
  hammer the endpoint as fast as responses return.
- **Don't trust Locust's blended aggregate during a ramp.** We bucket per stage in
  `StageCollector` precisely because an aggregate p99 would mix easy early stages
  with saturated late ones.
- **`malformed_query` 4xx is success.** A 4xx on the malformed query is treated as
  a valid measurement of the error path; only 5xx counts as a failure. See the
  `TRAPIUser.query` handling.
- **Long timeouts on purpose.** `REQUEST_TIMEOUT = 210` s because TRAPI queries are
  slow; ARS runs are far longer still (minutes–~1 hr) and need the async model.
- **gevent concurrency.** Locust uses gevent green-threads; avoid blocking calls in
  the user path. The ARS poll loop uses `gevent.sleep`, **never** `time.sleep`.
- **ARS submit/poll/merge is one logical measurement.** `_run_ars` issues several
  HTTP calls (named `ars_submit`/`ars_poll`/`ars_merge` — they show in Locust's
  own table as per-step diagnostics) but records exactly one `COLLECTOR.record`
  per logical query, with latency = wall-clock submit→terminal. It also fires a
  single synthetic `ars_query` Locust request event carrying that same full
  wall-clock, so Locust's native stats table shows the true per-query time
  (otherwise the only ARS rows would be the individual sub-calls — e.g. the
  `ars_merge` GET, which times just the final merge fetch, not the whole query).
  A `Done` that returns **0 results counts as a failure** (and raises a red
  flag); a non-terminal status past `max_poll_s` is a `Timeout` failure.
- **Completion tracking is a sidecar, not a metric change.** `max_poll_s` stays the
  failure threshold for the main stats and the knee — a query not terminal by then
  is a `Timeout` failure, exactly as before. Separately, `completion_max_poll_s`
  (>= `max_poll_s`, default 10 min on `ars`/`ars_pathfinder`) lets a **detached
  background greenlet** keep polling that same query to see if it *eventually*
  finishes; the outcome (end-to-end time + `finished`/`within_slo`/`status`) goes
  only to `<prefix>_ars_completion.csv` and the `completion` summary roll-up. It
  never feeds the per-stage stats, the `ars_query` event, or the knee, so existing
  measurements are unchanged. The greenlets are drained (bounded by the extra
  budget) in `on_test_stop`; queries still unfinished at shutdown are simply absent
  from the sidecar. This separates *slow* (finished after the SLO) from *broken*
  (never finished).
- **Inferred corpus mixes MVP1 + MVP2 and varies entities per request.** MVP1
  (`mvp1_heavy/medium/light`, treats-disease) samples a tiered disease; MVP2
  (`mvp2_chem_affects_gene`/`mvp2_chem_affects_open_gene`, a chemical→gene
  `affects` edge with the gene-pinned and chemical-pinned variants) samples the
  pinned entity + direction. The edge orientation is always chemical(subject)→
  gene(object) and the aspect qualifier is always `activity_or_abundance`. The
  per-request variation covers the real cost surface and avoids warming caches.
  MVP1 medium and light share the long-tail pool until calibrated (see Status &
  what's left).
- **Environments & TRAPI versions vary per service.** Endpoints live across
  `*.ci.transltr.io`, `*.test.transltr.io`, and prod, and individual services pin
  different TRAPI versions in their URL paths. Target deliberately.
- **Swap the CURIEs.** The KP corpus uses a few real MONDO/CHEBI entities and the
  inferred corpus draws diseases from `curie_list.json`; replace/extend them with
  entities the target service actually knows about, or queries return empty and
  won't reflect real cost.

## Roadmap (broader repo, next phases)

Done in earlier phases: per-component adapter + `--targets` CLI entry point
(one layer per run — there is intentionally no "all" mode, which would
double-load shared downstream services per the layering rule), the ARS async
submit→poll→merge user, per-target config, and a README for human onboarding.

Remaining, ordered so a future session can pick up where this leaves off:

a. **Calibrate the inferred tiers.** Profile each disease once (sort by merged
   result count) and split `LONG_TAIL_DISEASES` into real medium/light pools.
b. **Per-ARA health breakdown** for ARS, parsing the `trace=y` children so a
   red flag can name *which* downstream agent dropped answers.
c. **Restore the full endpoint registry as config** (per-KP/ARA URLs +
   predicate/query overrides) from the git-history assets above, if/when the
   stack returns to multiple independent KP/ARA endpoints.
d. **Make config file-driven** (external config file / full CLI overrides for
   stages, SLOs, poll knobs) instead of editing `config.py`.
e. **Adopt a `results/` output convention** so each service's reports land in a
   predictable, per-service location.
