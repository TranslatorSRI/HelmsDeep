"""
``helmsdeep-profile`` -- per-CURIE cost profiler for the MVP1/MVP2 corpus.

This is **not** a load test. It is the one-time profiling pass the roadmap asks
for (CLAUDE.md, "Calibrate the inferred tiers"): walk *every* entity the
inferred corpus can sample -- every MVP1 disease, every MVP2 gene and chemical
-- send that entity's query once (or ``--repeat`` times) at low concurrency,
and record what it cost:

  - wall-clock response time
  - result count (the answer-set size that dominates inferred-query cost)
  - knowledge-graph size (nodes/edges), which keeps meaning something when a
    service caps ``results``

The output is one row per query (``<prefix>_probe.csv``), a per-CURIE roll-up
(``<prefix>_entities.csv``), and a suggested re-binning of each entity pool into
light/medium/heavy by measured result count (``<prefix>_bins.json``). That last
file is the input to splitting ``LONG_TAIL_DISEASES``: today ``mvp1_medium`` and
``mvp1_light`` draw from the same pool because no per-disease size data existed.

The queries are built by calling ``trapi_corpus``'s own builders with a pinned
entity instead of a sampled one, so a profiled query is exactly the query the
load test sends -- same envelope, same qualifiers, same ``bypass_cache``.

Both protocols are supported, selected with ``--target`` (endpoint, poll knobs,
and timeouts come from ``config.TARGETS``):

  aras   sync   POST /query, read ``message.results``
  ars    async  POST /submit -> poll /messages/{pk} -> fetch merged message

Profiling the ARS is far slower (minutes per query, and the poll interval
quantizes the latency); for ~1000 diseases, profile the ARA and use
``--sample``/``--limit`` against the ARS as a spot check.

Because a full MVP1 pass is a long run, every row is flushed to the CSV as it
completes, ``--resume`` skips entities already measured successfully, and Ctrl-C
stops scheduling and still writes the roll-up from what was collected.

Load rule: this walks one layer, like every other HelmsDeep run (CLAUDE.md
"Layering rule"). Keep ``--concurrency`` low -- the goal is to measure each
query's natural cost, not to put the service under load. A profiling pass run
at high concurrency measures queueing, not answer-set size.

Usage:
    helmsdeep-profile --host https://your-ara.example.org --out mvp_profile
    helmsdeep-profile --host https://your-ara.example.org --out mvp_profile \\
        --qtypes mvp1 --sample 200 --concurrency 4
    helmsdeep-profile --summarize-only mvp_profile_probe.csv --out mvp_profile
"""

import argparse
import csv
import json
import os
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from . import config
from . import console
from . import trapi_corpus as corpus

# Targets whose corpus is the inferred MVP1/MVP2 mix. Pathfinder pins two
# endpoints and is a different question entirely, so it is not profilable here.
PROFILABLE_TARGETS = ("aras", "ars")

# MVP1 qtype for the profiler. The corpus splits MVP1 into heavy/medium/light --
# which is precisely the split this tool exists to re-derive -- so every MVP1
# probe carries one qtype and records which pool the CURIE comes from today.
MVP1_QTYPE = "mvp1_treats"

CSV_FIELDS = [
    "timestamp", "qtype", "mvp", "entity", "pool", "variant", "rep",
    "ok", "status", "http_status", "latency_s", "result_count",
    "kg_nodes", "kg_edges", "response_bytes", "pk", "error",
]

ENTITY_FIELDS = [
    "qtype", "mvp", "entity", "pool", "n", "n_ok",
    "result_count_median", "result_count_min", "result_count_max",
    "latency_s_median", "latency_s_min", "latency_s_max",
    "kg_nodes_median", "kg_edges_median", "suggested_bin",
]

DEFAULT_CUTS = (0.33, 0.67)
BIN_NAMES = ("light", "medium", "heavy")

USER_AGENT = "helmsdeep-profile/0.1 (+https://github.com/TranslatorSRI/StressTester)"


# ---------------------------------------------------------------------------
# The probe plan: one entity, one query, one row.
# ---------------------------------------------------------------------------
@dataclass
class Probe:
    """One (entity, variant) query to send, ``rep`` times."""
    qtype: str
    mvp: str
    entity: str
    pool: str
    variant: str
    rep: int
    payload: dict = field(repr=False)

    @property
    def key(self):
        """Identity for --resume: the same probe never needs measuring twice."""
        return (self.qtype, self.entity, self.variant, self.rep)


