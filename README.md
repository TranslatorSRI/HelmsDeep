# HelmsDeep

**HelmsDeep** — *HTTP Endpoint Load Measurement System, Determining Each
Endpoint's Performance* — drives a stepped ramp of concurrent users against a
single NCATS Translator component and reports the **max sustainable
concurrency** (the "knee") — the highest load where the service still meets a
latency/error SLO.

The Translator stack cascades **ARS → ARAs → KPs**, so a run targets exactly
**one** layer at a time (testing a higher layer already loads everything beneath
it). All three workflows are wired up: **Retriever (KP)**, **Shepherd (ARA)**,
and the asynchronous **ARS**.

> **In plain language:** HelmsDeep slowly turns up the number of simultaneous
> users hitting a service and watches when it starts getting too slow or too
> error-prone. The single headline number it reports — the **knee**, or "max
> sustainable concurrency" — is *the largest number of simultaneous users the
> service handled while still staying fast enough and erroring rarely enough.*
> Above that load, the service is overwhelmed. If you only read one thing,
> read the **["How to read the results"](#how-to-read-the-results)** section.

## Install

```bash
pip install -e .          # Python >= 3.12; installs locust
```

## Run a workflow

```bash
# Retriever (KP) — sync lookup queries, scalar parameters.tier per query
helmsdeep --targets kps \
    --host https://your-retriever-service.example.org \
    --csv-prefix run1

# Shepherd (ARA) — sync creative-mode (inferred) queries, cache bypassed
helmsdeep --targets aras \
    --host https://your-ara-service.example.org \
    --csv-prefix run1

# ARS — async submit/poll/merge of inferred queries (host = the ARS API base)
helmsdeep --targets ars \
    --host https://ars.ci.transltr.io/ars/api \
    --csv-prefix run1

# Pathfinder — its own heavier run type (ARA/ARS only); pins two endpoints and
# asks for connecting paths. Sync via the ARA, async via the ARS.
helmsdeep --targets aras_pathfinder \
    --host https://your-ara-service.example.org \
    --csv-prefix pf1
helmsdeep --targets ars_pathfinder \
    --host https://ars.ci.transltr.io/ars/api \
    --csv-prefix pf1
```

- `--targets` selects the layer: `kps` (Retriever), `aras` (Shepherd), `ars`, or
  the Pathfinder run types `aras_pathfinder` / `ars_pathfinder` (ARA/ARS only).
- `--host` is **required** — the base URL of the target service. For `kps`/`aras`
  the `/query` path is appended; for `ars` the host is the API base and the tool
  uses `/submit` then `/messages/{pk}`.
- `--csv-prefix` is optional; it falls back to the `LOCUST_CSV_PREFIX` env var,
  then to `trapi_run`.

The load profile (users, spawn rate, duration) is driven by the `StepLoad` shape,
**not** by CLI flags — so there is intentionally no `-u/-r/-t`. The ramp and knee
threshold are **per target** in `helmsdeep/config.py` (`stages` and
`p99_slo_ms`), since cost profiles differ wildly by layer; edit them there.

An optional per-target `cooldown_s` inserts a quiet gap **between** stages: users
ramp to 0 so slow in-flight queries drain (counted under the stage that launched
them) before the next stage starts clean, instead of bleeding into it. It defaults
to 0 (cheap KP lookups don't bleed) and is set on the expensive ARA/ARS/Pathfinder
targets.

## Outputs

Written to the working directory by the standalone/master node:

- `<prefix>_stages.csv` — one row per stage (overall metrics), including a
  `stage_start` column with the stage's wall-clock start time (ISO 8601 UTC)
- `<prefix>_by_qtype.csv` — one row per (stage, query type)
- `<prefix>_summary.json` — config (including which `target` was measured), all
  stages, and the chosen knee
- `<prefix>_ars_health.csv` — **ARS only**: per-stage health signals (see below)
- `<prefix>_report.html` — Locust's native, self-contained **HTML report**
  (latency-over-time charts, request/failure tables). Open it in any browser. See
  the caveat under ["How to read the results"](#how-to-read-the-results): its
  totals are *blended across the whole run*, so the authoritative ceiling is the
  knee in `summary.json`, not the HTML aggregate.
- a printed summary table ending in the headline **max sustainable concurrency**

For a guide to interpreting every one of these — written for non-specialists —
see ["How to read the results"](#how-to-read-the-results) below.

### ARS async workflow and health signals

The `ars` target is asynchronous: each logical query is `POST /submit` → poll
`GET /messages/{pk}?trace=y` (every `poll_interval_s`, capped at `max_poll_s`,
default 15 min) until `status` is `Done`/`Error` → fetch `GET /messages/{merged_pk}`
and count `fields.data.message.results`. Latency is the wall-clock submit→terminal
time; one measurement is recorded per logical query. A single `ars_query` Locust
request event carries that full wall-clock, so Locust's native stats table shows
the true per-query time — the individual `ars_submit`/`ars_poll`/`ars_merge` calls
also appear there as per-step diagnostics (e.g. `ars_merge` times only the final
merge fetch, not the whole query), but aren't double-counted as the query time.

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

## How to read the results

This section is for anyone — technical or not — who has a run's output files in
hand and wants to answer one question: **how much load can this service take?**

### Plain-language key

A few terms show up everywhere in the outputs. Here's what each one means,
without the jargon:

| Term | What it means |
|------|---------------|
| **Stage** | One step of the ramp. Each stage pins a fixed number of simultaneous users for a fixed time, then the next stage adds more. The run climbs stage by stage until the service struggles. |
| **Users / concurrency** | How many simultaneous users are hammering the service. "Users" is what we *asked for* in a stage; **concurrency** is the effective number actually in flight, computed from the measured throughput and speed. |
| **RPS** | Requests Per Second — how many queries the service actually completed each second during that stage. Higher is faster. |
| **Latency** | How long one query took, in **milliseconds** (1000 ms = 1 second). |
| **mean / p50 / p95 / p99** | Different ways to summarize latency. *mean* is the average. *p50* (median) is the typical query. *p95* / *p99* are the slow tail: "95% (or 99%) of queries were at least this fast." p99 is the one we hold to a standard, because it captures the bad experiences, not just the average. |
| **Error rate** | The fraction of queries that failed (e.g. `0.02` = 2%). For the ARS, a query that finishes but returns **zero answers** also counts as a failure. |
| **SLO** | Service Level Objective — the line in the sand for "acceptable." Here it's a p99 latency cap (e.g. `60000` ms = 60 s) plus a max error rate (default **1%**). A stage "passes" only if it stays under both. |
| **Knee** | The highest-load stage that still passes the SLO. It's the headline result: the most simultaneous users the service handled while staying fast enough and reliable enough. Past the knee, things fall apart. |

### Start here: `summary.json`

Open `<prefix>_summary.json` first. Two fields tell you almost everything:

- **`max_sustainable_concurrency`** — the headline number. This is the answer to
  "how much can this service take?"
- **`knee`** — the stage that number came from (its users, latency, error rate,
  etc.). This is the *last healthy stage* of the ramp.

If **`knee` is `null`** (and `max_sustainable_concurrency` is empty), the service
**failed even at the lightest load** — it was already too slow or too error-prone
in the very first stage. That's a strong signal something is wrong (or the SLO is
set tighter than the service can ever meet).

The same file also echoes the **`config`** that produced the run (which target,
which endpoint, the SLO, the ramp stages) so a result is self-documenting.

### Reading `stages.csv` — the story of the ramp

`<prefix>_stages.csv` has one row per stage and shows the service degrading as
load climbs. Here's an illustrative example (SLO: p99 ≤ 60 000 ms, errors ≤ 1%):

| stage | users | rps  | mean_ms | p99_ms | error_rate | concurrency |
|-------|-------|------|---------|--------|------------|-------------|
| 1     | 5     | 4.8  | 1 040   | 2 100  | 0.000      | 5.0         |
| 2     | 10    | 9.1  | 1 090   | 3 400  | 0.000      | 9.9         |
| 3     | 20    | 17.0 | 1 170   | 9 800  | 0.004      | 19.9        |
| 4     | 40    | 22.0 | 1 800   | 41 000 | 0.008      | **39.6** ← knee |
| 5     | 80    | 19.0 | 4 200   | 78 000 | 0.140      | —           |

How to read it:

- **Top to bottom = more load.** Each stage adds users.
- **Watch two columns: `p99_ms` and `error_rate`.** As long as both stay under
  the SLO (here 60 000 ms and 0.01), the service is coping.
- **The knee is the last "green" row** — stage 4 above. Its `concurrency` (≈ 40)
  is the headline number.
- **The row after the knee shows the cliff:** at stage 5, p99 latency blew past
  the 60 s cap *and* the error rate jumped to 14%. Notice RPS actually *dropped*
  (19 vs 22) even though we added users — a classic sign the service is
  saturated and thrashing, not going faster.

A healthy run shows latency rising gently and errors near zero until a clear
knee, then a sharp cliff. If the very first row already breaks the SLO, you get
no knee (see above).

> **Why not just trust Locust's own totals?** During a ramp, an overall average
> would blend the easy early stages with the saturated late ones and hide the
> knee. That's why HelmsDeep reports **per stage**. (It's also why the HTML
> report's blended totals aren't the official number — see below.)

### `by_qtype.csv` — which query is the bottleneck

`<prefix>_by_qtype.csv` breaks each stage down by *type* of query (e.g. simple
single-hop lookups vs. heavier multi-hop or "what treats this disease?"
queries). Use it to answer "what's slowing things down?" — often one query
shape saturates long before the others, and that's where to focus.

### `ars_health.csv` and `red_flags` (ARS runs only)

The ARS is what real users actually hit, and it can fail *silently* — a query
can come back "successful" but with **fewer answers than it should**, or zero.
`<prefix>_ars_health.csv` and the **`red_flags`** list in `summary.json` exist to
catch exactly this. In plain terms they flag things like:

- **Answers quietly disappearing under load** — the average number of results per
  query drops sharply as concurrency rises (something downstream is dropping out).
- **"Done" but empty** — queries that finished but returned zero answers (counted
  as failures).
- **Wild result-count swings** — identical queries returning very different
  amounts, a sign of instability.

If `red_flags` is non-empty, the service may be degrading in a way the raw
latency/error numbers alone wouldn't reveal — read those flags.

### The HTML report (`<prefix>_report.html`)

This is Locust's built-in report. **Just double-click it to open in a browser.**
It gives you nice visuals: response-time and request-rate charts over the whole
run, a table of every request type, and a list of any failures. It's great for a
quick visual feel and for sharing a screenshot.

> **Important caveat:** the HTML report's headline totals are **blended across
> the entire run** — it averages the gentle early stages together with the
> overwhelmed late ones. That makes its single "average response time" / "total
> RPS" numbers *not* the right ones to quote. **For the official ceiling, always
> read the knee in `summary.json` (and the per-stage story in `stages.csv`).**
> Think of the HTML as the pictures, and `summary.json`/`stages.csv` as the
> verdict.

## Tuning notes

- **Swap the CURIEs.** The corpus in `helmsdeep/trapi_corpus.py`
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
- **Pathfinder is its own run type** (`aras_pathfinder` / `ars_pathfinder`, ARA/ARS
  only). `PATHFINDER_CORPUS` sends a single drug↔disease shape that pins **two**
  endpoints and asks for connecting paths (a `paths` map in the query_graph, not
  `edges`); the `(chemical, disease)` pair varies per request from a curated
  `CHEM_DISEASE_PAIRS` list — **swap these** for pairs your service knows, and keep
  them plausibly connected so paths come back non-empty (on ARS a zero-result
  `Done` is a failure). It's the heaviest query class, so its targets ship the
  gentlest ramps and loosest SLOs (and, for `ars_pathfinder`, the longest
  `max_poll_s`); tune them in `config.py`.
- **Adjust corpus weights** in `RETRIEVER_CORPUS` / `SHEPHERD_CORPUS` to match
  your traffic mix.

## Running the engine directly

You can also invoke the locustfile without the CLI (defaults to the `kps`
target):

```bash
LOCUST_CSV_PREFIX=run1 \
locust -f helmsdeep/trapi_loadtest.py --headless \
    --host https://your-retriever-service.example.org \
    --html run1_report.html
```

The output-file prefix comes from the `LOCUST_CSV_PREFIX` env var (locust has no
`--csv-prefix` flag). Set `LOADTEST_TARGET` to choose a different (implemented)
layer. Add `--html <name>.html` yourself to get the HTML report (the `helmsdeep`
CLI passes this for you automatically, as `<prefix>_report.html`).
