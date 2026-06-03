"""
TRAPI step-load test for NCATS Translator services.

Runs a STEPPED ramp of concurrent users. For each stage it captures:
  - RPS, mean / p50 / p95 / p99 latency, error rate
  - effective concurrency (Little's Law: RPS * mean_latency_seconds)
  - the same metrics broken out per query type (qtype)

It then determines the "knee" = the highest stage where BOTH
  p99 <= P99_SLO_MS  AND  error_rate <= MAX_ERROR_RATE
hold. That stage's effective concurrency is your max sustainable concurrency.

The component under test is chosen by the LOADTEST_TARGET env var (see config.py).
Sync targets (KP/ARA) POST once; the async ARS target submits then polls
/messages/{pk} until terminal and fetches the merged message, additionally
capturing health signals (result-count variation, zero-result "Done" responses,
response size, result-drop-under-load) and surfacing them as red flags.

Outputs:
  - <prefix>_stages.csv      one row per stage (overall)
  - <prefix>_by_qtype.csv    one row per (stage, qtype)
  - <prefix>_ars_health.csv  ARS only: per-stage health signals
  - <prefix>_summary.json    config, all stages, the knee (+ ars_health/red_flags)

Usage (headless, recommended for reproducible numbers):

  locust -f trapi_loadtest.py --headless \
      --host https://your-trapi-service.example.org \
      --csv-prefix run1            # optional; falls back to LOCUST_CSV_PREFIX env

The LoadTestShape drives users/duration, so you do NOT pass -u / -r / -t.
Per-target load/SLO and the ARS poll knobs live in config.py.
"""

import json
import os
import statistics
import time
from collections import defaultdict

import gevent
from locust import HttpUser, task, constant, events
from locust import LoadTestShape
from locust.runners import MasterRunner, WorkerRunner

import config
from trapi_corpus import corpus_for

# ----------------------------------------------------------------------------
# Configuration. Per-target load + SLO live in config.py (cost profiles differ
# wildly by layer); edit them there. REQUEST_TIMEOUT is shared.
# ----------------------------------------------------------------------------
# Which Translator layer this run targets (one layer per run; see CLAUDE.md).
# Set by the helmsdeep CLI; defaults so `locust -f` works directly.
TARGET = os.environ.get("LOADTEST_TARGET", config.DEFAULT_TARGET)
_TGT = config.TARGETS[TARGET]
ENDPOINT = _TGT["endpoint"]    # request path for this component
CORPUS = corpus_for(_TGT["corpus"])   # query subset for this component
STAGES = _TGT["stages"]               # per-target step-load ramp
P99_SLO_MS = _TGT["p99_slo_ms"]       # per-target knee threshold
MAX_ERROR_RATE = config.MAX_ERROR_RATE   # shared error-rate cap
REQUEST_TIMEOUT = 210           # seconds; per individual HTTP call

# ARS is asynchronous (submit -> poll /messages/{pk} -> fetch merged). These are
# unused by the sync (KP/ARA) path.
PROTOCOL = _TGT.get("protocol", "sync")
MESSAGES_PATH = _TGT.get("messages_endpoint", "/messages")
POLL_INTERVAL_S = _TGT.get("poll_interval_s", 10)
MAX_POLL_S = _TGT.get("max_poll_s", 900)

CSV_PREFIX = os.environ.get("LOCUST_CSV_PREFIX", "trapi_run")

# Weighted, flattened corpus for O(1)-ish random selection.
import random
_FLAT = []
for qtype, builder, weight in CORPUS:
    _FLAT.extend([(qtype, builder)] * weight)