def _mvp1_entities():
    """Every disease the MVP1 corpus can pin, tagged with its current pool.

    ``HEAVY_DISEASES`` is a curated list and ``LONG_TAIL_DISEASES`` comes from
    ``curie_list.json``; they overlap, and the curated label is the interesting
    one (it is a claim this tool is checking), so it wins on a duplicate.
    """
    seen = {}
    for curie in corpus.HEAVY_DISEASES:
        seen.setdefault(curie, "heavy_curated")
    for curie in corpus.LONG_TAIL_DISEASES:
        seen.setdefault(curie, "long_tail")
    return list(seen.items())


def build_probes(qtypes, directions, repeat=1):
    """Expand the entity pools into the full list of probes to send."""
    probes = []
    for rep in range(1, repeat + 1):
        if "mvp1" in qtypes:
            for entity, pool in _mvp1_entities():
                probes.append(Probe(
                    qtype=MVP1_QTYPE, mvp="mvp1", entity=entity, pool=pool,
                    variant="", rep=rep,
                    payload=corpus._inferred_treats(entity),
                ))
        if "mvp2" in qtypes:
            for direction in directions:
                # Gene pinned: "what chemicals change gene X?"
                for gene in corpus.GENES:
                    probes.append(Probe(
                        qtype="mvp2_chem_affects_gene", mvp="mvp2",
                        entity=gene, pool="genes", variant=direction, rep=rep,
                        payload=corpus._affects(
                            {"categories": ["biolink:ChemicalEntity"]},
                            {"ids": [gene], "categories": ["biolink:Gene"]},
                            corpus._ASPECT, direction,
                        ),
                    ))
                # Chemical pinned: "what genes does chemical X affect?"
                for chem in corpus.CHEMICALS:
                    probes.append(Probe(
                        qtype="mvp2_chem_affects_open_gene", mvp="mvp2",
                        entity=chem, pool="chemicals", variant=direction,
                        rep=rep,
                        payload=corpus._affects(
                            {"ids": [chem],
                             "categories": ["biolink:ChemicalEntity"]},
                            {"categories": ["biolink:Gene"]},
                            corpus._ASPECT, direction,
                        ),
                    ))
    return probes


# ---------------------------------------------------------------------------
# Response parsing. A sync ARA answers with the TRAPI message inline; the ARS
# merged message nests it under fields.data. Read either, and report the
# knowledge-graph size too -- result counts are often capped by the service,
# graph size is not, so it keeps discriminating at the top of the range.
# ---------------------------------------------------------------------------
def _message(body):
    if not isinstance(body, dict):
        return None
    message = body.get("message")
    if isinstance(message, dict):
        return message
    nested = (((body.get("fields") or {}).get("data") or {}).get("message"))
    return nested if isinstance(nested, dict) else None


def _counts(body):
    """Return ``(result_count, kg_nodes, kg_edges)``; ``None`` where unreadable."""
    message = _message(body)
    if message is None:
        return None, None, None
    results = message.get("results")
    n_results = len(results) if isinstance(results, list) else None
    kg = message.get("knowledge_graph")
    if isinstance(kg, dict):
        nodes = kg.get("nodes")
        edges = kg.get("edges")
        n_nodes = len(nodes) if isinstance(nodes, (dict, list)) else None
        n_edges = len(edges) if isinstance(edges, (dict, list)) else None
    else:
        n_nodes = n_edges = None
    return n_results, n_nodes, n_edges


