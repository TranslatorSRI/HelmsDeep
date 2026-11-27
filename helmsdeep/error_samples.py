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

A status code is not a cause: a service that answers every failure with a 500
puts ten different problems in one ``http_500`` kind. So each kind is split
again by the **message** the service gave -- its own verdict (``description``,
``detail``, ERROR-level log line, or a plain-text error page), with the parts
that vary per query (CURIEs, ids, numbers) blanked out so the same error groups
together whatever it was about. Messages are counted on every run, saving or
not, and the sample cap applies per message, so a rare error keeps its own
examples instead of being crowded out by a common one. Only a non-2xx
response's body is read for this; a 2xx body (an ARS trace poll, a merged
message) is still never decoded unless a file is being written.

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

# Placeholders in the per-kind message tally: an error that said nothing we
# could read, and the overflow bucket once a kind has `messages_per_kind`
# distinct messages.
NO_MESSAGE = "(no message)"
OTHER_MESSAGES = "(other messages)"

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


# What a JSON error body calls its human-readable verdict, in order of
# preference: TRAPI ``description``, FastAPI/Django ``detail``, then the looser
# conventions. ``status`` is TRAPI's short code (``Error``,
# ``QueryNotTraversable``), used as a prefix or when nothing else is there.
_VERDICT_KEYS = ("description", "detail", "message", "error", "title")
_ERROR_LEVELS = ("ERROR", "CRITICAL")


def response_body(resp, max_bytes=None):
    """A response's body decoded once: ``(parsed_json_or_None, text)``.

    Memoised on the response object, because the engine asks twice of the same
    failed response (is it a refusal? what did it say?) and a failed TRAPI
    body can carry every log line the query produced. A body over
    ``max_bytes`` is not decoded at all -- ``(None, "")`` -- so one enormous
    error page cannot stall the event loop. Never raises.
    """
    cached = getattr(resp, "_helmsdeep_body", None)
    if cached is not None:
        return cached
    parsed, text = None, ""
    try:
        content = resp.content or b""
        if max_bytes is None or len(content) <= max_bytes:
            text = content.decode(getattr(resp, "encoding", None) or "utf-8",
                                  errors="replace")
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
    except Exception:
        parsed, text = None, ""
    try:
        resp._helmsdeep_body = (parsed, text)
    except Exception:
        pass
    return parsed, text