# ----------------------------------------------------------------------------
# Per-stage metric collection.
#
# We can't trust Locust's single aggregate run stats because we ramp load --
# an aggregate p99 would blend easy early stages with saturated late ones.
# So we bucket every completed request into the stage that was active when it
# *finished*, keyed by the shape's stage index.
# ----------------------------------------------------------------------------
class StageCollector:
    def __init__(self):
        # stage_idx -> qtype -> list of latencies (ms); qtype "__all__" = overall
        self.samples = defaultdict(lambda: defaultdict(list))
        self.errors = defaultdict(lambda: defaultdict(int))
        self.requests = defaultdict(lambda: defaultdict(int))
        self.stage_idx = 0
        self.stage_started = {}   # stage_idx -> wall-clock start
        self.stage_ended = {}
        # ARS-only health signals (populated by the async path).
        self.result_counts = defaultdict(lambda: defaultdict(list))   # answers per query
        self.response_bytes = defaultdict(lambda: defaultdict(list))  # merged-msg size
        self.statuses = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))  # status -> n

    def mark_stage(self, idx):
        if idx != self.stage_idx:
            self.stage_ended[self.stage_idx] = time.time()
        self.stage_idx = idx
        self.stage_started.setdefault(idx, time.time())

    def record(self, qtype, latency_ms, failed, *,
               status=None, result_count=None, response_bytes=None):
        s = self.stage_idx
        self.requests[s][qtype] += 1
        self.requests[s]["__all__"] += 1
        if failed:
            self.errors[s][qtype] += 1
            self.errors[s]["__all__"] += 1
        else:
            self.samples[s][qtype].append(latency_ms)
            self.samples[s]["__all__"].append(latency_ms)
        # Optional ARS health signals (recorded regardless of pass/fail so a
        # zero-result "Done" still shows up in the distributions).
        if status is not None:
            self.statuses[s][qtype][status] += 1
            self.statuses[s]["__all__"][status] += 1
        if result_count is not None:
            self.result_counts[s][qtype].append(result_count)
            self.result_counts[s]["__all__"].append(result_count)
        if response_bytes is not None:
            self.response_bytes[s][qtype].append(response_bytes)
            self.response_bytes[s]["__all__"].append(response_bytes)


COLLECTOR = StageCollector()


def _pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _stage_stats(idx, qtype):
    lat = COLLECTOR.samples[idx][qtype]
    n_req = COLLECTOR.requests[idx][qtype]
    n_err = COLLECTOR.errors[idx][qtype]
    started = COLLECTOR.stage_started.get(idx)
    ended = COLLECTOR.stage_ended.get(idx, time.time())
    dur = max(ended - started, 1e-9) if started else 1e-9
    rps = n_req / dur
    mean = sum(lat) / len(lat) if lat else 0.0
    return {
        "stage": idx,
        "qtype": qtype,
        "users": STAGES[idx][0] if idx < len(STAGES) else None,
        "requests": n_req,
        "errors": n_err,
        "error_rate": (n_err / n_req) if n_req else 0.0,
        "duration_s": round(dur, 2),
        "rps": round(rps, 3),
        "mean_ms": round(mean, 2),
        "p50_ms": round(_pct(lat, 50), 2),
        "p95_ms": round(_pct(lat, 95), 2),
        "p99_ms": round(_pct(lat, 99), 2),
        # Little's Law: concurrency = throughput * latency(seconds)
        "concurrency": round(rps * (mean / 1000.0), 2),
    }


def _ars_stage_health(idx, qtype="__all__"):
    """ARS-only per-stage health signals derived from the collector.

    Result counts/bytes are recorded for every query that reached a merged
    message (including zero-result Done), so the distributions capture answers
    thinning out or sizes drifting under load -- signals that something
    downstream broke even when the request itself "succeeded".
    """
    counts = COLLECTOR.result_counts[idx][qtype]
    nbytes = COLLECTOR.response_bytes[idx][qtype]
    statuses = COLLECTOR.statuses[idx][qtype]
    mean = (sum(counts) / len(counts)) if counts else 0.0
    cv = (statistics.pstdev(counts) / mean) if (counts and mean) else 0.0
    return {
        "stage": idx,
        "requests": COLLECTOR.requests[idx][qtype],
        "done": statuses.get("Done", 0),
        "errored": statuses.get("Error", 0),
        "timed_out": statuses.get("Timeout", 0),
        "submit_failed": statuses.get("SubmitError", 0) + statuses.get("NoPK", 0),
        "zero_result_done": sum(1 for c in counts if c == 0),
        "results_min": min(counts) if counts else 0,
        "results_mean": round(mean, 2),
        "results_max": max(counts) if counts else 0,
        "results_cv": round(cv, 3),
        "bytes_mean": round(sum(nbytes) / len(nbytes), 1) if nbytes else 0,
        "bytes_max": max(nbytes) if nbytes else 0,
    }