# ---------------------------------------------------------------------------
# The two protocols. Each returns a dict of the CSV's measurement columns; the
# caller stamps on the probe's identity.
# ---------------------------------------------------------------------------
class Prober:
    """Sends one probe and reports what it cost. One instance per run."""

    def __init__(self, host, cfg, request_timeout, poll_interval_s, max_poll_s,
                 stop=None):
        self.host = host.rstrip("/")
        self.endpoint = cfg["endpoint"]
        self.protocol = cfg.get("protocol", "sync")
        self.messages_path = cfg.get("messages_endpoint", "/messages")
        self.request_timeout = request_timeout
        self.poll_interval_s = poll_interval_s
        self.max_poll_s = max_poll_s
        self.stop = stop or threading.Event()
        # requests.Session is not thread-safe in general; give each worker
        # thread its own so connection pooling is still a win.
        self._local = threading.local()

    @property
    def session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT})
            self._local.session = session
        return session

    def run(self, probe):
        if self.protocol == "async":
            return self._run_ars(probe)
        return self._run_sync(probe)

    # -- sync (ARA) ---------------------------------------------------------
    def _run_sync(self, probe):
        url = self.host + self.endpoint
        start = time.time()
        try:
            resp = self.session.post(url, json=probe.payload,
                                     timeout=self.request_timeout)
        except requests.Timeout:
            return self._row(start, ok=False, status="Timeout",
                             error=f"no response within {self.request_timeout}s")
        except requests.RequestException as exc:
            return self._row(start, ok=False, status="RequestError",
                             error=str(exc)[:200])
        nbytes = len(resp.content or b"")
        if resp.status_code != 200:
            return self._row(start, ok=False, status=f"HTTP {resp.status_code}",
                             http_status=resp.status_code, response_bytes=nbytes,
                             error=f"status {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            return self._row(start, ok=False, status="Unparseable",
                             http_status=resp.status_code, response_bytes=nbytes,
                             error="response body is not JSON")
        n_results, n_nodes, n_edges = _counts(body)
        if n_results is None:
            return self._row(start, ok=False, status="Unparseable",
                             http_status=resp.status_code, response_bytes=nbytes,
                             error="no message.results in response")
        return self._row(start, ok=True, status="Done",
                         http_status=resp.status_code, result_count=n_results,
                         kg_nodes=n_nodes, kg_edges=n_edges,
                         response_bytes=nbytes)

    # -- async (ARS) --------------------------------------------------------
    def _run_ars(self, probe):
        """submit -> poll /messages/{pk} until terminal -> fetch merged.

        Mirrors ``trapi_loadtest.TRAPIUser._run_ars``: latency is the wall clock
        from submit to the merged message being read, and a non-terminal status
        past ``max_poll_s`` is a Timeout. Note the poll interval quantizes the
        latency -- a 10s interval measures response time to the nearest 10s.
        """
        start = time.time()
        try:
            resp = self.session.post(self.host + self.endpoint,
                                     json=probe.payload,
                                     timeout=self.request_timeout)
        except requests.RequestException as exc:
            return self._row(start, ok=False, status="SubmitError",
                             error=str(exc)[:200])
        if resp.status_code != 201:
            return self._row(start, ok=False, status="SubmitError",
                             http_status=resp.status_code,
                             error=f"submit status {resp.status_code}")
        try:
            pk = (resp.json() or {}).get("pk")
        except ValueError:
            pk = None
        if not pk:
            return self._row(start, ok=False, status="NoPK",
                             http_status=resp.status_code,
                             error="no pk in submit response")

        status = None
        merged_pk = None
        last_http = None
        deadline = start + self.max_poll_s
        while time.time() < deadline and not self.stop.is_set():
            time.sleep(min(self.poll_interval_s, max(0.0, deadline - time.time())))
            if self.stop.is_set():
                break
            try:
                poll = self.session.get(
                    f"{self.host}{self.messages_path}/{pk}?trace=y",
                    timeout=self.request_timeout)
            except requests.RequestException:
                continue        # transient; keep polling until the deadline
            last_http = poll.status_code
            if poll.status_code != 200:
                continue
            try:
                body = poll.json() or {}
            except ValueError:
                continue
            status = body.get("status")
            merged_pk = body.get("merged_version") or merged_pk
            if status in ("Done", "Error"):
                break

        if status == "Error":
            return self._row(start, ok=False, status="Error", pk=pk,
                             http_status=last_http, error="ARS Error status")
        if status != "Done":
            interrupted = self.stop.is_set()
            return self._row(
                start, ok=False,
                status=("Interrupted" if interrupted else "Timeout"),
                pk=pk, http_status=last_http,
                error=(("stopped while still polling"
                        if interrupted
                        else f"no terminal status within {self.max_poll_s}s")
                       + (f" (last: {status})" if status else "")))
        if not merged_pk:
            return self._row(start, ok=False, status="NoMerged", pk=pk,
                             http_status=last_http,
                             error="Done without merged_version")
        try:
            merged = self.session.get(
                f"{self.host}{self.messages_path}/{merged_pk}",
                timeout=self.request_timeout)
        except requests.RequestException as exc:
            return self._row(start, ok=False, status="MergeError", pk=pk,
                             error=str(exc)[:200])
        nbytes = len(merged.content or b"")
        if merged.status_code != 200:
            return self._row(start, ok=False, status="MergeError", pk=pk,
                             http_status=merged.status_code,
                             response_bytes=nbytes,
                             error=f"merge status {merged.status_code}")
        try:
            body = merged.json() or {}
        except ValueError:
            return self._row(start, ok=False, status="Unparseable", pk=pk,
                             http_status=merged.status_code,
                             response_bytes=nbytes,
                             error="merged message is not JSON")
        n_results, n_nodes, n_edges = _counts(body)
        if n_results is None:
            return self._row(start, ok=False, status="Unparseable", pk=pk,
                             http_status=merged.status_code,
                             response_bytes=nbytes,
                             error="no message.results in merged message")
        # A Done with 0 results is a real measurement here, not a failure: an
        # empty answer set is exactly what puts a CURIE in the light bin.
        return self._row(start, ok=True, status="Done", pk=pk,
                         http_status=merged.status_code, result_count=n_results,
                         kg_nodes=n_nodes, kg_edges=n_edges,
                         response_bytes=nbytes)

    @staticmethod
    def _row(start, ok, status, http_status=None, result_count=None,
             kg_nodes=None, kg_edges=None, response_bytes=None, pk=None,
             error=None):
        return {
            "ok": 1 if ok else 0,
            "status": status,
            "http_status": http_status if http_status is not None else "",
            "latency_s": round(time.time() - start, 3),
            "result_count": result_count if result_count is not None else "",
            "kg_nodes": kg_nodes if kg_nodes is not None else "",
            "kg_edges": kg_edges if kg_edges is not None else "",
            "response_bytes": response_bytes if response_bytes is not None else "",
            "pk": pk or "",
            "error": error or "",
        }


