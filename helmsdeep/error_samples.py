"""
Error samples: what a failure actually looked like, saved to disk.

The per-stage reports say *how many* requests failed and the per-query debug
log (ARS) says *which* -- but a status code on its own says nothing about
*why*. This module keeps a bounded number of full examples of every distinct
error kind, one JSON file each, so a run can be picked apart afterwards without
re-running it: the request that was sent, the response that came back (status,
headers, body), the client-side exception if there was no response at all, and
which stage / query type it happened under.

Layout, under ``<prefix>_errors/``::

    index.json                       every saved sample + a tally of every kind
    <kind>/<NN>_stage<S>_<qtype>.json   one sample

Saving is **opt-in** (``helmsdeep --save-errors [N]``): a disabled sampler only
counts occurrences per kind and stage for the summary -- it writes nothing,
deletes nothing, and never decodes a response body -- so a run on a box with
little disk or RAM pays nothing for it.

A *kind* is a short slug naming the failure mode -- ``http_502``, ``timeout``,
``connection_error`` for the sync layers; ``ars_submit_http_500``,
``ars_error``, ``ars_timeout``, ``ars_done_zero_results`` and the intermediate
``ars_poll_http_502`` etc. for the ARS. Samples are capped **per kind, per
stage** (``config.ERROR_SAMPLES_PER_KIND``) rather than per kind overall, because
the same status code at 5 users and at 60 users usually has a different body
behind it. Every occurrence is still *counted*, so the tally in ``index.json``
and ``summary.json`` is complete even where the samples are capped.

Response bodies are truncated to ``config.ERROR_SAMPLE_BODY_BYTES``; a merged
TRAPI message can run to megabytes and the first few KB are what say what went
wrong. Request headers are deliberately not recorded (they would be the place a
credential lives); response headers are.
"""

import json
import os
import re
import shutil
import time

_SLUG = re.compile(r"[^A-Za-z0-9_.-]+")


def slug(text):
    """Filesystem-safe version of a kind / qtype name."""
    return _SLUG.sub("_", str(text)).strip("_") or "unknown"


def http_kind(resp):
    """Classify one HTTP call's outcome into a kind slug.

    ``http_<code>`` when a response came back; otherwise (Locust reports
    status 0 and carries the exception) ``timeout`` / ``connection_error`` /
    ``exception_<Class>`` from the exception type.
    """
    code = getattr(resp, "status_code", 0) or 0
    if code:
        return f"http_{code}"
    exc = getattr(resp, "error", None)
    name = type(exc).__name__ if exc is not None else ""
    if "Timeout" in name:
        return "timeout"
    if "Connection" in name:
        return "connection_error"
    return f"exception_{name}" if name else "no_response"


def failure_message(resp, default):
    """The error text for a sample: ``default`` when a response came back, else
    the client-side exception (``no HTTP response (ReadTimeout: ...)``), which
    is the only cause a status-0 failure has."""
    if getattr(resp, "status_code", 0):
        return default
    exc = getattr(resp, "error", None)
    if exc is None:
        return "no HTTP response"
    return f"no HTTP response ({type(exc).__name__}: {exc})"


def describe_exception(exc):
    if exc is None:
        return None
    return {"type": type(exc).__name__, "message": str(exc)}


def describe_response(resp, body_limit):
    """A JSON-able picture of a (requests / Locust) response, body truncated.

    Tolerates the status-0 "no response" object Locust hands back on a
    connection error, and never raises: a sample that can't be described in
    full is still worth the parts that can.
    """
    if resp is None:
        return None
    out = {
        "status_code": getattr(resp, "status_code", None),
        "reason": getattr(resp, "reason", None),
        "headers": {},
        "bytes": None,
        "body": None,
        "body_truncated": False,
    }
    try:
        out["headers"] = dict(resp.headers or {})
    except Exception:
        pass
    try:
        elapsed = getattr(resp, "elapsed", None)
        if elapsed is not None:
            out["elapsed_ms"] = round(elapsed.total_seconds() * 1000.0, 1)
    except Exception:
        pass
    try:
        content = resp.content or b""
        out["bytes"] = len(content)
        head = content[:body_limit]
        out["body_truncated"] = len(content) > body_limit
        text = head.decode(resp.encoding or "utf-8", errors="replace")
        # Keep JSON as JSON when it's small enough to have survived whole --
        # far easier to read (and to jq) than an escaped string.
        parsed = None
        if not out["body_truncated"]:
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
        out["body"] = parsed if parsed is not None else text
    except Exception as e:   # pragma: no cover - defensive
        out["body"] = f"<unreadable: {type(e).__name__}: {e}>"
    return out