# ----------------------------------------------------------------------------
# The user: picks a weighted TRAPI query, POSTs it, records by qtype.
# Sync targets (KP/ARA) POST once; the ARS target submits then polls/merges.
# ----------------------------------------------------------------------------
class TRAPIUser(HttpUser):
    wait_time = constant(0)   # closed-loop; shape controls concurrency

    @task
    def query(self):
        qtype, builder = random.choice(_FLAT)
        payload = builder()
        if PROTOCOL == "async":
            self._run_ars(qtype, payload)
        else:
            self._run_sync(qtype, payload)

    def _run_sync(self, qtype, payload):
        """KP/ARA: a single blocking POST; success == HTTP 200."""
        # name= groups all requests of a qtype under one label in Locust's UI
        with self.client.post(
            ENDPOINT,
            json=payload,
            name=qtype,
            timeout=REQUEST_TIMEOUT,
            catch_response=True,
        ) as resp:
            failed = False
            # malformed_query is expected to 4xx -- treat that as a successful
            # measurement of the error path, not a load-test failure.
            if qtype == "malformed_query":
                if resp.status_code < 500:
                    resp.success()
                else:
                    failed = True
                    resp.failure(f"server error {resp.status_code}")
            else:
                if resp.status_code == 200:
                    resp.success()
                else:
                    failed = True
                    resp.failure(f"status {resp.status_code}")
            latency_ms = resp.request_meta["response_time"] or 0.0
            COLLECTOR.record(qtype, latency_ms, failed)

    def _run_ars(self, qtype, payload):
        """ARS: submit -> poll /messages/{pk} until terminal -> fetch merged.

        Latency is wall-clock submit->terminal. One COLLECTOR.record per logical
        query (the submit/poll/merge HTTP calls show separately in Locust's own
        table but are not double-counted). A Done with 0 results is a failure.
        """
        start = time.time()

        def _elapsed_ms():
            return (time.time() - start) * 1000.0

        # 1) Submit -> pk.
        with self.client.post(
            ENDPOINT, json=payload, name="ars_submit",
            timeout=REQUEST_TIMEOUT, catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"submit status {resp.status_code}")
                COLLECTOR.record(qtype, _elapsed_ms(), True, status="SubmitError")
                return
            try:
                pk = (resp.json() or {}).get("pk")
            except Exception:
                pk = None
            if not pk:
                resp.failure("no pk in submit response")
                COLLECTOR.record(qtype, _elapsed_ms(), True, status="NoPK")
                return
            resp.success()

        # 2) Poll until Done/Error or the per-query cap.
        status = None
        merged_pk = None
        deadline = start + MAX_POLL_S
        while time.time() < deadline:
            gevent.sleep(POLL_INTERVAL_S)   # cooperative; never time.sleep
            with self.client.get(
                f"{MESSAGES_PATH}/{pk}?trace=y", name="ars_poll",
                timeout=REQUEST_TIMEOUT, catch_response=True,
            ) as resp:
                if resp.status_code != 200:
                    resp.failure(f"poll status {resp.status_code}")
                    continue   # transient; keep polling until the deadline
                resp.success()
                try:
                    body = resp.json() or {}
                except Exception:
                    continue
                status = body.get("status")
                merged_pk = body.get("merged_version") or merged_pk
                if status in ("Done", "Error"):
                    break

        # 3) Terminal handling.
        if status == "Done":
            result_count, nbytes = self._fetch_merged(merged_pk)
            COLLECTOR.record(
                qtype, _elapsed_ms(), failed=(result_count == 0),
                status="Done", result_count=result_count, response_bytes=nbytes,
            )
        elif status == "Error":
            COLLECTOR.record(qtype, _elapsed_ms(), True, status="Error")
        else:
            COLLECTOR.record(qtype, _elapsed_ms(), True, status="Timeout")

    def _fetch_merged(self, merged_pk):
        """Fetch the merged message; return (result_count, response_bytes)."""
        if not merged_pk:
            return 0, 0
        with self.client.get(
            f"{MESSAGES_PATH}/{merged_pk}", name="ars_merge",
            timeout=REQUEST_TIMEOUT, catch_response=True,
        ) as resp:
            nbytes = len(resp.content or b"")
            if resp.status_code != 200:
                resp.failure(f"merge status {resp.status_code}")
                return 0, nbytes
            resp.success()
            try:
                body = resp.json() or {}
                results = (((body.get("fields") or {}).get("data") or {})
                           .get("message") or {}).get("results") or []
                return len(results), nbytes
            except Exception:
                return 0, nbytes