# ---------------------------------------------------------------------------
# Incremental CSV. A full MVP1 pass is hours long; every row hits the disk as it
# lands so a killed run keeps everything it measured.
# ---------------------------------------------------------------------------
class RowWriter:
    def __init__(self, path):
        self.path = path
        fresh = not os.path.exists(path) or os.path.getsize(path) == 0
        self._fh = open(path, "a", newline="")
        self._writer = csv.DictWriter(self._fh, fieldnames=CSV_FIELDS)
        if fresh:
            self._writer.writeheader()
            self._fh.flush()
        self._lock = threading.Lock()

    def write(self, row):
        with self._lock:
            self._writer.writerow(row)
            self._fh.flush()

    def close(self):
        self._fh.close()


def read_rows(path):
    """Read a probe CSV back (for --resume and --summarize-only)."""
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def measured_keys(rows):
    """Probe keys already measured successfully -- what --resume skips."""
    return {(r["qtype"], r["entity"], r.get("variant", ""), int(r["rep"] or 1))
            for r in rows if r.get("ok") == "1"}


# ---------------------------------------------------------------------------
# Roll-up + suggested bins. This is the deliverable the corpus consumes: each
# entity's typical cost, and which third of its pool that puts it in.
# ---------------------------------------------------------------------------
def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _median(values):
    return statistics.median(values) if values else None


