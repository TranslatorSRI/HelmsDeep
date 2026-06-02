# CLAUDE.md

Guidance for working in this repo. Keep claims here grounded in the actual code —
if you change behavior, update this file.

## Purpose

`LoadTester` measures, **for each NCATS Translator service, the maximum
sustainable concurrency** — how many concurrent users the service can feasibly
handle before it violates a latency/error SLO. The Translator stack has three
classes of service (Retriever/**KPs**, Shepherd/**ARAs**, and the **ARS**), each
of which speaks a slightly different protocol and expects a different kind of
TRAPI query. This tool sends each component the query type it expects and reports
a single headline number per run: the *knee*.

> This file describes the **target architecture** the repo is being reshaped
> toward. The current code (`trapi_loadtest.py` + `trapi_corpus.py`) is the
> measurement engine that target is built around, but several pieces below do not
> exist yet — see **Gap to target**. Don't assume a feature exists just because
> it's described here; check the **Current code map**.

## The metric we produce: the "knee"

A run drives a **stepped ramp** of concurrent users (the `STAGES` table) and, for
each stage, records RPS, mean/p50/p95/p99 latency, and error rate. The **knee** is
the highest stage where **both**:

- `p99 <= P99_SLO_MS` (default 60 000 ms), **and**
- `error_rate <= MAX_ERROR_RATE` (default 0.01 = 1%)

The knee's *effective concurrency* — computed via Little's Law,
`concurrency = rps * mean_latency_seconds` — is the deliverable: the max
sustainable concurrency for that service.

Implementation: `_stage_stats()` computes per-stage metrics and the Little's-Law
concurrency; the knee-selection loop lives in `on_test_stop()` in
`translator_load_tester/trapi_loadtest.py`. If no stage meets the SLO, the service
saturated below the first stage (or the SLO is too strict).

## Translator component model (target)

Each component class has its own (a) endpoint + protocol and (b) expected query
subset. The tool must route the correct corpus + protocol per target.

| Component | Role | Protocol it expects | Query it expects |
|-----------|------|---------------------|------------------|
| **Retriever / KPs** | Knowledge Providers | sync `POST /query` (TRAPI) | `lookup`-mode queries (pinned/open/batch one-hops). Cheapest. ~40+ endpoints. |
| **Shepherd / ARAs** | Reasoning agents | sync `POST /query` (or `/asyncquery`) | `inferred`/creative-mode queries. Expensive. |
| **ARS** | Autonomous Relay System | **async**: `POST /submit` → poll `GET /messages/{pk}` until `Done`/`Error` → fetch merged results | `inferred` query; very long-running (minutes–~1 hr). |

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

## Target architecture

```
endpoint registry  →  per-component adapter  →  Locust step-load engine  →  per-service report
(KPs/ARAs/ARS URLs)   (picks protocol +          (STAGES ramp, knee         (stages.csv,
                       corpus subset for          detection — reused          by_qtype.csv,
                       the chosen layer)          as-is)                      summary.json)
```

The Locust step-load engine in `trapi_loadtest.py` is the **reusable core** and
should not need to change much per component. What varies by component is only
(1) the request protocol (sync POST vs ARS submit/poll) and (2) which corpus
subset is sent (`lookup` for KPs, `inferred` for ARAs/ARS).

## Current code map (what exists today)

Two files under `translator_load_tester/`:

- **`trapi_loadtest.py`** — the measurement engine (Locust):
  - `StepLoad(LoadTestShape)` — drives the `STAGES` ramp and tells the collector
    which stage is active.
  - `StageCollector` / `COLLECTOR` — buckets every completed request into the
    stage that was active when it *finished* (per-stage, per-`qtype`).
  - `_stage_stats()` — per-stage RPS, latency percentiles, error rate, Little's-Law
    concurrency.
  - `TRAPIUser(HttpUser)` — picks a weighted query from the corpus and `POST`s it.
  - `on_test_stop()` — finds the knee and writes the output files.
  - Module-level config block: `ENDPOINT`, `REQUEST_TIMEOUT`, `P99_SLO_MS`,
    `MAX_ERROR_RATE`, `STAGES`, `CSV_PREFIX`.
- **`trapi_corpus.py`** — the query corpus:
  - `_qg(nodes, edges)` — wraps a query graph into the TRAPI `{message, parameters}`
    envelope.
  - 7 builder functions, each returning a TRAPI query of a different *shape*:
    `one_hop_lookup_pinned`, `one_hop_lookup_open`, `one_hop_inferred`,
    `one_hop_no_predicate`, `two_hop_lookup`, `batch_lookup`, `malformed_query`.
  - `CORPUS` — list of `(qtype_label, builder, weight)`; weights are relative.
  - Tunable real CURIEs (MONDO/CHEBI), e.g. `T2D`, `METFORMIN`, `ALZHEIMERS`.

Cost in TRAPI is driven by query-graph **shape**, not text length. The dimensions
that matter: hops, mode (`lookup` vs `inferred`), constraint (pinned vs open),
batch size, predicate specificity. See the module docstring in `trapi_corpus.py`.

## Gap to target (what's missing — do not assume these exist)

- **No component awareness.** There is a single hardcoded `ENDPOINT = "/query"`
  and a single `--host`. KPs/ARAs/ARS are not distinguished.
- **No endpoint registry and no `--targets` selection.** `README.md` advertises
  `run_performance_tests --targets kps`, but `setup.py` defines **no**
  `console_scripts` entry point — that command does not exist yet.
- **No ARS async submit/poll support.** The Locust user only does a blocking
  `POST`. The ARS submit → poll → merge workflow is unimplemented.
- **Corpus is not segmented by component contract.** KPs want `lookup`; ARAs/ARS
  want `inferred`. Today every run draws from the same mixed `CORPUS`.
- **Config is module-level constants**, not CLI/env/file driven (only
  `CSV_PREFIX` reads an env var, `LOCUST_CSV_PREFIX`).

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
- `git show b912968:curie_list.json` — ~1000 MONDO disease CURIEs for batch tests.

## How to run (today)

```bash
pip install -e .          # Python >= 3.12; installs locust

locust -f translator_load_tester/trapi_loadtest.py --headless \
    --host https://your-trapi-service.example.org \
    --csv-prefix run1
```

- The `LoadTestShape` (`StepLoad`) **drives users, spawn rate, and duration**, so
  do **NOT** pass `-u` / `-r` / `-t` — they would be ignored or fight the shape.
- `--csv-prefix` is optional; it falls back to the `LOCUST_CSV_PREFIX` env var,
  then to `trapi_run`.
- Outputs (written by the master/standalone node only):
  `<prefix>_stages.csv`, `<prefix>_by_qtype.csv`, `<prefix>_summary.json`, plus a
  printed summary table with the knee.

> The `run_performance_tests --targets kps` command in `README.md` is
> **aspirational** — it is not wired up yet (see Gap to target).

## Conventions & gotchas

- **The shape owns concurrency.** Tune load by editing `STAGES`, not CLI flags.
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
  the user path.
- **Environments & TRAPI versions vary per service.** Endpoints live across
  `*.ci.transltr.io`, `*.test.transltr.io`, and prod, and individual services pin
  different TRAPI versions in their URL paths. Target deliberately.
- **Swap the CURIEs.** The corpus uses a few real MONDO/CHEBI entities; replace
  them with entities the target service actually knows about, or lookups return
  empty and won't reflect real cost.

## Roadmap (broader repo, next phases)

Ordered so a future session can pick up where this leaves off:

a. **Restore the endpoint registry as config** (KPs/ARAs/ARS URLs + per-service
   predicate/query overrides), sourced from the git-history assets above.
b. **Add a per-component adapter** that selects the corpus subset (`lookup` vs
   `inferred`) and request protocol for the chosen layer.
c. **Add an ARS async user** implementing submit → poll `messages/{pk}` → merge.
d. **Add a real CLI entry point** in `setup.py` `console_scripts`:
   `run_performance_tests --targets {kps,aras,ars}` — selects **one** layer per
   run (mutually exclusive). There is intentionally no "all" option that hits every
   layer at once, because that double-loads shared downstream services
   (see the layering rule).
e. **Make config CLI/env/file driven** instead of module-level constants.
f. **Expand `README.md`** for human onboarding (this `CLAUDE.md` is agent/dev
   guidance; the README should be the friendly run-it-yourself doc).
g. **Adopt a `results/` output convention** so each service's reports land in a
   predictable, per-service location.
