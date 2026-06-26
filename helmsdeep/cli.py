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
import subprocess
import sys

from . import config

_LOCUSTFILE = os.path.join(os.path.dirname(__file__), "trapi_loadtest.py")


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
    parser.add_argument(
        "--csv-prefix",
        default=None,
        help="Prefix for output files (<prefix>_stages.csv, etc.). "
             "Falls back to the LOCUST_CSV_PREFIX env var, then 'trapi_run'.",
    )
    return parser.parse_args(argv)


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
    print(f"Locust HTML report will be written to {html_report}")
    return subprocess.run(cmd, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
