"""
TRAPI step-load test for NCATS Translator services.

Runs a STEPPED ramp of concurrent users. For each stage it captures:
  - RPS, mean / p50 / p95 / p99 latency, error rate
  - effective concurrency (Little's Law: RPS * mean_latency_seconds)
  - the same metrics broken out per query type (qtype)

It then determines the "knee" = the highest stage where BOTH
  p99 <= P99_SLO_MS  AND  error_rate <= MAX_ERROR_RATE
hold. That stage's effective concurrency is your max sustainable concurrency.

Outputs:
  - <prefix>_stages.csv     one row per stage (overall)
  - <prefix>_by_qtype.csv   one row per (stage, qtype)
  - <prefix>_summary.json   config, all stages, and the chosen knee

Usage (headless, recommended for reproducible numbers):

  locust -f trapi_loadtest.py --headless \
      --host https://your-trapi-service.example.org \
      --csv-prefix run1            # optional; falls back to LOCUST_CSV_PREFIX env

The LoadTestShape drives users/duration, so you do NOT pass -u / -r / -t.
Tune ENDPOINT, the STAGES table, and the SLO constants below.
"""

import json
import os
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
# Set by the run_performance_tests CLI; defaults so `locust -f` works directly.
TARGET = os.environ.get("LOADTEST_TARGET", config.DEFAULT_TARGET)
_TGT = config.TARGETS[TARGET]
ENDPOINT = _TGT["endpoint"]    # request path for this component
CORPUS = corpus_for(_TGT["corpus"])   # query subset for this component
STAGES = _TGT["stages"]               # per-target step-load ramp
P99_SLO_MS = _TGT["p99_slo_ms"]       # per-target knee threshold
MAX_ERROR_RATE = config.MAX_ERROR_RATE   # shared error-rate cap
REQUEST_TIMEOUT = 210           # seconds; TRAPI queries can be slow

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

    def mark_stage(self, idx):
        if idx != self.stage_idx:
            self.stage_ended[self.stage_idx] = time.time()
        self.stage_idx = idx
        self.stage_started.setdefault(idx, time.time())

    def record(self, qtype, latency_ms, failed):
        s = self.stage_idx
        self.requests[s][qtype] += 1
        self.requests[s]["__all__"] += 1
        if failed:
            self.errors[s][qtype] += 1
            self.errors[s]["__all__"] += 1
        else:
            self.samples[s][qtype].append(latency_ms)
            self.samples[s]["__all__"].append(latency_ms)


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


# ----------------------------------------------------------------------------
# The user: picks a weighted TRAPI query, POSTs it, records by qtype.
# ----------------------------------------------------------------------------
class TRAPIUser(HttpUser):
    wait_time = constant(0)   # closed-loop; shape controls concurrency

    @task
    def query(self):
        qtype, builder = random.choice(_FLAT)
        payload = builder()
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

    summary = {
        "config": {
            "target": TARGET,
            "component": _TGT["label"],
            "endpoint": ENDPOINT,
            "p99_slo_ms": P99_SLO_MS,
            "max_error_rate": MAX_ERROR_RATE,
            "stages": STAGES,
            "corpus_weights": {c[0]: c[2] for c in CORPUS},
        },
        "stages": overall_rows,
        "knee": knee,
        "max_sustainable_concurrency": knee["concurrency"] if knee else None,
    }
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
    print(f"Wrote: {CSV_PREFIX}_stages.csv, {CSV_PREFIX}_by_qtype.csv, "
          f"{CSV_PREFIX}_summary.json")
    print("=" * 64 + "\n")