def entity_summary(rows):
    """Collapse probe rows to one record per (qtype, entity)."""
    grouped = {}
    for row in rows:
        key = (row["qtype"], row["entity"])
        rec = grouped.setdefault(key, {
            "qtype": row["qtype"], "mvp": row.get("mvp", ""),
            "entity": row["entity"], "pool": row.get("pool", ""),
            "n": 0, "n_ok": 0,
            "results": [], "latencies": [], "nodes": [], "edges": [],
        })
        rec["n"] += 1
        if row.get("ok") != "1":
            continue
        rec["n_ok"] += 1
        for src, dest in (("result_count", "results"), ("latency_s", "latencies"),
                          ("kg_nodes", "nodes"), ("kg_edges", "edges")):
            value = _num(row.get(src))
            if value is not None:
                rec[dest].append(value)

    out = []
    for rec in grouped.values():
        results, latencies = rec["results"], rec["latencies"]
        out.append({
            "qtype": rec["qtype"], "mvp": rec["mvp"], "entity": rec["entity"],
            "pool": rec["pool"], "n": rec["n"], "n_ok": rec["n_ok"],
            "result_count_median": _median(results),
            "result_count_min": min(results) if results else None,
            "result_count_max": max(results) if results else None,
            "latency_s_median": _median(latencies),
            "latency_s_min": min(latencies) if latencies else None,
            "latency_s_max": max(latencies) if latencies else None,
            "kg_nodes_median": _median(rec["nodes"]),
            "kg_edges_median": _median(rec["edges"]),
            "suggested_bin": "unmeasured",
        })
    out.sort(key=lambda r: (r["qtype"], -(r["result_count_median"] or -1),
                            r["entity"]))
    return out


def assign_bins(summary, metric="result_count_median", cuts=DEFAULT_CUTS):
    """Split each qtype's measured entities into light/medium/heavy.

    Cuts are quantiles of the metric *within a qtype* -- diseases are binned
    against diseases, genes against genes. Entities with no successful probe
    stay ``unmeasured`` and are reported separately rather than silently
    dropped into a bin they were never measured for.

    Returns ``{qtype: {"thresholds": {...}, "pools": {bin: [curie, ...]}}}``.
    """
    lo_cut, hi_cut = cuts
    bins = {}
    by_qtype = {}
    for rec in summary:
        by_qtype.setdefault(rec["qtype"], []).append(rec)

    for qtype, records in by_qtype.items():
        measured = [r for r in records if r[metric] is not None]
        unmeasured = [r["entity"] for r in records if r[metric] is None]
        if not measured:
            bins[qtype] = {"metric": metric, "thresholds": None,
                           "pools": {name: [] for name in BIN_NAMES},
                           "unmeasured": unmeasured}
            continue
        values = [r[metric] for r in measured]
        light_max = console._pct(values, lo_cut * 100)
        medium_max = console._pct(values, hi_cut * 100)
        pools = {name: [] for name in BIN_NAMES}
        for rec in measured:
            value = rec[metric]
            if value <= light_max:
                rec["suggested_bin"] = "light"
            elif value <= medium_max:
                rec["suggested_bin"] = "medium"
            else:
                rec["suggested_bin"] = "heavy"
            pools[rec["suggested_bin"]].append(rec["entity"])
        bins[qtype] = {
            "metric": metric,
            "thresholds": {"light_max": light_max, "medium_max": medium_max},
            "pools": pools,
            "unmeasured": unmeasured,
        }
    return bins


def write_summary(prefix, summary, bins, meta):
    """Write ``<prefix>_entities.csv`` and ``<prefix>_bins.json``."""
    entities_path = f"{prefix}_entities.csv"
    with open(entities_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=ENTITY_FIELDS)
        writer.writeheader()
        for rec in summary:
            writer.writerow({
                key: ("" if rec.get(key) is None else
                      (round(rec[key], 3) if isinstance(rec.get(key), float)
                       else rec.get(key)))
                for key in ENTITY_FIELDS
            })

    bins_path = f"{prefix}_bins.json"
    with open(bins_path, "w") as fh:
        json.dump({**meta, "bins": bins}, fh, indent=2)
    return entities_path, bins_path


