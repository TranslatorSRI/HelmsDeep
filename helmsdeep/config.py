"""
Component (target) registry for HelmsDeep.

The Translator stack cascades ARS -> ARAs -> KPs, so a run targets exactly ONE
layer at a time (see CLAUDE.md "Layering rule"). Each layer differs only in
(1) the request protocol and (2) which corpus subset is sent; the step-load
engine in ``trapi_loadtest.py`` is shared across all of them.

``--targets`` on the CLI selects one of these keys. ``kps`` (Retriever),
``aras`` (Shepherd), and ``ars`` (the async submit/poll/merge pipeline) are all
wired up.

Per-target load + SLO tuning lives here because cost profiles differ wildly by
layer: a KP lookup is cheap, an ARA creative query is expensive, and an ARS run
takes minutes to ~an hour. So each target carries its own step-load ramp and
p99 knee threshold. The error-rate cap is shared (``MAX_ERROR_RATE``).

Fields:
  label        human name for the component
  endpoint     request path appended to --host
  corpus       key into trapi_corpus.CORPUS_BY_NAME (which query subset to send)
  protocol     "sync" (blocking POST) or "async" (ARS submit/poll/merge)
  implemented  whether the pipeline is runnable yet
  stages       step-load ramp: list of (users, spawn_rate, hold_seconds). Each
               stage should hold long enough for a stable measurement.
  p99_slo_ms   knee requires this layer's stage p99 <= this (ms)

Async (``ars``) targets add:
  messages_endpoint  base path for polling/merge: /messages/{pk}
  poll_interval_s    seconds between status polls (gevent.sleep)
  max_poll_s         per-query poll cap; exceeding it marks a Timeout failure
"""

# Shared across all targets: a stage also needs error_rate <= this to be a knee.
MAX_ERROR_RATE = 0.01   # 1%

TARGETS = {
    "kps": {
        "label": "Retriever",
        "endpoint": "/query",
        "corpus": "retriever",
        "protocol": "sync",
        "implemented": True,
        # Cheap lookups -- ramp high to find saturation.
        "stages": [
            (5,   5, 60),
            (10,  5, 60),
            (20,  5, 60),
            (40, 10, 60),
            (80, 10, 60),
            (120, 20, 60),
            (160, 20, 60),
        ],
        "p99_slo_ms": 60000,
    },
    "aras": {
        "label": "Shepherd",
        "endpoint": "/query",
        "corpus": "shepherd",
        "protocol": "sync",
        "implemented": True,
        # Creative (inferred) reasoning is far heavier -- gentler ramp, longer
        # holds (slow queries need time to accumulate samples), looser p99.
        "stages": [
            (2,  1, 300),  # 5 mins
            (3,  1, 300),  # 5 mins
            (5,  2, 330),  # 5.5 mins
            (10, 2, 360),  # 6 mins
            (20, 5, 420),  # 7 mins
            (40, 5, 600),  # 10 mins
        ],
        "p99_slo_ms": 210000,
    },
    "ars": {
        "label": "ARS",
        "endpoint": "/ars/api/submit",            # submit path (appended to --host)
        "messages_endpoint": "/ars/api/messages", # poll/merge base: /messages/{pk}
        "corpus": "ars",
        "protocol": "async",
        "implemented": True,
        "poll_interval_s": 10,            # gevent.sleep between status polls
        "max_poll_s": 360,                # 6-min cap; exceeding it = Timeout
        # Runs take minutes -- very low concurrency, long holds.
        "stages": [
            (2,  1, 300),  # 5 mins
            (3,  1, 300),  # 5 mins
            (5,  2, 330),  # 5.5 mins
            (10, 2, 360),  # 6 mins
            (20, 5, 420),  # 7 mins
            (40, 5, 600),  # 10 mins
        ],
        "p99_slo_ms": 240000,             # 4-min knee target (< max_poll_s)
    },
    # Pathfinder is its own run type (ARA + ARS only): it pins two endpoints and
    # asks for connecting paths -- the most intensive query class. Same endpoints
    # as aras/ars; only the corpus, ramp, and SLO differ (gentler + looser).
    "aras_pathfinder": {
        "label": "Shepherd (Pathfinder)",
        "endpoint": "/query",
        "corpus": "pathfinder",
        "protocol": "sync",
        "implemented": True,
        # Heavier than `aras`: lower concurrency, longer holds, looser p99.
        "stages": [
            (2,  1, 300),  # 5 mins
            (3,  1, 300),  # 5 mins
            (5,  2, 330),  # 5.5 mins
            (10, 2, 360),  # 6 mins
            (20, 5, 420),  # 7 mins
            (40, 5, 600),  # 10 mins
        ],
        "p99_slo_ms": 300000,             # 5-min knee target (vs 2 min for aras)
    },
    "ars_pathfinder": {
        "label": "ARS (Pathfinder)",
        "endpoint": "/submit",
        "messages_endpoint": "/messages",
        "corpus": "pathfinder",
        "protocol": "async",
        "implemented": True,
        "poll_interval_s": 10,
        "max_poll_s": 360,               # 6-min cap
        "stages": [
            (2,  1, 300),  # 5 mins
            (3,  1, 300),  # 5 mins
            (5,  2, 330),  # 5.5 mins
            (10, 2, 360),  # 6 mins
            (20, 5, 420),  # 7 mins
            (40, 5, 600),  # 10 mins
        ],
        "p99_slo_ms": 300000,            # 5-min knee target (< max_poll_s)
    },
}

DEFAULT_TARGET = "kps"
