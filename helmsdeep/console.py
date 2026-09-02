"""
Live terminal output for a HelmsDeep run.

A run is long (minutes to hours) and, before this module, said nothing while it
worked except Locust's request table every two seconds -- which scrolls the one
thing an operator actually wants ("which stage are we on, and is it holding?")
off the screen within seconds.

This module replaces that with two channels:

* a **sticky footer** pinned to the bottom of the terminal, redrawn a few times
  a second: run progress, the current stage and its progress, and the live
  numbers for that stage (throughput, errors, percentiles vs the SLO, and for
  ARS the poll/status picture).
* a **scrolling log** above it -- one line when a stage starts, one verdict line
  when it ends, plus anything else the run prints. Warnings and Locust's own
  logging land here too, because ``start()`` routes stdout/stderr through a
  proxy that lifts the footer out of the way before any other write.

Nothing here measures anything: every number is read from the same
``StageCollector`` and ``_stage_stats()`` the reports are written from, so the
footer cannot drift from ``stages.csv``.

When stdout is not a TTY (CI, a pipe, a log file) the footer is impossible, so
the same content is emitted as one periodic status line -- see ``PLAIN_EVERY_S``.
Set ``HELMSDEEP_LIVE=0`` to force that mode, or ``NO_COLOR`` to keep the layout
without ANSI colour.
"""

import logging
import os
import re
import shutil
import sys
import time

try:                       # installed package
    from . import config
except ImportError:        # loaded flat by the locustfile (locust -f puts the
    import config          # package dir on sys.path, so these are top-level)

# How often the footer is redrawn (seconds). Fast enough to feel live, slow
# enough to be invisible in the run's CPU profile.
REFRESH_S = 0.5
# Plain (non-TTY) mode: how often to emit the one-line status instead.
PLAIN_EVERY_S = 30

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_BAR_FULL = "━"    # heavy horizontal
_BAR_EMPTY = "─"   # light horizontal
_RULE = "─"
_OK = "✓"
_BAD = "✗"


# ----------------------------------------------------------------------------
# Capability detection + styling.
# ----------------------------------------------------------------------------
def _env_flag(name):
    """Tri-state env flag: True (force on), False (force off), None (auto)."""
    val = os.environ.get(name)
    if val is None or val == "":
        return None
    return val.strip().lower() not in ("0", "off", "false", "no")


def live_enabled(stream=None):
    """Whether a redrawn footer is possible (and wanted) on this stream."""
    forced = _env_flag("HELMSDEEP_LIVE")
    if forced is not None:
        return forced
    stream = stream or sys.stdout
    try:
        if not stream.isatty():
            return False
    except Exception:
        return False
    return os.environ.get("TERM", "") not in ("", "dumb")


def color_enabled(stream=None):
    """Colour is a separate question from cursor control: NO_COLOR kills only it."""
    if os.environ.get("NO_COLOR"):
        return False
    if _env_flag("HELMSDEEP_LIVE") is False:
        return False
    stream = stream or sys.stdout
    try:
        return stream.isatty() and os.environ.get("TERM", "") not in ("", "dumb")
    except Exception:
        return False


_CODES = {
    "bold": "1", "dim": "2",
    "red": "31", "green": "32", "yellow": "33", "blue": "34",
    "magenta": "35", "cyan": "36", "grey": "90",
}


class _Painter:
    """``paint("text", "bold", "cyan")`` -- a no-op when colour is off."""

    def __init__(self, enabled):
        self.enabled = enabled

    def __call__(self, text, *styles):
        if not self.enabled or not styles:
            return text
        codes = ";".join(_CODES[s] for s in styles if s in _CODES)
        return f"\x1b[{codes}m{text}\x1b[0m" if codes else text


def terminal_width(default=100, cap=110):
    """Usable width for a printed line: the terminal's, clamped to something
    readable (a 400-column window shouldn't stretch a status line across it)."""
    try:
        return max(60, min(shutil.get_terminal_size((default, 24)).columns, cap))
    except Exception:
        return default


def painter(stream=None):
    """A ``paint`` callable for `stream`, colour-enabled only where appropriate."""
    return _Painter(color_enabled(stream))


def vislen(text):
    """Printable width, ignoring ANSI escapes."""
    return len(_ANSI_RE.sub("", text))


def truncate(text, width):
    """Clip to `width` visible characters, keeping escapes balanced."""
    if vislen(text) <= width:
        return text
    # Walk the string, copying escapes free of charge and counting the rest.
    out, shown, i = [], 0, 0
    while i < len(text) and shown < width:
        m = _ANSI_RE.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        out.append(text[i])
        shown += 1
        i += 1
    return "".join(out) + ("\x1b[0m" if "\x1b[" in text else "")