def _as_text(value):
    """A verdict field as one string. FastAPI's ``detail`` is a list of
    ``{loc, msg, type}`` on a validation error; anything else that isn't a
    string is not a message."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("msg"), str):
                loc = ".".join(str(x) for x in item.get("loc") or [])
                parts.append(f"{loc}: {item['msg']}" if loc else item["msg"])
            elif isinstance(item, str):
                parts.append(item)
        return "; ".join(parts)
    return ""


def verdict_texts(resp, max_bytes=None):
    """Every piece of text in which a failed response states its own verdict.

    For a JSON object: its top-level ``status`` / ``description`` / ``detail``
    / ``message`` strings and its ERROR/CRITICAL log messages -- never WARNING
    or INFO logs, which a failed query's body is full of and which say nothing
    about why it failed. For a body that isn't JSON (a proxy's error page),
    the first 4096 characters of its text.
    """
    body, text = response_body(resp, max_bytes)
    texts = []
    if isinstance(body, dict):
        for key in ("status", "description", "detail", "message"):
            if isinstance(body.get(key), str):
                texts.append(body[key])
        for log in body.get("logs") or []:
            if (isinstance(log, dict)
                    and str(log.get("level", "")).upper() in _ERROR_LEVELS
                    and isinstance(log.get("message"), str)):
                texts.append(log["message"])
    elif body is None and text:
        texts.append(text[:4096])
    return texts


_TAG = re.compile(r"<[^>]{1,200}>")
_NORMALISERS = (
    (re.compile(r"https?://[^\s\"'<>)\]]+"), "<url>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    # A CURIE: PREFIX:local with no space after the colon, and a digit
    # somewhere in the local part or an upper-case prefix (MONDO:0005148,
    # NCBIGene:1017, UNII:31YO63LBSN) -- so "biolink:treats", which names the
    # KIND of thing that failed, is kept.
    (re.compile(r"\b[A-Za-z][A-Za-z0-9_.]*:(?=[A-Za-z0-9_.\-]*\d)"
                r"[A-Za-z0-9_.\-]+\b"), "<curie>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<addr>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{12,}\b"), "<id>"),
    (re.compile(r"(?<![A-Za-z_<])\d+(?:\.\d+)?"), "<n>"),
)
_SPACE = re.compile(r"\s+")


def normalise_message(text, limit=200):
    """Reduce an error message to the part that names the PROBLEM, so two
    occurrences of one error group together whatever query they were about:
    URLs, UUIDs, CURIEs, long hex ids and numbers become placeholders,
    whitespace is collapsed, and the result is cut to ``limit`` characters."""
    text = str(text or "")
    for pattern, placeholder in _NORMALISERS:
        text = pattern.sub(placeholder, text)
    text = _SPACE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def response_message(resp, *, limit=200, max_bytes=None):
    """What a failed (non-2xx) response said was wrong, normalised -- or
    ``""`` when it said nothing readable.

    ``Status: description`` for a TRAPI error; else the first of ``detail`` /
    ``message`` / ``error`` / ``title``; else the LAST ERROR-level log line
    (the one the query died on); else the bare ``status``. A body that isn't
    JSON is an error page: its text with any markup stripped.
    """
    body, text = response_body(resp, max_bytes)
    try:
        if max_bytes is not None and len(resp.content or b"") > max_bytes:
            return f"(body over {max_bytes} bytes, not read)"
    except Exception:
        pass
    raw = ""
    if isinstance(body, dict):
        status = body.get("status") if isinstance(body.get("status"), str) else ""
        said = next((t for t in (_as_text(body.get(k)) for k in _VERDICT_KEYS)
                     if t.strip()), "")
        if not said:
            logs = [log["message"] for log in body.get("logs") or []
                    if isinstance(log, dict)
                    and str(log.get("level", "")).upper() in _ERROR_LEVELS
                    and isinstance(log.get("message"), str)
                    and log["message"].strip()]
            said = logs[-1] if logs else ""
        if status and said and status.lower() not in said.lower():
            raw = f"{status}: {said}"
        else:
            raw = said or status
    elif isinstance(body, str):
        raw = body
    elif body is None:
        raw = _TAG.sub(" ", text[:4096])
    return normalise_message(raw, limit)


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
                 enabled=True, messages_per_kind=50, message_chars=200,
                 message_parse_bytes=4 * 1024 * 1024):
        self.root = root
        self.per_kind_per_stage = int(per_kind_per_stage)
        self.body_limit = int(body_limit)
        self.enabled = bool(enabled) and self.per_kind_per_stage > 0
        self.messages_per_kind = int(messages_per_kind)
        self.message_chars = int(message_chars)
        self.message_parse_bytes = int(message_parse_bytes)
        self._occurrences = {}   # kind -> stage -> count (every occurrence)
        # kind -> message -> stage -> count (every occurrence). The message is
        # what the service said, normalised; see _message().
        self._messages = {}
        self._saved = {}         # kind -> (message, stage) -> files written
        self.samples = []        # index rows, in capture order

    # -- lifecycle -----------------------------------------------------------
    def reset(self):
        """Start clean: drop the directory left by a previous run with this
        prefix (the CSVs are overwritten the same way) and forget the tallies.
        A disabled sampler never touches the filesystem, not even to delete."""
        if self.enabled and os.path.isdir(self.root):
            shutil.rmtree(self.root, ignore_errors=True)
        self._occurrences.clear()
        self._messages.clear()
        self._saved.clear()
        self.samples.clear()

    # -- capture ---------------------------------------------------------------
    def _message(self, kind, error, response, exception):
        """The message this occurrence is grouped under within its kind.

        A non-2xx response speaks for itself. With no response at all the
        client-side exception is the cause. Anything else (a 2xx that was
        scored as an error: an ARS ``Error`` status, a shortfall) has only the
        engine's own description -- and its 2xx body is NOT decoded here.
        A non-2xx with nothing readable in it is ``(no message)``: the engine's
        "HTTP 500" would only repeat the kind.
        """
        text = ""
        try:
            code = getattr(response, "status_code", 0) or 0
            exc = (exception if exception is not None
                   else getattr(response, "error", None))
            if code and not 200 <= code < 300:
                text = response_message(response, limit=self.message_chars,
                                        max_bytes=self.message_parse_bytes)
            elif not code and exc is not None:
                text = normalise_message(f"{type(exc).__name__}: {exc}",
                                         self.message_chars)
            else:
                text = normalise_message(error, self.message_chars)
        except Exception:   # pragma: no cover - defensive
            text = ""
        text = text or NO_MESSAGE
        known = self._messages.setdefault(kind, {})
        if text not in known and len(known) >= self.messages_per_kind:
            # Bound the tally: a service that puts something unique in every
            # error would otherwise grow one entry per failure.
            text = OTHER_MESSAGES
        return text

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
        message = self._message(kind, error, response, exception)
        by_stage = self._messages[kind].setdefault(message, {})
        by_stage[stage] = by_stage.get(stage, 0) + 1
        if not self.enabled:
            return None
        # Capped per MESSAGE (per stage), not per kind: the common error can't
        # use up the slots a rare one needs.
        saved = self._saved.setdefault(kind, {})
        bucket = (message, stage)
        if saved.get(bucket, 0) >= self.per_kind_per_stage:
            return None
        saved[bucket] = saved.get(bucket, 0) + 1

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
            # What the service said, normalised: the key this sample is grouped
            # under in the summary's per-kind `messages`.
            "message": message,
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
            saved[bucket] -= 1
            return None
        self.samples.append({
            "path": path,
            "kind": kind,
            "stage": stage,
            "users": users,
            "qtype": qtype,
            "intermediate": bool(intermediate),
            "error": error,
            "message": message,
            "captured_at": record["captured_at"],
        })
        return path

    # -- reporting -------------------------------------------------------------
    def messages(self, kind):
        """One roll-up row per distinct message within a kind, most frequent
        first: what the service said, how often, at which stages, and a saved
        example of it (when saving is on)."""
        saved = self._saved.get(kind, {})
        rows = []
        for message, per_stage in self._messages.get(kind, {}).items():
            first = next((s for s in self.samples
                          if s["kind"] == kind and s["message"] == message), None)
            rows.append({
                "message": message,
                "occurrences": sum(per_stage.values()),
                "saved": sum(n for (m, _), n in saved.items() if m == message),
                "stages": sorted(per_stage),
                "by_stage": {str(k): v for k, v in sorted(per_stage.items())},
                "example": first["path"] if first else None,
            })
        rows.sort(key=lambda r: (-r["occurrences"], r["message"]))
        return rows

    def kinds(self):
        """One roll-up row per kind, most frequent first, each with its
        ``messages`` breakdown."""
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
                "messages": self.messages(kind),
            })
        rows.sort(key=lambda r: (-r["occurrences"], r["kind"]))
        return rows

    def summary(self):
        return {
            "dir": self.root,
            "enabled": self.enabled,
            # The cap on saved files: per kind, per message, per stage.
            "per_kind_per_stage": self.per_kind_per_stage,
            "messages_per_kind": self.messages_per_kind,
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