def describe_request(resp, *, method=None, url=None, payload=None):
    """The request side of a sample. Prefers the explicit values (what the user
    path asked for); falls back to the PreparedRequest on the response."""
    req = getattr(resp, "request", None)
    out = {
        "method": method or getattr(req, "method", None),
        "url": url or getattr(req, "url", None),
    }
    if payload is not None:
        out["json"] = payload
    elif req is not None and getattr(req, "body", None):
        body = req.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        try:
            out["json"] = json.loads(body)
        except Exception:
            out["body"] = str(body)[:4096]
    return out


class ErrorSampler:
    """Bounded on-disk capture of error examples, keyed by kind and stage.

    ``capture()`` is cheap on the hot path once a (kind, stage) bucket is full:
    it only bumps counters. Files are written the moment a sample is taken (not
    at shutdown), so an aborted run still leaves its samples behind.
    """

    def __init__(self, root, *, per_kind_per_stage=3, body_limit=64 * 1024,
                 enabled=True):
        self.root = root
        self.per_kind_per_stage = int(per_kind_per_stage)
        self.body_limit = int(body_limit)
        self.enabled = bool(enabled) and self.per_kind_per_stage > 0
        self._occurrences = {}   # kind -> stage -> count (every occurrence)
        self._saved = {}         # kind -> stage -> count (files written)
        self.samples = []        # index rows, in capture order

    # -- lifecycle -----------------------------------------------------------
    def reset(self):
        """Start clean: drop the directory left by a previous run with this
        prefix (the CSVs are overwritten the same way) and forget the tallies.
        A disabled sampler never touches the filesystem, not even to delete."""
        if self.enabled and os.path.isdir(self.root):
            shutil.rmtree(self.root, ignore_errors=True)
        self._occurrences.clear()
        self._saved.clear()
        self.samples.clear()

    # -- capture ---------------------------------------------------------------
    def capture(self, kind, *, stage, users, qtype, error, response=None,
                request=None, exception=None, latency_ms=None,
                intermediate=False, extra=None):
        """Record one error occurrence; save it to disk if its bucket has room.

        Returns the path of the saved file (relative to the working directory)
        or ``None`` when the sample was only counted.
        """
        kind = slug(kind)
        per_stage = self._occurrences.setdefault(kind, {})
        per_stage[stage] = per_stage.get(stage, 0) + 1
        if not self.enabled:
            return None
        saved = self._saved.setdefault(kind, {})
        if saved.get(stage, 0) >= self.per_kind_per_stage:
            return None
        saved[stage] = saved.get(stage, 0) + 1

        n = sum(saved.values())   # running number within this kind
        name = f"{n:02d}_stage{stage}_{slug(qtype)}.json"
        path = os.path.join(self.root, kind, name)
        record = {
            "kind": kind,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stage": stage,
            "users": users,
            "qtype": qtype,
            # An intermediate error was retried past and did not decide the
            # query's outcome (ARS poll/merge trouble); a terminal one did.
            "intermediate": bool(intermediate),
            "error": error,
            "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
            "request": request,
            "response": describe_response(response, self.body_limit),
            "exception": describe_exception(
                exception if exception is not None
                else getattr(response, "error", None)),
        }
        if extra:
            record.update(extra)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(record, f, indent=2, default=str)
        except OSError:
            # Sampling must never fail a measurement. Give the slot back so a
            # later occurrence gets another try.
            saved[stage] -= 1
            return None
        self.samples.append({
            "path": path,
            "kind": kind,
            "stage": stage,
            "users": users,
            "qtype": qtype,
            "intermediate": bool(intermediate),
            "error": error,
            "captured_at": record["captured_at"],
        })
        return path

    # -- reporting -------------------------------------------------------------
    def kinds(self):
        """One roll-up row per kind, most frequent first."""
        rows = []
        for kind, per_stage in self._occurrences.items():
            saved = self._saved.get(kind, {})
            first = next((s for s in self.samples if s["kind"] == kind), None)
            rows.append({
                "kind": kind,
                "occurrences": sum(per_stage.values()),
                "saved": sum(saved.values()),
                "stages": sorted(per_stage),
                "by_stage": {str(k): v for k, v in sorted(per_stage.items())},
                "intermediate": bool(first and first["intermediate"]),
                "example": first["path"] if first else None,
            })
        rows.sort(key=lambda r: (-r["occurrences"], r["kind"]))
        return rows

    def summary(self):
        return {
            "dir": self.root,
            "enabled": self.enabled,
            "per_kind_per_stage": self.per_kind_per_stage,
            "body_bytes": self.body_limit,
            "total_occurrences": sum(
                sum(s.values()) for s in self._occurrences.values()),
            "saved": len(self.samples),
            "kinds": self.kinds(),
        }

    def write_index(self):
        """Write ``<root>/index.json`` (tally + every saved sample). Returns the
        path, or ``None`` when nothing was captured (no directory is created)."""
        if not self.enabled or not self._occurrences:
            return None
        os.makedirs(self.root, exist_ok=True)
        path = os.path.join(self.root, "index.json")
        with open(path, "w") as f:
            json.dump({**self.summary(), "samples": self.samples}, f, indent=2)
        return path