# ----------------------------------------------------------------------------
# Formatting helpers.
# ----------------------------------------------------------------------------
def fmt_duration(seconds):
    """`45s`, `4m10s`, `1h07m` -- compact enough for a status line."""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def fmt_ms(ms):
    """Latency in the unit an operator reads at a glance: ms below a second."""
    if ms is None:
        return "-"
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000.0:.1f}s"


def bar(fraction, width, paint=None, style=None):
    """A progress bar; the filled part can carry a colour."""
    fraction = min(max(fraction, 0.0), 1.0)
    filled = int(round(fraction * width))
    head = _BAR_FULL * filled
    if paint and style:
        head = paint(head, *style)
    return head + _BAR_EMPTY * (width - filled)


_SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values, threshold=None, paint=None):
    """The shape of a metric across the ramp, one character per stage.

    Heights are measured against `threshold` -- the p99 SLO, the error cap -- so
    a bar's height reads as "how close this stage came to the bar", and a run
    that stayed comfortably under one stays visibly low instead of being
    normalised up to full height. (A series that tops out above the threshold
    rescales to its own maximum, so the overshoot is still legible.) Stages that
    breach the threshold are painted red and the rest green, which puts the
    crossing point in one glance. With no threshold, heights are relative to the
    largest value.
    """
    values = [v or 0.0 for v in values]
    if not values:
        return ""
    top = max(values + ([threshold] if threshold is not None else [])) or 1.0
    out = []
    for v in values:
        ch = _SPARK[min(int(v / top * (len(_SPARK) - 1) + 0.5), len(_SPARK) - 1)]
        if paint and threshold is not None:
            ch = paint(ch, "red" if v > threshold else "green")
        out.append(ch)
    return "".join(out)


