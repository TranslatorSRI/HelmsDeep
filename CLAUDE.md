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
`ars_pathfinder` (async, via the ARS submit/poll/merge) — so the single-class
targets never mix it into the inferred corpus. Same protocols/endpoints as
`aras`/`ars`, just a different corpus + a gentler ramp / looser SLO.

The one place the classes *are* combined is the **mixed capacity profile**
(`aras_mixed`/`ars_mixed`): a deliberate 2/3 inferred + 1/3 Pathfinder blend that
asks whether the system holds a target concurrency under the workload mix
production actually sends, rather than characterizing one class in isolation.
The 2:1 is a split of **concurrent users**, not of requests: each user is pinned
to one class (see `MIXED_CLASSES` below).

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

## Result-count expectations

`helmsdeep-profile` measured what every MVP1/MVP2 query answers against an idle
service, and `helmsdeep/mvp_baselines.json` ships that measurement. A run can
therefore check something neither latency nor HTTP status shows: **did this query
come back with the answers it is supposed to have?** A service under load that
quietly returns a third of its results is broken, and every other signal says it
is fine.

The mechanism is deliberately staged, because scoring answers as failures moves
the knee:

| mode | what it does |
|---|---|
| `off` | no check, and no parsing of the response body it would need |
| **`report`** (default) | checks every query that has a baseline; writes `<prefix>_result_checks.csv`, a `results` block in `summary.json`, and a printed block — **fails nothing**. The error rate, the knee and every checkpoint verdict are bit-for-bit what they were before this existed |
| `enforce` | a shortfall *also* scores as a failure, so it counts against the error rate and therefore the knee |

Rules that matter:

- **Only a shortfall counts.** More answers than the baseline is reported
  (`over`) and never failed: the baseline is one measurement of a knowledge
  graph that grows.
- **The tolerance is loose on purpose** (`0.5` = "fewer than half the expected
  answers"). In the profiling pass the same query run twice against an idle
  service differed by up to **41%**, with one repeat landing 20% below its own
  median, so a tighter bar flags ordinary non-determinism as breakage. The
  printed block buckets the shortfalls (`under_10pct` … `under_75pct`) precisely
  so a `report` run tells you where to set the bar before you turn on `enforce`.
- **A query with no baseline is never checked** — and never even parsed. Every
  KP qtype, Pathfinder (it pins *two* entities), a batch lookup (many ids), and
  any unprofiled CURIE have no expectation, so `kps`/`*_pathfinder` runs print no
  block at all and the mixed profile checks only its inferred 2/3.
- **The pinned entity is read back out of the payload** (`trapi_corpus.pinned_entity`)
  rather than carried alongside it, so the builders stay single-return and the
  expectation is derived from the exact bytes that were sent.
- `median_ratio` per stage (observed/expected) is the number to watch: it falling
  as users rise is answer loss under load, visible in `report` mode without a
  single query being scored as a failure.
- Under `enforce` a 0-result `Done` fails as the most extreme shortfall there is,
  **even where `zero_result_is_failure` is False** — the two policies overlap at
  the bottom of the range.

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
  (`kps`/`aras`/`ars`, the Pathfinder run types `aras_pathfinder`/
  `ars_pathfinder`, and the mixed capacity profile `aras_mixed`/`ars_mixed`) to a
  component config: `label`, `endpoint`, `corpus`
  (key into the corpus module), `protocol` (`sync`/`async`), `implemented`, and
  per-target `stages` + `p99_slo_ms` + an optional `cooldown_s` (a quiet gap
  between stages — users ramp to 0 so slow in-flight queries drain into the stage
  that launched them instead of bleeding into the next; defaults to 0, set on the
  expensive ARA/ARS/Pathfinder targets). Async targets (`ars`, `ars_pathfinder`) add
  `messages_endpoint`, `poll_interval_s`, `max_poll_s`, and an optional
  `completion_max_poll_s` (>= `max_poll_s`; the extended cap for the completion
  sidecar — how long a background poller keeps watching a timed-out query to see if
  it *eventually* finishes; defaults to `max_poll_s` = off) and an optional
  `zero_result_is_failure` (whether a terminal `Done` carrying 0 results scores
  as a failure; defaults to `False`, and every target leaves it `False`). The `*_pathfinder` targets
  reuse the ARA/ARS endpoints + protocols but point at the `pathfinder` corpus and
  ship a gentler ramp + looser SLO (pinned-two-endpoint path-finding is the
  heaviest query class). The `*_mixed` targets add two more optional fields:
  `request_timeout_s` (per-HTTP-call timeout, default 210; raised on `aras_mixed`
  so its 5-min p99 SLO is measurable instead of landing as client timeouts) and
  `checkpoints` — a list of pass/fail acceptance criteria (`users`, `goal`,
  `p99_slo_ms`, `max_error_rate`, and on the mixed profile an optional
  `class_p99_slo_ms` per-class bar) evaluated against the stage that ran that
  many users. `MAX_ERROR_RATE` is shared; `DEFAULT_TARGET="kps"`.
  Also shared: the result-count expectation knobs `RESULT_CHECK`
  (`off`/`report`/`enforce`, default **`report`**) and
  `RESULT_SHORTFALL_TOLERANCE` (default 0.5), with per-target overrides
  `result_check` / `result_shortfall_tolerance` -- see **Result-count
  expectations** below.
  Per-target ramps/SLOs live here because cost profiles differ wildly by layer:
  `kps` holds each stage 5 minutes with a 60s cooldown because Retriever answers
  in seconds to tens of seconds, not the milliseconds the old ~40-KP layer did --
  at the 60s holds it used to carry, every stage tripped both measurement-quality
  flags (see `_stage_quality`).
  `build_timeline(stages, cooldown_s)` lays the ramp out on a wall clock as
  `(start, end, stage_idx_or_None)` windows (`None` = a cooldown gap); the
  `StepLoad` shape drives users from it, and the live console uses it only for
  the run's total duration (its stage identity comes from the collector -- see
  `console.Dashboard._phase`).
  `natural_duration_s()` and `time_scaled(cfg, budget_s)` implement
  `--time-budget`/`--quick`: `time_scaled` returns a `(compressed_cfg, scale)`
  pair whose *durations* (stage holds, `cooldown_s`, `request_timeout_s`,
  `max_poll_s`/`completion_max_poll_s`/`poll_interval_s`) are scaled to fit the
  budget, with per-knob floors (`MIN_HOLD_S` etc.) and spawn rates raised so a
  short stage still spends its time at load rather than climbing to it. The
  *shape* — users, SLOs, checkpoints — is never touched, and a budget above the
  natural duration is a no-op (scale 1.0): it only ever speeds a run up.