def print_report(summary, bins):
    """Print the roll-up an operator reads before editing the corpus pools."""
    paint = console.painter()
    print()
    print(paint("PER-CURIE PROFILE", "bold"))
    by_qtype = {}
    for rec in summary:
        by_qtype.setdefault(rec["qtype"], []).append(rec)

    for qtype, records in sorted(by_qtype.items()):
        measured = [r for r in records if r["result_count_median"] is not None]
        print()
        print(f"  {paint(qtype, 'bold')}  "
              f"{len(measured)}/{len(records)} entities measured")
        if not measured:
            print("    no successful probes -- check --host and the CURIEs")
            continue
        info = bins.get(qtype) or {}
        thresholds = info.get("thresholds") or {}
        print(paint(f"    {'bin':<8}{'n':>5}{'results range':>20}"
                    f"{'results median':>16}{'latency median':>16}", "grey"))
        for name in BIN_NAMES:
            members = set((info.get("pools") or {}).get(name, []))
            group = [r for r in measured if r["entity"] in members]
            if not group:
                continue
            results = [r["result_count_median"] for r in group]
            latencies = [r["latency_s_median"] for r in group
                         if r["latency_s_median"] is not None]
            span = f"{min(results):.0f} .. {max(results):.0f}"
            print(f"    {name:<8}{len(group):>5}{span:>20}"
                  f"{_median(results):>16.0f}"
                  f"{(_median(latencies) or 0):>15.1f}s")
        if thresholds:
            metric = info.get("metric", "result_count_median")
            label = metric.replace("_median", "").replace("result_count",
                                                          "results")
            print(paint(f"    cuts ({label}): light <= "
                        f"{thresholds['light_max']:.0f} < medium <= "
                        f"{thresholds['medium_max']:.0f} < heavy", "grey"))
        unmeasured = (info.get("unmeasured") or [])
        if unmeasured:
            shown = ", ".join(unmeasured[:5])
            more = f" (+{len(unmeasured) - 5} more)" if len(unmeasured) > 5 else ""
            print(paint(f"    unmeasured ({len(unmeasured)}): {shown}{more}",
                        "yellow"))
        top = measured[:3]
        print("    heaviest: " + ", ".join(
            f"{r['entity']} ({r['result_count_median']:.0f})" for r in top))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="helmsdeep-profile",
        description="Profile every MVP1/MVP2 entity once: response time and "
                    "result count per CURIE, plus a suggested light/medium/"
                    "heavy re-binning of each pool. Not a load test.",
    )
    parser.add_argument(
        "--host",
        help="Base URL of the service to profile "
             "(e.g. https://your-ara.example.org). Required unless "
             "--summarize-only or --dry-run.",
    )
    parser.add_argument(
        "--target", default="aras", choices=PROFILABLE_TARGETS,
        help="Which layer's endpoint/protocol to use (default: aras). 'ars' "
             "profiles through the async submit/poll/merge pipeline -- minutes "
             "per query, so pair it with --sample.",
    )
    parser.add_argument(
        "--qtypes", default="mvp1,mvp2",
        help="Comma-separated query families to profile: mvp1, mvp2, or both "
             "(default: mvp1,mvp2).",
    )
    parser.add_argument(
        "--directions", default="both",
        choices=("both", "increased", "decreased"),
        help="MVP2 object_direction_qualifier values to sweep (default: both).",
    )
    parser.add_argument(
        "--repeat", type=int, default=1,
        help="Send each entity's query this many times (default: 1). More reps "
             "give a median that survives one slow response.",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4,
        help="Queries in flight at once (default: 4). Keep it low -- this "
             "measures each query's natural cost, and a concurrent pass "
             "measures queueing instead.",
    )
    parser.add_argument(
        "--sample", type=int, default=None,
        help="Profile a random sample of this many entities per qtype instead "
             "of all of them (use --seed for a repeatable sample).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Stop after this many probes in total (a pilot run).",
    )
    parser.add_argument(
        "--shuffle", action="store_true",
        help="Shuffle the probe order. Spreads any one pool's queries over the "
             "whole run, so a mid-run service slowdown doesn't land entirely on "
             "one pool.",
    )
    parser.add_argument("--seed", type=int, default=None,
                        help="Seed for --sample/--shuffle.")
    parser.add_argument(
        "--out", default="mvp_profile",
        help="Output prefix: <prefix>_probe.csv, <prefix>_entities.csv, "
             "<prefix>_bins.json (default: mvp_profile).",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip entities already measured successfully in <prefix>_probe.csv "
             "and append to it. Failures are re-tried.",
    )
    parser.add_argument(
        "--request-timeout", type=float, default=None,
        help="Per-HTTP-call timeout in seconds (default: the target's "
             "request_timeout_s).",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=None,
        help="ARS only: seconds between status polls (default: the target's "
             "poll_interval_s). It quantizes the measured latency.",
    )
    parser.add_argument(
        "--max-poll", type=float, default=None,
        help="ARS only: per-query poll cap in seconds (default: the target's "
             "max_poll_s). Past it the probe is recorded as a Timeout.",
    )
    parser.add_argument(
        "--bin-cuts", default="0.33,0.67",
        help="Quantile boundaries for light|medium|heavy (default: 0.33,0.67). "
             "Applied within each qtype.",
    )
    parser.add_argument(
        "--bin-metric", default="result_count_median",
        choices=("result_count_median", "kg_edges_median", "kg_nodes_median",
                 "latency_s_median"),
        help="What to bin on (default: result_count_median). Bin on a graph-size "
             "column instead when the service caps results and the top of the "
             "range flattens out.",
    )
    parser.add_argument(
        "--summarize-only", metavar="PROBE_CSV", default=None,
        help="Skip querying: re-read this probe CSV and re-emit the roll-up and "
             "bins. Re-bin with different --bin-cuts/--bin-metric for free.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the probe plan and one sample payload per qtype, then exit.",
    )
    return parser.parse_args(argv)


