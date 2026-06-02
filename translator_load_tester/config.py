"""
Component (target) registry for the Translator Load Tester.

The Translator stack cascades ARS -> ARAs -> KPs, so a run targets exactly ONE
layer at a time (see CLAUDE.md "Layering rule"). Each layer differs only in
(1) the request protocol and (2) which corpus subset is sent; the step-load
engine in ``trapi_loadtest.py`` is shared across all of them.

``--targets`` on the CLI selects one of these keys. Only ``kps`` (Retriever) is
wired up today; ``aras``/``ars`` are placeholders that mark where the Shepherd
and ARS pipelines will plug in.

Fields:
  label        human name for the component
  endpoint     request path appended to --host
  corpus       key into trapi_corpus.CORPUS_BY_NAME (which query subset to send)
  protocol     "sync" (blocking POST) or "async" (ARS submit/poll/merge)
  implemented  whether the pipeline is runnable yet
"""

TARGETS = {
    "kps": {
        "label": "Retriever",
        "endpoint": "/query",
        "corpus": "retriever",
        "protocol": "sync",
        "implemented": True,
    },
    "aras": {
        "label": "Shepherd",
        "endpoint": "/query",
        "corpus": "shepherd",
        "protocol": "sync",
        "implemented": True,
    },
    "ars": {
        "label": "ARS",
        "endpoint": "/submit",
        "corpus": "ars",
        "protocol": "async",
        "implemented": False,
    },
}

DEFAULT_TARGET = "kps"