- **`cli.py`** — the `helmsdeep` entry point (registered in
  `setup.py` `console_scripts`). Parses `--targets` (required, one layer),
  `--host` (required), `--csv-prefix`, the mutually exclusive
  `--time-budget DURATION` / `--quick` (= `--time-budget 10m`),
  `--result-check off|report|enforce` (sets `HELMSDEEP_RESULT_CHECK`; the banner
  states which mode is in force, because `enforce` changes the knee),
  `--trapi-version 2.0|1.6` (normalized to major.minor by `config.trapi_minor`;
  sets `HELMSDEEP_TRAPI_VERSION`; the banner shows the version in force), the two
  output flags `--no-live` (sets `HELMSDEEP_LIVE=0`) / `--verbose`, and the
  opt-in `--save-errors [N]` (`nargs="?"`, const = `config.ERROR_SAMPLES_PER_KIND`,
  default 0; sets `HELMSDEEP_ERROR_SAMPLES=N` only when > 0 -- absent means
  error kinds are counted but nothing is written to disk); rejects
  not-yet-`implemented` targets; sets `LOADTEST_TARGET` (+ `LOCUST_CSV_PREFIX`,
  + `HELMSDEEP_TIME_BUDGET_S` when a budget is given) and launches
  `python -m locust -f trapi_loadtest.py --headless --host …`. Unless
  `--verbose`, it adds `--only-summary --loglevel WARNING`: Locust's 2-second
  request table and its ramp narration are exactly the noise the live display
  replaces (Locust still prints its final tables at shutdown). `_print_plan()`
  shows the (possibly compressed) ramp and what was traded away before the run
  starts; `_duration()` parses `600`/`90s`/`10m`/`1h30m`.