def _cuts(text):
    try:
        lo, hi = (float(part) for part in text.split(","))
    except ValueError:
        raise SystemExit(f"invalid --bin-cuts {text!r}; use e.g. 0.33,0.67")
    if not 0 < lo < hi < 1:
        raise SystemExit(f"invalid --bin-cuts {text!r}; need 0 < lo < hi < 1")
    return lo, hi


def _sampled(probes, sample, rng):
    """Keep `sample` entities per qtype -- all of an entity's reps/variants."""
    by_qtype = {}
    for probe in probes:
        by_qtype.setdefault(probe.qtype, set()).add(probe.entity)
    keep = set()
    for qtype, entities in by_qtype.items():
        ordered = sorted(entities)
        chosen = (ordered if sample >= len(ordered)
                  else rng.sample(ordered, sample))
        keep.update((qtype, entity) for entity in chosen)
    return [p for p in probes if (p.qtype, p.entity) in keep]


def _summarize(prefix, probe_csv, meta, cuts, metric):
    rows = read_rows(probe_csv)
    summary = entity_summary(rows)
    bins = assign_bins(summary, metric=metric, cuts=cuts)
    entities_path, bins_path = write_summary(prefix, summary, bins, meta)
    print_report(summary, bins)
    paint = console.painter()
    print()
    print(paint(f"wrote {probe_csv}, {entities_path}, {bins_path}", "grey"))
    print(paint("re-bin without re-querying:  helmsdeep-profile "
                f"--summarize-only {probe_csv} --out {prefix} "
                "--bin-cuts 0.25,0.75", "grey"))
    return 0