def _pct(values, p):
    """Percentile over an unsorted list (same maths as the report writer)."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# ----------------------------------------------------------------------------
# Sticky footer: a block of lines pinned below the scrolling log.
#
# The trick is only that every OTHER write has to erase the block first, or it
# gets scrolled into the transcript as garbage. `start()` wraps stdout/stderr
# (and re-points the logging handlers that captured them) so that happens
# automatically, whoever does the printing.
# ----------------------------------------------------------------------------
class _StreamProxy:
    """Erases the footer before letting a write through to the real stream."""

    def __init__(self, raw, footer):
        self._raw = raw
        self._footer = footer

    def write(self, text):
        if text:
            self._footer.clear()
        return self._raw.write(text)

    def flush(self):
        return self._raw.flush()

    def isatty(self):
        return self._raw.isatty()

    def __getattr__(self, name):
        return getattr(self._raw, name)


class StickyFooter:
    """Owns the bottom-of-screen block and the streams that must respect it."""

    def __init__(self, stream=None):
        self.raw = stream or sys.stdout
        self._lines = 0
        self._installed = []       # (holder, attr, original) to restore
        self._patched_handlers = []

    # -- drawing -------------------------------------------------------------
    def clear(self):
        if self._lines:
            self.raw.write(f"\x1b[{self._lines}A\x1b[J")
            self.raw.flush()
            self._lines = 0

    def draw(self, lines):
        self.clear()
        if not lines:
            return
        self.raw.write("\n".join(lines) + "\n")
        self.raw.flush()
        self._lines = len(lines)

    # -- stream capture ------------------------------------------------------
    def install(self):
        """Route stdout/stderr (and the logging handlers holding them) through
        the proxy, so any print/log lifts the footer instead of colliding."""
        originals = {"stdout": sys.stdout, "stderr": sys.stderr}
        for attr, original in originals.items():
            proxy = _StreamProxy(original, self)
            setattr(sys, attr, proxy)
            self._installed.append((sys, attr, original))
            # Logging's StreamHandlers captured the stream object at
            # construction time, so re-pointing sys.stdout alone misses them.
            for handler in _stream_handlers():
                if handler.stream is original:
                    handler.stream = proxy
                    self._patched_handlers.append((handler, original))

    def restore(self):
        self.clear()
        for handler, original in self._patched_handlers:
            handler.stream = original
        self._patched_handlers = []
        for holder, attr, original in self._installed:
            setattr(holder, attr, original)
        self._installed = []


def _stream_handlers():
    """Every StreamHandler currently attached anywhere in the logging tree."""
    loggers = [logging.getLogger()]
    manager_dict = getattr(logging.Logger.manager, "loggerDict", {})
    for obj in list(manager_dict.values()):
        if isinstance(obj, logging.Logger):
            loggers.append(obj)
    handlers = []
    for logger in loggers:
        for handler in list(getattr(logger, "handlers", [])):
            if isinstance(handler, logging.StreamHandler) and \
                    hasattr(handler, "stream"):
                handlers.append(handler)
    return handlers


# ----------------------------------------------------------------------------
# The dashboard itself.
# ----------------------------------------------------------------------------
class Dashboard:
    """Renders where a run is and how the current stage is doing.

    Reads live state from the run's ``StageCollector`` and its ``stage_stats``
    function -- it never keeps its own tally, so the footer and the CSVs cannot
    disagree.
    """

    def __init__(self, *, label, target, host, endpoint, stages, cooldown_s,
                 p99_slo_ms, max_error_rate, protocol, time_scale,
                 collector, stage_stats, stream=None, refresh_s=REFRESH_S,
                 plain_every_s=PLAIN_EVERY_S):
        self.label = label
        self.target = target
        self.host = host
        self.endpoint = endpoint
        self.stages = stages
        self.cooldown_s = cooldown_s
        self.p99_slo_ms = p99_slo_ms
        self.max_error_rate = max_error_rate
        self.protocol = protocol
        self.time_scale = time_scale
        self.collector = collector
        self.stage_stats = stage_stats
        self.refresh_s = refresh_s
        self.plain_every_s = plain_every_s

        self.stream = stream or sys.stdout
        self.live = live_enabled(self.stream)
        self.paint = _Painter(color_enabled(self.stream))
        self.footer = StickyFooter(self.stream)

        # The ramp on a wall clock -- the same layout the load shape drives from.
        self.windows, self.total_s = config.build_timeline(stages, cooldown_s)

        self.started_at = None
        self._greenlet = None
        self._stopped = False
        self._announced_stage = -1
        self._pending_recap = None
        self._recapped = set()
        self._draining = False
        self._last_plain = 0.0

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        import gevent   # only needed once the run is actually going
        self.started_at = time.time()
        self._last_plain = self.started_at
        if self.live:
            self.footer.install()
        self._greenlet = gevent.spawn(self._loop)
        return self

    def stop(self):
        self._stopped = True
        if self._greenlet is not None:
            self._greenlet.kill(block=False)
            self._greenlet = None
        self.footer.restore()

    def _loop(self):
        import gevent
        while not self._stopped:
            try:
                self.tick()
            except Exception as exc:      # a cosmetic layer must never kill a run
                self.log(self.paint(f"[live display error: {exc}]", "grey"))
            gevent.sleep(self.refresh_s)

    # -- output --------------------------------------------------------------
    def log(self, *lines):
        """Print above the footer, lifting it out of the way first.

        This writes to the stream captured at construction (the real stdout),
        not to the proxy `install()` puts in `sys.stdout` -- so unlike an
        ordinary `print`, it has to clear the block itself.
        """
        self.footer.clear()
        for line in lines:
            print(line, file=self.stream)
        self.stream.flush()

    def tick(self):
        elapsed = (time.time() - self.started_at) if self.started_at else 0.0
        idx, phase, frac, left = self._phase(elapsed)
        self._announce(idx, phase)
        self._flush_recap()
        if self.live:
            self.footer.draw(self._footer_lines(elapsed, idx, phase, frac, left))
        elif time.time() - self._last_plain >= self.plain_every_s:
            self._last_plain = time.time()
            self.log(self._status_line(elapsed, idx, phase, frac, left))

    # -- where in the ramp are we -------------------------------------------
    def _phase(self, elapsed):
        """(stage_idx, phase, fraction_through_phase, seconds_left_in_phase).

        ``phase`` is ``hold`` (under load), ``drain`` (cooldown gap between
        stages, users at 0 while slow queries finish) or ``done``.
        """
        for start, end, idx in self.windows:
            if start <= elapsed < end:
                span = max(end - start, 1e-9)
                if idx is None:
                    # Cooldown: attribute it to the stage that just finished, as
                    # the collector does -- drained queries land in that stage.
                    return (self._stage_before(start), "drain",
                            (elapsed - start) / span, end - elapsed)
                return idx, "hold", (elapsed - start) / span, end - elapsed
        return len(self.stages) - 1, "done", 1.0, 0.0

    def _stage_before(self, cooldown_start):
        prev = 0
        for start, end, idx in self.windows:
            if idx is not None and end <= cooldown_start:
                prev = idx
        return prev

    def _announce(self, idx, phase):
        """Emit the scrolling record: a header per stage, a verdict per stage."""
        if phase == "hold" and idx != self._announced_stage:
            if self._announced_stage >= 0:
                # Queued, not printed: the numbers only settle once the
                # collector has closed that stage out (see _flush_recap).
                self._pending_recap = self._announced_stage
                self._flush_recap()
            users, rate, hold = self.stages[idx]
            self.log("")
            self.log(self.paint(
                f"▶ stage {idx + 1}/{len(self.stages)}  "
                f"{users} users  {fmt_duration(hold)} hold"
                f"  (spawn {rate}/s)", "bold", "cyan"))
            self._announced_stage = idx
            self._draining = False
            # Don't let the periodic plain-mode status line land on top of the
            # stage header with an empty stage behind it.
            self._last_plain = time.time()
        elif phase == "drain" and not self._draining:
            self._draining = True
            inflight = len(getattr(self.collector, "inflight", {}))
            self.log(self.paint(
                f"  … cooldown {fmt_duration(self.cooldown_s)}: draining "
                f"{inflight} in-flight quer{'y' if inflight == 1 else 'ies'} "
                f"into stage {idx + 1}", "grey"))

    def _flush_recap(self):
        """Print a queued verdict line once its stage's window is closed.

        The collector freezes ``stage_ended`` when the stage changes; recapping
        before that would divide by a still-running clock and report a duration
        and RPS that disagree with the row eventually written to stages.csv.
        """
        idx = self._pending_recap
        if idx is None:
            return
        if idx in self.collector.stage_ended or self.collector.stage_idx > idx:
            self._pending_recap = None
            self._recap(idx)

    def _recap(self, idx):
        """One verdict line for a finished stage: the knee test, per criterion."""
        if idx in self._recapped:
            return
        self._recapped.add(idx)
        row = self.stage_stats(idx, "__all__")
        if not row["requests"]:
            self.log(self.paint(
                f"  {_BAD} stage {idx + 1} · no completed requests", "yellow"))
            return
        p99_ok = row["p99_ms"] <= self.p99_slo_ms
        err_ok = row["error_rate"] <= self.max_error_rate
        good = p99_ok and err_ok
        mark = self.paint(_OK, "green") if good else self.paint(_BAD, "red")
        p99 = (f"p99 {fmt_ms(row['p99_ms'])} "
               f"{self.paint(_OK, 'green') if p99_ok else self.paint(_BAD, 'red')}")
        err = (f"err {row['error_rate'] * 100:.2f}% "
               f"{self.paint(_OK, 'green') if err_ok else self.paint(_BAD, 'red')}")
        self.log(
            f"  {mark} stage {idx + 1} · {row['users']}u · "
            f"{fmt_duration(row['duration_s'])} · n={row['requests']} · "
            f"{row['rps']:.2f} rps · {p99} · {err} · "
            f"conc {row['concurrency']}"
            + ("" if good else self.paint("  (over the bar)", "grey")))

    def recap_final(self):
        """Called at the end so the last stage gets its verdict line too."""
        if self._pending_recap is not None:
            self._recap(self._pending_recap)
            self._pending_recap = None
        if self._announced_stage >= 0:
            self._recap(self._announced_stage)

    # -- the footer ----------------------------------------------------------
    def _width(self):
        return terminal_width()

    def _rule(self, width, text=""):
        if not text:
            return self.paint(_RULE * width, "grey")
        head = f"{_RULE}{_RULE} {text} "
        return self.paint(head + _RULE * max(0, width - vislen(head)), "grey")

    def _live_stage_metrics(self, idx):
        """Live numbers for the active stage, straight off the collector."""
        c = self.collector
        n_req = c.requests[idx]["__all__"]
        n_err = c.errors[idx]["__all__"]
        lat = c.samples[idx]["__all__"]
        started = c.stage_started.get(idx)
        ended = c.stage_ended.get(idx)
        dur = max((ended or time.time()) - started, 1e-9) if started else 1e-9
        mean = (sum(lat) / len(lat)) if lat else 0.0
        return {
            "requests": n_req,
            "errors": n_err,
            "error_rate": (n_err / n_req) if n_req else 0.0,
            "rps": n_req / dur,
            "mean_ms": mean,
            "p50_ms": _pct(lat, 50),
            "p95_ms": _pct(lat, 95),
            "p99_ms": _pct(lat, 99),
            "concurrency": (n_req / dur) * (mean / 1000.0),
        }

    def _footer_lines(self, elapsed, idx, phase, frac, left):
        w = self._width()
        p = self.paint
        users = self.stages[idx][0] if idx < len(self.stages) else 0
        m = self._live_stage_metrics(idx)
        inflight = len(getattr(self.collector, "inflight", {}))

        title = f"HelmsDeep · {self.label} ({self.target})"
        if self.time_scale < 1.0:
            title += f" · compressed x{self.time_scale:.2f}"
        lines = [self._rule(w, p(title, "bold"))]

        run_frac = min(elapsed / self.total_s, 1.0) if self.total_s else 0.0
        if phase == "done":
            # The ramp is over but users are still finishing their last query
            # (stop_timeout), so elapsed keeps climbing past the plan. Say that
            # rather than showing a finish time already in the past.
            tail = f"{fmt_duration(elapsed)} in · ramp complete, draining"
        else:
            eta = time.strftime("%H:%M", time.localtime(
                time.time() + max(self.total_s - elapsed, 0)))
            tail = (f"{fmt_duration(elapsed)} in · "
                    f"{fmt_duration(self.total_s - elapsed)} left · ends ~{eta}")
        lines.append(
            f" run    {bar(run_frac, 24, p, ('cyan',))} {run_frac * 100:3.0f}%  "
            + p(tail, "grey"))

        phase_label = {"hold": "HOLD", "drain": "DRAIN", "done": "DONE"}[phase]
        phase_style = {"hold": ("green",), "drain": ("yellow",),
                       "done": ("grey",)}[phase]
        lines.append(
            " stage  " + p(f"{idx + 1}/{len(self.stages)}", "bold")
            + f"  {users}u  " + p(phase_label, *phase_style) + "  "
            + bar(frac, 14, p, phase_style) + f" {frac * 100:3.0f}%  "
            + p(f"{fmt_duration(left)} left", "grey"))

        err_style = ("red",) if m["error_rate"] > self.max_error_rate else ("green",)
        lines.append(
            f" load   {m['requests']} done · {inflight} in flight · "
            + p(f"{m['errors']} failed ({m['error_rate'] * 100:.1f}%)", *err_style)
            + f" · {m['rps']:.2f} rps · conc {m['concurrency']:.1f}")

        p99_style = ("red",) if m["p99_ms"] > self.p99_slo_ms else ("green",)
        lines.append(
            f" latency p50 {fmt_ms(m['p50_ms'])} · p95 {fmt_ms(m['p95_ms'])} "
            f"· p99 " + p(fmt_ms(m["p99_ms"]), *p99_style)
            + p(f" / SLO {fmt_ms(self.p99_slo_ms)}", "grey"))

        if self.protocol == "async":
            lines.append(" ars    " + self._ars_line(idx))

        lines.append(self._rule(w))
        return [truncate(line, w) for line in lines]

    def _ars_line(self, idx):
        c = self.collector
        statuses = c.statuses[idx]["__all__"]
        counts = c.result_counts[idx]["__all__"]
        zero = sum(1 for n in counts if n == 0)
        inflight = getattr(c, "inflight", {})
        oldest = ""
        if inflight:
            age = time.time() - min(start for start, _ in inflight.values())
            oldest = f" · oldest {fmt_duration(age)}"
        parts = [f"Done {statuses.get('Done', 0)}"]
        for key, style in (("Error", "red"), ("Timeout", "red"),
                           ("SubmitError", "red"), ("NoPK", "red")):
            if statuses.get(key):
                parts.append(self.paint(f"{key} {statuses[key]}", style))
        if zero:
            parts.append(self.paint(f"0-result {zero}", "yellow"))
        return " · ".join(parts) + self.paint(oldest, "grey")

    # -- plain (non-TTY) mode ------------------------------------------------
    def _status_line(self, elapsed, idx, phase, frac, left):
        users = self.stages[idx][0] if idx < len(self.stages) else 0
        m = self._live_stage_metrics(idx)
        inflight = len(getattr(self.collector, "inflight", {}))
        where = (f"{frac * 100:.0f}% ({fmt_duration(left)} left)"
                 if phase != "done" else "ramp complete, draining")
        return (
            f"[{fmt_duration(elapsed)}/{fmt_duration(self.total_s)}] "
            f"stage {idx + 1}/{len(self.stages)} {users}u {phase.upper()} "
            f"{where} · "
            f"{m['requests']} done, {inflight} in flight, {m['errors']} failed "
            f"({m['error_rate'] * 100:.1f}%) · {m['rps']:.2f} rps · "
            f"p99 {fmt_ms(m['p99_ms'])} / SLO {fmt_ms(self.p99_slo_ms)}"
        )
