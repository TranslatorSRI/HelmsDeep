"""
``helmsdeep`` -- CLI entry point for HelmsDeep.

Selects exactly ONE Translator layer per run (the stack cascades
ARS -> ARAs -> KPs, so testing one layer already loads everything beneath it;
see CLAUDE.md "Layering rule"). It wires up the chosen component's endpoint and
corpus via an env var, then launches the shared Locust step-load engine.

The StepLoad shape owns users/spawn-rate/duration, so this CLI intentionally
does NOT expose -u/-r/-t -- they would fight the shape.

Usage:
    helmsdeep --targets kps --host https://retriever.example.org \
        --csv-prefix run1
"""

import argparse
import os
import re
import subprocess
import sys

from . import config

_LOCUSTFILE = os.path.join(os.path.dirname(__file__), "trapi_loadtest.py")

# --quick is just a preset budget: the whole ramp inside a coffee break.
QUICK_BUDGET_S = 600


def _duration(text):
    """Parse a duration -- '600', '10m', '1h', '1h30m' -- into seconds."""
    text = str(text).strip().lower()
    if text.isdigit():
        seconds = int(text)
    else:
        match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text)
        if not match or not any(match.groups()):
            raise argparse.ArgumentTypeError(
                f"invalid duration {text!r}; use e.g. 600, 90s, 10m, 1h30m")
        h, m, sec = (int(g or 0) for g in match.groups())
        seconds = h * 3600 + m * 60 + sec
    if seconds <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return seconds


def _fmt_duration(seconds):
    """Human-readable duration for the printed run plan."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="helmsdeep",
        description="Measure max sustainable concurrency for a Translator "
                    "component (KPs/ARAs/ARS). One layer per run.",
    )
    parser.add_argument(
        "--targets",
        required=True,
        choices=sorted(config.TARGETS),
        help="Which Translator layer to characterize (exactly one).",
    )
    parser.add_argument(
        "--host",
        required=True,
        help="Base URL of the target service "
             "(e.g. https://retriever.ci.transltr.io).",
    )
    budget = parser.add_mutually_exclusive_group()
    budget.add_argument(
        "--time-budget",
        type=_duration,
        default=None,
        metavar="DURATION",
        help="Compress the run to roughly this wall clock (e.g. 30m, 1h, 600). "
             "Stage holds, cooldowns, and poll/timeout caps shrink to fit; the "
             "ramp (user counts, SLOs, checkpoints) is unchanged. Fewer samples "
             "per stage, so the numbers are indicative, not a measurement. "
             "Ignored if it exceeds the target's natural duration.",
    )
    budget.add_argument(
        "--quick",
        action="store_true",
        help=f"Smoke run: shorthand for --time-budget "
             f"{QUICK_BUDGET_S // 60}m. Checks a host, corpus, and config end "
             f"to end in a coffee break.",
    )
    parser.add_argument(
        "--csv-prefix",
        default=None,
        help="Prefix for output files (<prefix>_stages.csv, etc.). "
             "Falls back to the LOCUST_CSV_PREFIX env var, then 'trapi_run'.",
    )
    return parser.parse_args(argv)


def _print_plan(plan, natural_s, budget_s, scale):
    """Print the ramp about to run, and what a time budget did to it."""
    planned_s = config.natural_duration_s(plan)
    stages = " -> ".join(f"{users}u x {_fmt_duration(hold)}"
                         for users, _rate, hold in plan["stages"])
    if scale < 1.0:
        print(f"Compressed run: {_fmt_duration(natural_s)} -> "
              f"{_fmt_duration(planned_s)} (time scale {scale:.2f}), "
              f"budget {_fmt_duration(budget_s)}")
        if planned_s > budget_s * 1.05:
            # Compression stops at the floors rather than shrinking a stage to
            # where it exercises nothing; be honest about the resulting overrun.
            print(f"  Budget undershoots the floors "
                  f"({config.MIN_HOLD_S}s minimum per stage), so the run takes "
                  f"{_fmt_duration(planned_s)} instead.")
        print(f"  Stages: {stages}"
              + (f", {_fmt_duration(plan['cooldown_s'])} cooldown between"
                 if plan.get("cooldown_s") else ""))
        # Whichever cap gates a single query on this protocol got cut too, and
        # that changes what counts as a failure -- say so explicitly.
        cap = plan.get("max_poll_s") or plan.get("request_timeout_s")
        if cap:
            print(f"  Per-query cap cut to {_fmt_duration(cap)} -- a query "
                  f"slower than that is recorded as a\n  timeout failure, so "
                  f"error rates are not comparable to a full run's.")
        print("  Far fewer samples per stage: treat the percentiles, error rates, "
              "and any\n  checkpoint verdict as indicative, not a measurement.")
    else:
        if budget_s:
            print(f"Time budget {_fmt_duration(budget_s)} is already above this "
                  f"target's natural duration -- running in full.")
        print(f"Run plan: {_fmt_duration(planned_s)} -- {stages}")


def main(argv=None):
    args = _parse_args(argv)
    tgt = config.TARGETS[args.targets]

    if not tgt["implemented"]:
        runnable = ", ".join(t for t in sorted(config.TARGETS)
                             if config.TARGETS[t]["implemented"])
        print(
            f"The {tgt['label']} ({args.targets}) pipeline is not yet "
            f"implemented. Runnable targets: {runnable}.",
            file=sys.stderr,
        )
        return 1

    # The locustfile reads the output-file prefix from LOCUST_CSV_PREFIX (locust
    # itself has no --csv-prefix flag), so pass it via the environment.
    env = dict(os.environ)
    env["LOADTEST_TARGET"] = args.targets
    if args.csv_prefix:
        env["LOCUST_CSV_PREFIX"] = args.csv_prefix

    # Optional wall-clock budget. The locustfile applies the same compression to
    # the target config at import; we compute it here too, purely to print the
    # plan the operator is about to commit to.
    budget_s = QUICK_BUDGET_S if args.quick else args.time_budget
    natural_s = config.natural_duration_s(tgt)
    plan, scale = (config.time_scaled(tgt, budget_s) if budget_s else (tgt, 1.0))
    if budget_s:
        env["HELMSDEEP_TIME_BUDGET_S"] = str(budget_s)

    # Derive the output prefix exactly as the locustfile does (CLI flag ->
    # LOCUST_CSV_PREFIX env var -> "trapi_run") so the HTML report file name
    # lines up with the CSV/JSON outputs.
    prefix = args.csv_prefix or os.environ.get("LOCUST_CSV_PREFIX") or "trapi_run"
    html_report = f"{prefix}_report.html"

    cmd = [
        sys.executable, "-m", "locust",
        "-f", _LOCUSTFILE,
        "--headless",
        "--host", args.host,
        # Locust's native self-contained HTML report (charts + request/failure
        # tables). It's a supplementary visual artifact; the authoritative knee
        # lives in <prefix>_summary.json (see README "How to read the results").
        "--html", html_report,
    ]

    print(f"Load-testing {tgt['label']} ({args.targets}) at {args.host}{tgt['endpoint']}")
    _print_plan(plan, natural_s, budget_s, scale)
    print(f"Locust HTML report will be written to {html_report}")
    return subprocess.run(cmd, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