def main(argv=None):
    args = _parse_args(argv)
    paint = console.painter()
    cuts = _cuts(args.bin_cuts)
    prefix = args.out
    probe_csv = f"{prefix}_probe.csv"

    if args.summarize_only:
        if not os.path.exists(args.summarize_only):
            print(f"no such probe CSV: {args.summarize_only}", file=sys.stderr)
            return 1
        return _summarize(prefix, args.summarize_only, {
            "generated_utc": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "source": args.summarize_only,
            "bin_cuts": list(cuts),
            "bin_metric": args.bin_metric,
        }, cuts, args.bin_metric)

    qtypes = {part.strip() for part in args.qtypes.split(",") if part.strip()}
    unknown = qtypes - {"mvp1", "mvp2"}
    if unknown:
        print(f"unknown --qtypes value(s): {', '.join(sorted(unknown))}; "
              f"choose from mvp1, mvp2", file=sys.stderr)
        return 1
    directions = (corpus._DIRECTIONS if args.directions == "both"
                  else [args.directions])
    if args.repeat < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 1
    if args.concurrency < 1:
        print("--concurrency must be at least 1", file=sys.stderr)
        return 1
    if not args.host and not args.dry_run:
        print("--host is required (unless --summarize-only or --dry-run)",
              file=sys.stderr)
        return 1

    rng = random.Random(args.seed)
    probes = build_probes(qtypes, directions, repeat=args.repeat)
    total_planned = len(probes)
    if args.sample:
        probes = _sampled(probes, args.sample, rng)
    if args.shuffle:
        rng.shuffle(probes)

    skipped = 0
    if args.resume and os.path.exists(probe_csv):
        done = measured_keys(read_rows(probe_csv))
        before = len(probes)
        probes = [p for p in probes if p.key not in done]
        skipped = before - len(probes)
    if args.limit:
        probes = probes[:args.limit]

    cfg = config.TARGETS[args.target]
    request_timeout = (args.request_timeout
                       if args.request_timeout is not None
                       else cfg.get("request_timeout_s", 210))
    poll_interval = (args.poll_interval if args.poll_interval is not None
                     else cfg.get("poll_interval_s", 10))
    max_poll = (args.max_poll if args.max_poll is not None
                else cfg.get("max_poll_s", 900))

    print()
    print(paint(f"HelmsDeep profile  ·  {cfg['label']} ({args.target})", "bold"))
    if args.host:
        print(paint(f"                   ·  {args.host.rstrip('/')}"
                    f"{cfg['endpoint']}", "grey"))
    plan = f"{len(probes)} probes"
    if skipped:
        plan += f" ({skipped} already measured, skipped)"
    if len(probes) != total_planned and not skipped:
        plan += f" of {total_planned} planned"
    print(paint(f"                   ·  {plan}, concurrency "
                f"{args.concurrency}", "grey"))
    print(paint(f"                   ·  writing {probe_csv}", "grey"))

    if args.dry_run:
        by_qtype = {}
        for probe in probes:
            by_qtype.setdefault(probe.qtype, []).append(probe)
        for qtype, group in sorted(by_qtype.items()):
            entities = {p.entity for p in group}
            print()
            print(f"  {paint(qtype, 'bold')}: {len(group)} probes over "
                  f"{len(entities)} entities")
            print("  sample payload: "
                  + json.dumps(group[0].payload, separators=(",", ":")))
        print()
        return 0

    if not probes:
        print(paint("\nnothing to profile -- every probe is already measured. "
                    "Summarizing what's on disk.", "yellow"))
        return _summarize(prefix, probe_csv, {
            "generated_utc": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "host": args.host, "target": args.target,
            "bin_cuts": list(cuts), "bin_metric": args.bin_metric,
        }, cuts, args.bin_metric)
    print()

    stop = threading.Event()
    prober = Prober(args.host, cfg, request_timeout, poll_interval, max_poll,
                    stop=stop)
    writer = RowWriter(probe_csv)
    counter = {"done": 0, "ok": 0}
    counter_lock = threading.Lock()
    started = time.time()
    total = len(probes)

    def _work(probe):
        if stop.is_set():
            return
        try:
            measurement = prober.run(probe)
        except Exception as exc:
            # One unexpected failure must not take the whole overnight pass
            # down with it -- record it and carry on; --resume retries it.
            measurement = Prober._row(time.time(), ok=False,
                                      status="ProbeError", error=str(exc)[:200])
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "qtype": probe.qtype, "mvp": probe.mvp, "entity": probe.entity,
            "pool": probe.pool, "variant": probe.variant, "rep": probe.rep,
            **measurement,
        }
        writer.write(row)
        with counter_lock:
            counter["done"] += 1
            counter["ok"] += row["ok"]
            done = counter["done"]
        elapsed = time.time() - started
        eta = (elapsed / done) * (total - done) if done else 0
        results = row["result_count"]
        detail = (f"{results:>6} results" if results != ""
                  else f"{row['status']:>13}")
        # One write, not print()'s text-then-newline pair: several workers are
        # logging into the same terminal and would otherwise interleave.
        sys.stdout.write(
            f"  [{done:>4}/{total}] {probe.qtype:<26} {probe.entity:<18} "
            f"{detail}  {row['latency_s']:>7.1f}s   "
            f"ETA {console.fmt_duration(eta)}\n")

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        try:
            list(pool.map(_work, probes))
        except KeyboardInterrupt:
            stop.set()
            print(paint("\ninterrupted -- finishing in-flight probes, then "
                        "summarizing what was measured.", "yellow"))
    writer.close()

    print()
    print(paint(f"{counter['ok']}/{counter['done']} probes succeeded in "
                f"{console.fmt_duration(time.time() - started)}", "bold"))
    return _summarize(prefix, probe_csv, {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": args.host,
        "target": args.target,
        "protocol": cfg.get("protocol", "sync"),
        "bin_cuts": list(cuts),
        "bin_metric": args.bin_metric,
        "repeat": args.repeat,
        "concurrency": args.concurrency,
    }, cuts, args.bin_metric)


if __name__ == "__main__":
    sys.exit(main())