# ----------------------------------------------------------------------------
# Step-load shape. tick() returns (user_count, spawn_rate) or None to stop.
# It also tells the collector which stage is active.
# ----------------------------------------------------------------------------
class StepLoad(LoadTestShape):
    def __init__(self):
        super().__init__()
        self._bounds = []
        t = 0
        for i, (_users, _rate, hold) in enumerate(STAGES):
            self._bounds.append((t, t + hold, i))
            t += hold
        self._total = t

    def tick(self):
        run_time = self.get_run_time()
        if run_time > self._total:
            return None
        for start, end, idx in self._bounds:
            if start <= run_time < end:
                COLLECTOR.mark_stage(idx)
                users, rate, _hold = STAGES[idx]
                return (users, rate)
        return None


# ----------------------------------------------------------------------------
# On test stop, compute per-stage stats, find the knee, write outputs.
# Only the master/standalone node writes files.
# ----------------------------------------------------------------------------
@events.test_stop.add_listener
def on_test_stop(environment, **_kw):
    if isinstance(environment.runner, WorkerRunner):
        return

    COLLECTOR.stage_ended.setdefault(COLLECTOR.stage_idx, time.time())

    overall_rows = []
    qtype_rows = []
    seen_stages = sorted(COLLECTOR.requests.keys())
    qtypes = [c[0] for c in CORPUS]

    for idx in seen_stages:
        overall_rows.append(_stage_stats(idx, "__all__"))
        for qt in qtypes:
            if COLLECTOR.requests[idx].get(qt):
                qtype_rows.append(_stage_stats(idx, qt))

    # Knee = highest stage meeting BOTH p99 SLO and error-rate cap.
    knee = None
    for row in overall_rows:
        if row["p99_ms"] <= P99_SLO_MS and row["error_rate"] <= MAX_ERROR_RATE:
            if knee is None or row["concurrency"] > knee["concurrency"]:
                knee = row

    # ARS health signals + red flags (async target only).
    ars_health = []
    red_flags = []
    if PROTOCOL == "async":
        ars_health = [_ars_stage_health(idx) for idx in seen_stages]
        prev = None
        for h in ars_health:
            i = h["stage"]
            if h["zero_result_done"]:
                red_flags.append(
                    f"Stage {i}: {h['zero_result_done']} 'Done' response(s) returned "
                    f"0 results (counted as failures; possible silent downstream break).")
            if h["errored"]:
                red_flags.append(f"Stage {i}: {h['errored']} query/queries returned Error status.")
            if h["timed_out"]:
                red_flags.append(
                    f"Stage {i}: {h['timed_out']} query/queries did not reach a terminal "
                    f"status within {MAX_POLL_S}s.")
            if h["submit_failed"]:
                red_flags.append(f"Stage {i}: {h['submit_failed']} submit failure(s) (no pk / bad status).")
            if h["requests"] >= 3 and h["results_mean"] > 0 and h["results_cv"] > 0.5:
                red_flags.append(
                    f"Stage {i}: high result-count variability (CV={h['results_cv']}, "
                    f"min={h['results_min']}, max={h['results_max']}) across identical queries.")
            if h["bytes_mean"] and h["bytes_max"] > 3 * h["bytes_mean"]:
                red_flags.append(
                    f"Stage {i}: abnormal response-size spread "
                    f"(max={h['bytes_max']}B vs mean={h['bytes_mean']}B).")
            # Result counts thinning out as concurrency rises.
            if (prev and prev["results_mean"] > 0 and h["results_mean"] > 0
                    and h["results_mean"] < 0.5 * prev["results_mean"]):
                red_flags.append(
                    f"Stage {i}: mean result count dropped under load "
                    f"({prev['results_mean']} -> {h['results_mean']} vs stage {prev['stage']}); "
                    f"the system may be shedding answers as concurrency rises.")
            prev = h

    # Write overall CSV.
    fields = ["stage", "users", "requests", "errors", "error_rate",
              "duration_s", "rps", "mean_ms", "p50_ms", "p95_ms", "p99_ms",
              "concurrency"]
    with open(f"{CSV_PREFIX}_stages.csv", "w") as f:
        f.write(",".join(fields) + "\n")
        for r in overall_rows:
            f.write(",".join(str(r[k]) for k in fields) + "\n")

    # Write per-qtype CSV.
    qfields = ["stage", "qtype", "users", "requests", "errors", "error_rate",
               "rps", "mean_ms", "p50_ms", "p95_ms", "p99_ms", "concurrency"]
    with open(f"{CSV_PREFIX}_by_qtype.csv", "w") as f:
        f.write(",".join(qfields) + "\n")
        for r in qtype_rows:
            f.write(",".join(str(r[k]) for k in qfields) + "\n")

    # Write ARS health CSV (async target only).
    hfields = ["stage", "requests", "done", "errored", "timed_out",
               "submit_failed", "zero_result_done", "results_min",
               "results_mean", "results_max", "results_cv", "bytes_mean",
               "bytes_max"]
    if ars_health:
        with open(f"{CSV_PREFIX}_ars_health.csv", "w") as f:
            f.write(",".join(hfields) + "\n")
            for r in ars_health:
                f.write(",".join(str(r[k]) for k in hfields) + "\n")

    summary = {
        "config": {
            "target": TARGET,
            "component": _TGT["label"],
            "endpoint": ENDPOINT,
            "protocol": PROTOCOL,
            "p99_slo_ms": P99_SLO_MS,
            "max_error_rate": MAX_ERROR_RATE,
            "stages": STAGES,
            "corpus_weights": {c[0]: c[2] for c in CORPUS},
        },
        "stages": overall_rows,
        "knee": knee,
        "max_sustainable_concurrency": knee["concurrency"] if knee else None,
    }
    if PROTOCOL == "async":
        summary["ars_health"] = ars_health
        summary["red_flags"] = red_flags
    with open(f"{CSV_PREFIX}_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print("\n" + "=" * 64)
    print("TRAPI LOAD TEST SUMMARY")
    print("=" * 64)
    hdr = f"{'stg':>3} {'usr':>4} {'rps':>7} {'p50':>7} {'p95':>8} {'p99':>8} {'err%':>6} {'conc':>7}"
    print(hdr)
    for r in overall_rows:
        print(f"{r['stage']:>3} {str(r['users']):>4} {r['rps']:>7.1f} "
              f"{r['p50_ms']:>7.0f} {r['p95_ms']:>8.0f} {r['p99_ms']:>8.0f} "
              f"{r['error_rate']*100:>5.1f}% {r['concurrency']:>7.1f}")
    print("-" * 64)
    if knee:
        print(f"Max sustainable concurrency: {knee['concurrency']} "
              f"(stage {knee['stage']}, {knee['users']} users, "
              f"p99={knee['p99_ms']:.0f}ms, err={knee['error_rate']*100:.1f}%)")
    else:
        print("No stage met the SLO -- service saturated below the first stage, "
              "or SLO is too strict. Lower the first stage or relax P99_SLO_MS.")

    wrote = [f"{CSV_PREFIX}_stages.csv", f"{CSV_PREFIX}_by_qtype.csv"]
    if ars_health:
        print("-" * 64)
        print("ARS HEALTH (per stage)")
        hh = (f"{'stg':>3} {'req':>4} {'done':>5} {'err':>4} {'t/o':>4} "
              f"{'0res':>5} {'res_mean':>9} {'res_cv':>7} {'bytes_max':>10}")
        print(hh)
        for h in ars_health:
            print(f"{h['stage']:>3} {h['requests']:>4} {h['done']:>5} "
                  f"{h['errored']:>4} {h['timed_out']:>4} {h['zero_result_done']:>5} "
                  f"{h['results_mean']:>9} {h['results_cv']:>7} {h['bytes_max']:>10}")
        print("-" * 64)
        if red_flags:
            print(f"RED FLAGS ({len(red_flags)}):")
            for msg in red_flags:
                print(f"  [!] {msg}")
        else:
            print("RED FLAGS: none detected.")
        wrote.append(f"{CSV_PREFIX}_ars_health.csv")
    wrote.append(f"{CSV_PREFIX}_summary.json")
    print(f"Wrote: {', '.join(wrote)}")
    print("=" * 64 + "\n")