- **`profile_corpus.py`** — the `helmsdeep-profile` entry point: the per-CURIE
  cost profiler, and the **only** part of the package that is not a load test.
  It walks every entity the inferred corpus can pin — every MVP1 disease
  (`HEAVY_DISEASES` + `LONG_TAIL_DISEASES`), every MVP2 gene and chemical, both
  directions — sends each one's query once (`--repeat` for more) at low
  concurrency, and records response time, `result_count`, and knowledge-graph
  size per CURIE. It is the profiling pass "Calibrate the inferred tiers" needs.
  Plain threaded `requests`, no Locust and no gevent (`time.sleep` is correct
  here, unlike in the engine).
  - `build_probes()` builds each query by calling `trapi_corpus`'s own builders
    (`_inferred_treats`, `_affects`) with a **pinned** entity instead of a
    sampled one, so a profiled query is byte-for-byte the query the load test
    sends. There is no second copy of the corpus to drift.
  - `Prober` dispatches on the target's `protocol`, reusing `config.TARGETS`
    for endpoint/poll/timeout knobs: `_run_sync` (one POST) or `_run_ars`
    (submit → poll → merge, mirroring `_run_ars` in the engine). `--target` is
    restricted to `aras`/`ars` (`PROFILABLE_TARGETS`) — Pathfinder pins two
    endpoints and asks a different question.
  - A 0-result `Done` is a **measurement, not a failure**, here: an empty answer
    set is exactly what puts a CURIE in the light bin. (The opposite of the
    engine's `zero_result_is_failure` policy, which is about the knee.)
  - Long runs survive: every row is flushed to `<prefix>_probe.csv` as it lands,
    `--resume` skips entities already measured successfully (retrying failures),
    and Ctrl-C stops scheduling and still writes the roll-up.
  - `entity_summary()` collapses probes to one row per (qtype, CURIE);
    `assign_bins()` splits each qtype's entities into light/medium/heavy at
    `--bin-cuts` quantiles of `--bin-metric` (within a qtype, so diseases rank
    against diseases). An entity with no successful probe stays `unmeasured` and
    is reported rather than binned on missing data. Outputs
    `<prefix>_entities.csv` + `<prefix>_bins.json`; `--summarize-only` re-bins an
    existing probe CSV without sending a single query. `baselines()` writes
    `<prefix>_baselines.json` — the one output the package itself reads; copy it
    over `helmsdeep/mvp_baselines.json` to adopt a profile as the corpus's pools
    *and* its expected result counts. `--summarize-only` takes either a probe
    CSV or an `<prefix>_entities.csv` roll-up (`is_entity_csv`), so a profile
    someone hands you can be re-binned without the per-query rows.
- **`console.py`** — the live terminal display. Purely cosmetic and strictly
  read-only: every number comes from the run's `StageCollector` and its
  `_stage_stats`, so the screen can't drift from `stages.csv`, and an exception
  in the render loop is caught and logged rather than failing the run.
  - `Dashboard` — spawns a gevent greenlet that redraws twice a second.
    `_phase()` answers "stage N, holding or draining, how far in" **from the
    collector's `stage_idx`/`stage_started`/`stage_ended`**, never from elapsed
    time: the shape ticks off Locust's runner clock and the display off
    `test_start`, so a clock-derived stage disagreed with the measurements for a
    second or so at every boundary — exactly when the display has something to
    say. `_footer_lines()` renders the block (run progress, stage progress, live
    load/latency vs the SLO, plus an `ars` line for async targets);
    `_announce()`/`_recap()` write the scrolling record — a header when a stage
    starts, a `✓`/`✗` verdict line (the knee test on one stage) when it ends.
    Since a new stage can only appear after `mark_stage` closed the previous one,
    the previous stage's verdict is always final and always printed immediately
    before the next header: the transcript order is fixed, not a race.
  - `StickyFooter` — the bottom-of-screen block. `install()` wraps stdout/stderr
    (and re-points the logging handlers that captured them at setup time) in a
    proxy that erases the block before any other write, so prints and log lines
    scroll above it instead of colliding with it.
  - `sparkline(values, threshold, paint)` — the shape of a metric across the ramp,
    one character per stage, red where the stage broke `threshold`; the summary
    prints one for p99 and one for the error rate.
  - `live_enabled()` / `color_enabled()` — a non-TTY stdout (CI, a pipe) or
    `HELMSDEEP_LIVE=0` drops to plain mode: the same content as one status line
    every `PLAIN_EVERY_S`. `NO_COLOR` drops colour only.
- **`error_samples.py`** — bounded on-disk capture of what each failure actually
  looked like. `ErrorSampler(root, per_kind_per_stage, body_limit)` keeps, under
  `<prefix>_errors/<kind>/<NN>_stage<S>_<qtype>.json`, the request (method, URL,
  payload), the response (status, reason, headers, body truncated to
  `config.ERROR_SAMPLE_BODY_BYTES`, JSON kept as JSON) or the client exception
  when there was none, the stage/users/qtype, and any `extra` context (the ARS
  pk/step/`message_url`, the last `trace=y` poll body). `capture()` writes the
  file *immediately* (an aborted run keeps its samples) and always counts the
  occurrence, so `kinds()`/`summary()` are complete even once a `(kind, stage)`
  bucket is full (cap is per kind **per stage** -- the same 502 at 5 and at 60
  users has different bodies). **Opt-in**: with `per_kind_per_stage=0` (the
  default, no `--save-errors`) `enabled` is False and the sampler only counts --
  no file, no directory, no body decoding, and `reset()` doesn't delete either.
  When enabled, `reset()` wipes the directory at `test_start`;
  `write_index()` writes `index.json` (tally + every saved sample) at stop.
  `http_kind(resp)` names a kind from a response (`http_502`; status 0 →
  `timeout`/`connection_error`/`exception_<Class>` from Locust's `resp.error`),
  `failure_message(resp, default)` swaps in the exception text for status 0.
  Never raises into the user path (the engine's `_sample()` wrapper also
  swallows). The engine's kinds: sync `http_<code>`/`timeout`/…; ARS
  `ars_submit_http_<code>`/`ars_submit_no_pk`/`ars_error`/`ars_timeout`/
  `ars_done_zero_results` (terminal) and `ars_poll_http_<code>`/
  `ars_poll_bad_json`/`ars_merge_http_<code>`/`ars_merge_unparseable`/
  `ars_merge_missing` (`intermediate: true`).
- **`trapi_loadtest.py`** — the measurement engine (Locust). The component under
  test is chosen by `LOADTEST_TARGET`, and when `HELMSDEEP_TIME_BUDGET_S` is set
  the target config is passed through `config.time_scaled` at import (module-level
  `TIME_SCALE`, stamped into `summary.json` as `config.time_scale` and printed as
  a warning banner) so everything downstream reads the compressed values; `ENDPOINT`, `CORPUS`, `STAGES`,
  `P99_SLO_MS`, and the async knobs (`PROTOCOL`, `MESSAGES_PATH`,
  `POLL_INTERVAL_S`, `MAX_POLL_S`) plus `COOLDOWN_S` are all derived from
  `config.TARGETS[...]`.
  - `StepLoad(LoadTestShape)` — drives the `stages` ramp and marks the active stage.
    When `COOLDOWN_S` is set it inserts a drain gap between stages (`tick()`
    returns 0 users in the gap and calls `COLLECTOR.end_active_stage()` to freeze
    the just-finished stage's end time); the tick that ends the ramp freezes the
    **final** stage the same way, before returning `None`, so its `duration_s` is
    its hold and not its hold plus Locust's shutdown drain. Every boundary the
    shape reports is back-dated through `_boundary_time()` — `tick()` is polled
    about once a second, so it learns a boundary passed up to a second late, and
    a stage's recorded window is the one `stages` configures rather than the one
    the poll noticed. An `@events.init` listener sets
    `stop_timeout = REQUEST_TIMEOUT` so a slow in-flight query finishes rather than
    being killed when users ramp to 0. The gap's spawn rate is the just-finished
    stage's **user count**, so Locust's dispatcher stops every user in one
    iteration: it removes `floor(spawn_rate)` users per iteration and blocks on
    each batch's `stop_timeout`, so a smaller rate walks the teardown down a few
    users a second while everyone not yet stopped keeps starting *new* queries.
    `_draining()` + the gate at the top of `TRAPIUser.query` close the remaining
    seam (Locust polls `tick()` only about once a second, and acts on it
    asynchronously): a user whose query returns inside the gap parks on
    `gevent.sleep(DRAIN_RECHECK_S)` instead of issuing another one. It reads the
    shape's own clock (`StepLoad.in_cooldown()`, via the module-level `SHAPE`),
    not the last tick, so the boundary is exact.
  - `StageCollector` / `COLLECTOR` — buckets every completed request into the
    stage active when it *finished* (per-stage, per-`qtype`). Also holds the two
    per-query ARS lists (`queries`, `completions`) and hands out the shared
    `new_query_id()` that joins them. Records each stage's
    wall-clock `stage_started` / `stage_ended` (both `setdefault`, so a cooldown
    freeze isn't overwritten by the next `mark_stage`; both take an optional
    `when` the shape uses to back-date them to the exact ramp boundary).
    `record()` also takes optional ARS signals: `status`, `result_count`,
    `response_bytes`. On a multi-class target it also buckets requests/errors/
    latencies under `class:<name>` (via `CLASS_KEY`), which is what
    `by_class.csv` and the per-class checkpoint p99 read.
    `begin_inflight()`/`end_inflight()` track logical queries currently running,
    for the live display only -- no report reads them.
  - `_stage_stats()` — per-stage RPS, percentiles, error rate, Little's-Law
    concurrency. `_ars_stage_health()` — per-stage ARS health row.
  - `TRAPIUser(HttpUser)` — **abstract**; the runnable user classes are
    generated from `USER_CLASSES` (`trapi_corpus.user_classes_for`) and bound
    into module globals so Locust discovers them: one class per query class,
    weighted by its share of users, each with its own flattened corpus
    (`_flat`). A single-class target gets one (`RetrieverUser`,
    `ShepherdUser`, …); the mixed profile gets `InferredUser` (weight 2) +
    `PathfinderUser` (weight 1), and Locust's dispatcher holds that split
    exactly (20/10 at 30 users). `query()` draws from `self._flat`, then
    dispatches on `PROTOCOL`: `_run_sync()` (KP/ARA: one
    blocking `POST`) or `_run_ars()` (submit → poll `/messages/{pk}` with
    `gevent.sleep` until `Done`/`Error`/timeout → `_fetch_merged()` counts
    `fields.data.message.results` and returns its HTTP status). One
    `COLLECTOR.record` per logical query, plus one `COLLECTOR.record_query` debug
    row (`_record_query`) carrying the `pk`, the submit/poll/merge HTTP codes, the
    poll count, the terminal ARS status, a `QueryIssues` tally of the
    intermediate (retried, non-fatal) errors hit along the way
    (`intermediate_error_count` + `intermediate_errors`), a `message_url`, and
    an `error_sample` path (the saved example of the terminal error, if kept) —
    written on every terminal path including `SubmitError`/`NoPK`/`Timeout`.
    Every failure on either path also goes through the module-level `_sample()`
    → `SAMPLER` (an `error_samples.ErrorSampler` rooted at `<prefix>_errors`,
    cap from `HELMSDEEP_ERROR_SAMPLES`, default 0 = count only / write nothing,
    reset on `test_start` on every node; the two `last_poll` descriptions are
    guarded on `SAMPLER.enabled` so a disabled run never decodes a trace body):
    sync failures with the response and
    the POSTed payload; ARS submit/poll/merge trouble as it happens (poll/merge
    ones `intermediate=True`), and the terminal `ars_error`/`ars_timeout`/
    `ars_done_zero_results` with the submitted query as the request and the
    last parsed `trace=y` poll (`last_poll`) as the response. When a
    query blows `MAX_POLL_S` (already recorded as a Timeout failure — main stats
    unchanged), `_run_ars` spawns a detached `_extended_poll` greenlet that keeps
    polling to `COMPLETION_MAX_POLL_S` and appends one `COLLECTOR.record_completion`
    row (end-to-end time + whether it `finished`); queries that finish within
    `MAX_POLL_S` record their completion row inline.
  - `_check_results(qtype, payload, count, latency_ms)` + `_count_sync_results`
    — the result-count expectation (see **Result-count expectations**). `count`
    is a **callable**, not a number: the baseline lookup is a dict hit while the
    observed count on the sync path means parsing a response that can be
    megabytes, so a query with no baseline never decodes its body. Returns
    `(verdict, failed)` where `failed` is True only under `enforce` and only for
    a shortfall — in `report` mode this function cannot move the error rate, the
    knee, or a checkpoint. `StageCollector.record_result_check` tallies per stage
    (`checked`/`shortfalls`/`surpluses`/`result_ratios`) and appends one
    `result_checks` row per *deviation* (not per query checked — the in-spec rows
    would bury them). `_result_check_stage(idx)` is the per-stage roll-up;
    `on_test_stop` writes `<prefix>_result_checks.csv`, the `results` block in
    `summary.json`, and the printed RESULT COUNTS block. A shortfall is error-
    sampled (kind `result_shortfall` / `ars_result_shortfall`) **only when it was
    actually scored as a failure**, so the ERROR SAMPLES block keeps listing only
    things that failed.
  - `_stage_quality(row)` — the two conditions that make a stage's row untrust-
    worthy however correct its arithmetic: fewer than `MIN_P99_SAMPLES` (100)
    completed requests, so the p99 rests on the two slowest and moves with one
    outlier; and Little's-Law concurrency below `MIN_CONCURRENCY_RATIO` (0.5) of
    the stage's user count, meaning most users were blocked on queries that
    finished in the *next* stage (we bucket by finish time), inflating its tail
    and deflating this one. Each issue is `{kind, detail}` (`thin_samples` /
    `in_flight_bleed`) so the printed remedy names only the fix that applies and
    a script can branch without parsing prose. `on_test_stop` collects these into
    `stage_warnings` + `knee_unsupported` in the summary, a printed MEASUREMENT
    QUALITY block, and a caveat on the final headline when the knee rests on a
    flagged stage.
  - `_evaluate_checkpoints()` — for targets that configure `checkpoints`, judges
    each one against the stage matching its user count (and, on the mixed
    profile, each query class's own p99 via `_class_p99s`, bar
    `class_p99_slo_ms[cls]` defaulting to the checkpoint's; a judged class
    with no completed requests is a miss) and returns a
    `PASS`/`FAIL`/`NO DATA` verdict (a checkpoint's `p99_slo_ms` defaults to the
    target's; explicit `None` means latency isn't judged — an overload probe where
    slowdown is expected but failures aren't).
  - `_start_dashboard()` (`@events.test_start`) / `DASHBOARD` — starts the
    `console.Dashboard` on the master/standalone node; `on_test_stop` recaps the
    final stage and lifts the footer before printing the summary.
    `_stash_headline()` + `_print_headline()` (`@events.quit`) repeat the knee
    (or the missed checkpoints) *after* Locust's own end-of-run tables, so the
    number the run exists to produce is the last thing on screen.
  - `on_test_stop()` — drains any in-flight completion greenlets (bounded), finds
    the knee, writes `stages.csv` (incl. a `stage_start` ISO-8601-UTC column) /
    `by_qtype.csv` / `summary.json`, (checkpointed targets only)
    `checkpoints.csv` + `checkpoints`/`checkpoints_passed` in the summary and a
    printed verdict block that sets `environment.process_exit_code = 1` on any
    miss, prints the stage table plus two `console.sparkline` shape rows (p99 and
    error rate across the ramp, red where a stage broke its bar), and (async only) `ars_health.csv`, the `ars_queries.csv` per-query
    debug log + a printed "FAILED QUERIES" block naming the first few pks/URLs,
    the `ars_completion.csv` sidecar, plus `red_flags` + a `completion` roll-up in
    the summary + printed block. Also `SAMPLER.write_index()` →
    `<prefix>_errors/index.json`, `error_samples` (the sampler's `summary()`)
    in `summary.json`, a printed ERROR SAMPLES block (kind / count / saved /
    stages / an example path) and an `ERRORS:` headline line.
- **`trapi_corpus.py`** — the per-component query corpuses:
  - `_qg(nodes, edges, tier=None, bypass_cache=None)` — TRAPI envelope; adds
    scalar `parameters.tier` (KP-only) or `bypass_cache` (ARA/ARS) only when
    supplied. `_set_bypass_cache` places `bypass_cache` per `trapi_version()`:
    `parameters.bypass_cache` for TRAPI 2.0 (the default, `config.TRAPI_VERSION`),
    a top-level field for 1.x. `_set_qualifiers` does the same for an edge's
    qualifier set: `constraints.qualifiers` (a list of plain
    `{qualifier_type_id: value}` mappings) for 2.0, `qualifier_constraints`
    (`[{"qualifier_set": [{qualifier_type_id, qualifier_value}]}]`) for 1.x.
    The 1.x query shape is byte-for-byte what 0.3.0 sent; under 2.0 every
    corpus query validates against `translator_tom`'s TRAPI 2.0 `Query` with no
    field the model doesn't define, bar Retriever's own `parameters.tier`.
    `trapi_version()` reads `HELMSDEEP_TRAPI_VERSION`
    each time a query is built, so the CLI flag reaches the load test and
    `helmsdeep-profile` alike; the locustfile reads it once at import (so a bad
    value fails before any user spawns) and records it as
    `summary.json`'s `config.trapi_version`.
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
  - **`MIXED_CLASSES`** — the mixed capacity profile (`aras_mixed`/`ars_mixed`):
    `[("inferred", SHEPHERD_CORPUS, 2), ("pathfinder", PATHFINDER_CORPUS, 1)]`,
    weights from `INFERRED_PATHFINDER_RATIO = (2, 1)`. The weights split
    **users**, not requests: each user stays in one class, and within a class
    draws per request from that corpus (so MVP1/MVP2 retuning propagates). It
    used to be one blended list drawn per request, and in a closed loop that
    doesn't hold the mix. Inferred users finished fast and re-rolled, the ones
    that rolled Pathfinder stayed stuck, and the in-flight share drifted to
    ~`L_pf / (L_pf + 2·L_inf)` (~80% Pathfinder), worse as saturation slowed
    Pathfinder further. `MIXED_CORPUS` is now just the union of the two, for
    code that lists qtypes. `user_classes_for(name)` returns the split (a single
    `(name, corpus, 1)` class for every other corpus).
  - Entity pools, now **measured** rather than curated. `mvp_baselines.json`
    (shipped via `package_data`, written by `helmsdeep-profile`) carries one
    record per CURIE — its measured `results`, `latency_s`, its `bin`, and
    `usable` — and the pools are built from it at import by `_binned()`:
    `HEAVY_DISEASES` (329 diseases, 218–1071 results, median 68s),
    `MEDIUM_DISEASES` (337, 38–217, median 35s), `LIGHT_DISEASES` (243, 1–36,
    median 11s). The tiers are disjoint and ordered, so `mvp1_heavy/medium/light`
    finally differ in **cost** and not just in weight. `LONG_TAIL_DISEASES`
    (from **`curie_list.json`**, ~1000 MONDO CURIEs) survives only as the
    fallback when a tier has no measured members — a checkout whose baselines
    file is missing still runs. `GENES`/`CHEMICALS` are curated pools filtered
    through `_usable()`, which drops what the profile measured as answering
    nothing (donepezil, `CHEBI:53289`, answered 0 on both probes and is out).
    `corpus_for(name)` returns the right list.
  - `pinned_entity(payload)` — the one CURIE a query pins, or None if it pins
    none or several. `baseline_for(qtype, payload)` — that CURIE's baseline
    record via `BASELINE_QTYPES` (which maps the three MVP1 qtypes onto the one
    `mvp1_treats` record set), or None when the question was never profiled.
    `BASELINE_BY_ENTITY` is the flat `(qtype, entity) -> record` index it reads.

For KPs, cost is driven by query-graph **shape** (hops, mode, pinned vs open,
batch, predicate). For ARA/ARS creative queries, the dominant cost driver is the
**pinned disease's answer-set size**, which is why that corpus varies the entity.

## Status & what's left

Implemented: component awareness, the `helmsdeep` CLI + entry point, the
`helmsdeep-profile` per-CURIE corpus profiler, the **measured** MVP1 tiers and
the result-count expectation check (`report` mode on by default),
per-target stages/SLO, segmented corpuses, scalar `parameters.tier` per KP query,
the ARS async submit/poll/merge user, ARS health metrics + red flags, the
measured-and-tiered inferred disease mix, and the mixed capacity profile (2:1
inferred/Pathfinder blend + pass/fail acceptance checkpoints, ARA and ARS).

Remaining refinements (not yet done — don't assume these exist):

- **Enforcement of result counts is not switched on.** `RESULT_CHECK` defaults
  to `report`: a run measures every answer count against the profiled baseline
  and reports the deviations, but nothing fails and the knee is unchanged. That
  is deliberate stage one. Stage two is flipping a target (or the shared default)
  to `enforce` once a `report` run on the real service shows what its normal
  spread is — read `observed_over_expected_bands` and `median_ratio` in
  `summary.json` before choosing the tolerance, and expect to tighten
  `RESULT_SHORTFALL_TOLERANCE` below 0.5 at the same time.
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
pip install -e .          # Python >= 3.11; installs locust

# One layer per run (kps | aras | ars); --host required.
helmsdeep --targets kps --host https://your-retriever.example.org --csv-prefix run1
helmsdeep --targets aras --host https://your-ara.example.org --csv-prefix run1
helmsdeep --targets ars  --host https://ars.ci.transltr.io --csv-prefix run1

# Pathfinder is its own (heavier) run type, ARA/ARS only:
helmsdeep --targets aras_pathfinder --host https://your-ara.example.org --csv-prefix pf1
helmsdeep --targets ars_pathfinder  --host https://ars.ci.transltr.io --csv-prefix pf1

# Mixed capacity profile (ARA/ARS only): 2/3 inferred MVP1+MVP2 + 1/3 Pathfinder,
# ramped to 30 -> 45 -> 60 concurrent and judged pass/fail per checkpoint.
helmsdeep --targets aras_mixed --host https://your-ara.example.org --csv-prefix mix1
helmsdeep --targets ars_mixed  --host https://ars.ci.transltr.io --csv-prefix mix1
```

- The `LoadTestShape` (`StepLoad`) **drives users, spawn rate, and duration**, so
  there is no `-u` / `-r` / `-t`. Tune the ramp via the per-target `stages` in
  `config.py`.
Profiling the corpus (not a load test — one query per CURIE, low concurrency):

```bash
# Every MVP1 disease + every MVP2 gene/chemical, via the ARA.
helmsdeep-profile --host https://your-ara.example.org --out mvp_profile
helmsdeep-profile --host https://your-ara.example.org --out mvp_profile --resume
# Re-bin from data already on disk; sends nothing. Takes either the probe CSV
# or an _entities.csv roll-up.
helmsdeep-profile --summarize-only mvp_profile_probe.csv --out mvp_profile \
    --bin-cuts 0.25,0.75

# Adopt a profile as the corpus's pools AND its expected result counts:
cp mvp_profile_baselines.json helmsdeep/mvp_baselines.json
```

- `--csv-prefix` is optional; it falls back to the `LOCUST_CSV_PREFIX` env var,
  then to `trapi_run`.
- You can also run the locustfile directly (`locust -f helmsdeep/
  trapi_loadtest.py --headless --host …`), selecting the layer with the
  `LOADTEST_TARGET` env var (defaults to `kps`). Note locust has no
  `--csv-prefix` flag — set `LOCUST_CSV_PREFIX` instead.
- A checkpointed run (`*_mixed`) **exits non-zero** when a checkpoint is missed,
  so it can gate a CI/acceptance job.
- `--quick` (= `--time-budget 10m`) or `--time-budget 30m` compresses any target
  to a wall clock; see the conventions note below for what that costs.
- Outputs (written by the master/standalone node only):
  `<prefix>_stages.csv`, `<prefix>_by_qtype.csv`, `<prefix>_summary.json`
  (+ `<prefix>_checkpoints.csv` for checkpointed targets, with a
  `class_p99_ms` column on the mixed profile) (+ `<prefix>_by_class.csv` and a
  `by_class` summary block for the mixed profile -- one row per (stage, query
  class))
  (+ `<prefix>_result_checks.csv` whenever anything the run sent had a profiled
  baseline -- one row per answer-count deviation, with the CURIE) (+
  `<prefix>_ars_health.csv`, the `<prefix>_ars_queries.csv` per-query debug log,
  the `<prefix>_ars_completion.csv` sidecar, and a `red_flags` list +
  `completion` roll-up for the `ars` target), with `--save-errors [N]` only a
  `<prefix>_errors/` directory (one JSON file per saved error example, grouped
  by kind, plus `index.json`; without the flag error kinds are counted in the
  summary but nothing is written), plus a printed summary table with the knee.

## Conventions & gotchas

- **The shape owns concurrency.** Tune load by editing the per-target `stages`
  in `config.py`, not CLI flags.
- **Two kinds of run: knee-finding vs acceptance.** Every target reports the knee
  ("how far can we go?"). A target that also defines `checkpoints` answers a
  pass/fail question at named concurrency levels ("does 30 hold?") and exits
  non-zero on a miss. `aras_mixed`/`ars_mixed` are the acceptance profile: a 2:1
  inferred/Pathfinder blend checked at 30 (peak) / 45 (headroom) / 60 (overload,
  error-rate only). The blend is of *users*, so the request counts lean hard
  toward inferred (its users finish more queries) and the pooled p99 is mostly
  inferred latency; that's why each class's p99 is judged on its own too. Checkpoints are generic, not special-cased to that profile --
  a target without them behaves exactly as before.
- **A stage's `duration_s` is its load window, never its drain.** Every stage's
  end is frozen when its *load* stops: at the cooldown gap for stages that have
  one, and — for the last stage, which has no gap after it — on the tick that
  ends the ramp, before Locust's `stop_timeout` shutdown drain. Nothing starts a
  new query in either drain, so counting it would divide the stage's requests by
  a window padded with dead air. It is not a rounding error: a 600 s hold trailed
  by a ~190 s drain reported its RPS, and with it its Little's-Law concurrency,
  ~24 % low — enough to move the knee off the top of the ramp onto a stage the
  service had already beaten. Requests are still bucketed by the stage active
  when they *finished*, so a query that drains after the freeze still counts in
  the stage that launched it; only the denominator changed.
- **Cooldown drains, it doesn't bleed.** With `cooldown_s` set, the gap between
  stages ramps users to 0; the just-finished stage's end time is frozen so its
  `duration_s`/RPS reflect the active window, and a slow query still running
  drains into *that* stage (via `stop_timeout`), keeping the next stage clean.
  A drain is **only** in-flight queries finishing — **no new query starts once
  the gap begins**. Locust won't give you that for free: its teardown is
  rate-limited and its shape poll is ~1 s coarse, so the gap both stops every
  user in a single dispatch iteration (spawn rate = the stage's user count) and
  gates `TRAPIUser.query` on `_draining()`. Without the gate the frozen stage
  keeps collecting requests it never launched under load, inflating its RPS.
- **Closed-loop load.** `TRAPIUser.wait_time = constant(0)` — no think time; users
  hammer the endpoint as fast as responses return.
- **A compressed run is not a measurement.** `--time-budget`/`--quick` scale
  durations only (holds, cooldowns, poll/timeout caps) — the ramp, SLOs, and
  checkpoints are identical, so the run asks the same questions of the same load
  levels. But it answers them from far fewer samples (a p99 over a handful of
  queries is noise), and the shrunken per-query caps *change what counts as a
  failure*: a query that would finish in 4 minutes is a timeout when the cap is 2.
  Compressed runs are for exercising a host/corpus/config end to end, not for
  quoting a knee or gating CI. `config.time_scale` in `summary.json` (< 1.0) is
  how you tell after the fact.
- **The live display never measures anything.** `console.py` reads the same
  `StageCollector` and `_stage_stats()` the CSVs are written from; it owns no
  tally of its own, and its greenlet swallows its own exceptions. Changing what
  the footer says is a rendering change, never a measurement change.
- **The collector owns "which stage are we on" -- for the display too.** The
  display derives the stage, its progress, and its verdict from
  `COLLECTOR.stage_idx`/`stage_started`/`stage_ended`, not from its own elapsed
  clock. Two clocks meant the screen and the CSV could name different stages at a
  boundary, and stage verdicts landed on either side of the next stage's header
  depending on which won the race. If you add anything that reports where a run
  is, read it from the collector.
- **A stage's row can be right and still not mean anything.** `_stage_quality`
  flags the two ways that happens -- a p99 over too few samples, and effective
  concurrency far below the user count (in-flight queries bleeding into the next
  stage). Both are ramp-shape problems, fixed with longer holds and a
  `cooldown_s` in `config.py`, never by adjusting the numbers. If you retune a
  target's stages, check a real run for these flags rather than assuming the old
  holds still fit the service's latency.
- **Don't trust Locust's blended aggregate during a ramp.** We bucket per stage in
  `StageCollector` precisely because an aggregate p99 would mix easy early stages
  with saturated late ones.
- **`malformed_query` 4xx is success.** A 4xx on the malformed query is treated as
  a valid measurement of the error path; only 5xx counts as a failure. See the
  `TRAPIUser.query` handling.
- **Long timeouts on purpose.** `REQUEST_TIMEOUT` defaults to 210 s (per-target
  override: `request_timeout_s`) because TRAPI queries are slow; ARS runs are far longer still (minutes–~1 hr) and need the async model.
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
  A `Done` that returns **0 results is not a failure** (it still raises a red
  flag) — see `zero_result_is_failure` below; a non-terminal status past
  `max_poll_s` is a `Timeout` failure.
- **`zero_result_is_failure` decides what a 0-result `Done` means.** Default
  `False`, and **every target keeps it `False`**: only transport/protocol
  outcomes (submit error, `Error` status, `Timeout`) fail, and the zero-result
  query's latency joins the percentile pool. Setting it `True` on a target scores
  an empty answer set as a failure (a silent downstream drop-out) that counts
  against the knee — and drops its latency from the pool (failed requests
  contribute no latency samples), so it shifts mean/p99/concurrency, not just the
  error rate. Either way the query is still tallied in `ars_health`
  (`zero_result_done`) and still raises a red flag, the per-query debug log still
  carries the `Done with 0 results` note (its `failed` column follows the policy),
  and the summary JSON + printed ARS health block record which policy was in force.
- **Intermediate errors are the trouble a query survived.** The poll loop retries
  through non-200 polls, unparseable poll bodies and a bad merge fetch, so none of
  them change `ars_status` — which means a query that fought through thirty 502s
  lands in the log as a clean `Done`. `QueryIssues` tallies them per query into
  `intermediate_error_count` (0 = clean) + `intermediate_errors` (`poll HTTP 502
  x3; merge HTTP 500`), and `on_test_stop` prints a roll-up separate from the
  FAILED QUERIES block precisely because most queries carrying them succeeded.
- **Error samples are the bridge from a status code to a cause.** A stage row
  says 12% failed; Locust's table says they were 502s; neither says what the
  502 *said*. `error_samples.py` keeps a few full examples per error kind per
  stage under `<prefix>_errors/` — the request, the response body/headers or
  the client exception — written the moment they happen, but **only when the
  run passes `--save-errors`**: the default is count-only, so a run on a box
  with little disk or RAM never accumulates response bodies, and the
  printed/JSON tally of kinds is the same either way. Adding a new failure
  path to the engine means adding a `_sample(...)` call beside its
  `COLLECTOR.record` (terminal) or `issues.add` (intermediate), with a kind
  slug that names the mode, not the query: kinds are what the post-mortem
  groups by. Sampling never touches the measurement: every `_sample` call is
  wrapped, a full bucket only bumps the counter, and a failed write gives the
  slot back. The directory is wiped at `test_start` like the CSVs are
  overwritten, so don't point two concurrent runs at one prefix.
- **The per-query debug log is the bridge from a number to a query.** The
  aggregates say 3% failed; `<prefix>_ars_queries.csv` says which pks, with the
  HTTP status of each step and a `message_url` to pull one up. One row per logical
  query on every terminal path (including `SubmitError`/`NoPK`, which have no pk
  at all), attributed to the stage active when it reached its outcome. It joins to
  `ars_completion.csv` on `query`; a timed-out query whose extended poll was still
  in flight at shutdown is in the debug log but *not* the sidecar.
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
- **The inferred tiers are measured; don't hand-edit them.** `HEAVY_DISEASES` /
  `MEDIUM_DISEASES` / `LIGHT_DISEASES` are derived at import from
  `mvp_baselines.json`. Retune by re-profiling and copying the new
  `<prefix>_baselines.json` over it, never by editing lists in
  `trapi_corpus.py`. The same file is the expected-result-count baseline, so
  editing one without the other would make the pools and the expectations
  disagree.
- **An entity that answers nothing is not a cheap query, it's a dead one.**
  `usable: false` in the baselines (fewer than `min_usable_results` answers)
  keeps a CURIE on the record but out of the sampling pools: it exercises
  nothing, and on a target with `zero_result_is_failure` it would poison the
  error rate with a known-empty question. 86 of the 995 profiled diseases are in
  that state, plus donepezil on the MVP2 chemical side.
- **A `report`-mode result check changes no measurement.** That is the whole
  point of the default: the per-stage tables, the knee, the error rate and every
  checkpoint verdict are identical with it on or off. Only `enforce` moves them.
  If you touch `_check_results`, keep that true.
- **Inferred corpus mixes MVP1 + MVP2 and varies entities per request.** MVP1
  (`mvp1_heavy/medium/light`, treats-disease) samples a tiered disease; MVP2
  (`mvp2_chem_affects_gene`/`mvp2_chem_affects_open_gene`, a chemical→gene
  `affects` edge with the gene-pinned and chemical-pinned variants) samples the
  pinned entity + direction. The edge orientation is always chemical(subject)→
  gene(object) and the aspect qualifier is always `activity_or_abundance`. The
  per-request variation covers the real cost surface and avoids warming caches.
  MVP1's three tiers are measured (see **Result-count expectations**): on the
  profiled ARA their median response times were 68s / 35s / 11s.
- **Environments & TRAPI versions vary per service.** Endpoints live across
  `*.ci.transltr.io`, `*.test.transltr.io`, and prod, and individual services pin
  different TRAPI versions in their URL paths. Target deliberately.
- **The TestHarness pins a tag, so releases are explicit.** TestHarness installs
  HelmsDeep from `requirements-runners.txt` as `git+…/HelmsDeep@vX.Y.Z`, not
  `@main`. To ship a change to it: bump `version` in `setup.py`, tag that commit
  `vX.Y.Z` (matching), push the tag, then bump the pin in TestHarness. Never move
  an existing tag -- a tag that changes underneath a built image defeats the pin.
- **Stay on 3.11 syntax.** `setup.py` pins `python_requires=">=3.11"`, not 3.12,
  because the [Translator TestHarness](https://github.com/TranslatorSRI/TestHarness)
  installs HelmsDeep as its performance runner and runs on a 3.11 base image. No
  3.12-only syntax anywhere in the package.
- **Swap the CURIEs.** The KP corpus uses a few real MONDO/CHEBI entities;
  replace/extend them with entities the target service actually knows about, or
  queries return empty and won't reflect real cost. The *inferred* corpus no
  longer needs this done by hand -- `helmsdeep-profile` finds the empty ones and
  the baselines file keeps them out of the pools -- but a different service (a
  different environment, or a different ARA) needs its own profile, because
  which CURIEs answer is a property of the service, not of the corpus.

## Roadmap (broader repo, next phases)

Done in earlier phases: per-component adapter + `--targets` CLI entry point
(one layer per run — there is intentionally no "all" mode, which would
double-load shared downstream services per the layering rule), the ARS async
submit→poll→merge user, per-target config, and a README for human onboarding.

Remaining, ordered so a future session can pick up where this leaves off:

a. **Turn on result-count enforcement.** The tiers are calibrated and the check
   runs in `report` mode (done — see **Result-count expectations**). What remains
   is a `report` run against the real service to learn its normal spread, then
   `--result-check enforce` with a tolerance set from that evidence. Re-profile
   first if the knowledge graph has moved since
   `profiles/mvp_profile_entities_2026-10-14.csv`: a stale baseline makes a
   healthy service look like it is dropping answers.
a2. **Re-profile periodically.** `profiles/` holds the pass the shipped
   baselines came from (995 diseases + 20 MVP2 entities, profiled via the ARA).
   86 of those diseases answered 0 results and are excluded from sampling; if
   that count moves a lot, the corpus is drifting away from what the service
   knows.
b. **Per-ARA health breakdown** for ARS, parsing the `trace=y` children so a
   red flag can name *which* downstream agent dropped answers.
c. **Restore the full endpoint registry as config** (per-KP/ARA URLs +
   predicate/query overrides) from the git-history assets above, if/when the
   stack returns to multiple independent KP/ARA endpoints.
d. **Make config file-driven** (external config file / full CLI overrides for
   stages, SLOs, poll knobs) instead of editing `config.py`.
e. **Adopt a `results/` output convention** so each service's reports land in a
   predictable, per-service location.
