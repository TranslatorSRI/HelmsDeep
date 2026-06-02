"""
TRAPI query corpus for load testing NCATS Translator services.

Cost in TRAPI is driven by query-graph SHAPE, not text length. The dimensions
that matter for load characterization:

  - hops:        one-hop (n0-e0-n1) vs multi-hop (n0-e0-n1-e1-n2). More hops =
                 more graph expansion = more work.
  - mode:        "lookup" (exact DB match) vs "inferred" (reasoning/creative
                 mode, ARAs may expand one-hops). Inferred is far more expensive.
  - constraint:  fully-pinned (both nodes have ids) vs open (one node is
                 category-only, the answer set). Open nodes fan out.
  - batch:       number of CURIEs in an `ids` list. Larger batch = more lookups.
  - predicate:   specific predicate vs none (any predicate) vs broad category.

Each query is tagged with a `qtype` so per-type latency can be broken out.
Adjust the per-corpus weights to match your production traffic distribution --
uniform testing will NOT predict real aggregate latency.

Corpuses are segmented per Translator component (see CLAUDE.md). Retriever (KP)
wants `lookup`-mode queries and a scalar `parameters.tier`; Shepherd (ARA) and
ARS want `inferred`-mode queries. `corpus_for(name)` returns the right subset.

Retriever's `tier` selects the backend graph and is therefore a load dimension,
so each builder pins its own tier deliberately:
  - tier 0 -- backend graph that handles arbitrary multi-hop queries
  - tier 1 -- backend graph that handles mostly single-hop queries
Multi-hop shapes are paired with tier 0, single-hop shapes with tier 1.

The inferred (ARA/ARS) corpus instead varies the pinned DISEASE per request:
for creative "what treats X?" queries the answer-set size of the pinned disease
dominates cost far more than graph shape, so we sample from size-tiered disease
pools (heavy/medium/light) to be representative of production traffic and to
avoid the cache-warming artifact of repeating one entity.
"""

import json
import os
import random


# A few real-ish CURIEs from the Biolink/MONDO/CHEBI space used in TRAPI docs.
# Swap these for entities your target service actually knows about.
T2D = "MONDO:0005148"          # type-2 diabetes
METFORMIN = "CHEBI:6801"
ALZHEIMERS = "MONDO:0004975"
PARKINSONS = "MONDO:0005180"
ASTHMA = "MONDO:0004979"

DISEASE_BATCH = [T2D, ALZHEIMERS, PARKINSONS, ASTHMA, "MONDO:0007739"]  # +Huntington


# ---------------------------------------------------------------------------
# Disease entity pools for the inferred (ARA/ARS) corpus.
#
# Heavy = common, heavily-studied disease hubs with large answer sets (curated
# by domain knowledge). The long-tail pool is ~1000 real MONDO diseases restored
# from git history (curie_list.json) and is dominated by rarer diseases, so it
# stands in for medium/light traffic. Tiering by *measured* answer-set size
# isn't possible offline -- medium and light currently draw from the same
# long-tail pool; split it once you have per-disease degree/result-size data.
# The heavy/medium/light WEIGHTS (in SHEPHERD_CORPUS) and the per-tier qtype
# labels are already in place, so that refinement is a data change, not a code
# change.
# ---------------------------------------------------------------------------
HEAVY_DISEASES = [T2D, ASTHMA, ALZHEIMERS, PARKINSONS, "MONDO:0007739"]


def _load_disease_pool():
    """Load the long-tail disease pool (MONDO CURIEs) from curie_list.json."""
    path = os.path.join(os.path.dirname(__file__), "curie_list.json")
    try:
        with open(path) as f:
            pool = [c for c in json.load(f)
                    if isinstance(c, str) and c.startswith("MONDO:")]
        if pool:
            return pool
    except (OSError, ValueError):
        pass
    # Fallback so the module still imports if the data file is missing.
    return [ALZHEIMERS, PARKINSONS, ASTHMA, T2D]


LONG_TAIL_DISEASES = _load_disease_pool()


def _qg(nodes, edges, tier=None, bypass_cache=None):
    """Wrap a query graph in the TRAPI envelope.

    Optional top-level fields are added only when supplied so each component
    sends what its contract expects and nothing it doesn't:
      - ``tier``: Retriever's scalar ``parameters.tier`` (0 or 1). KP-only; it
        is meaningless to ARAs/ARS, so inferred builders leave it off.
      - ``bypass_cache``: forces fresh reasoning. Used by creative-mode (ARA)
        queries so the load test measures reasoning cost, not cache hits.
    """
    env = {
        "message": {
            "query_graph": {"nodes": nodes, "edges": edges}
        }
    }
    if tier is not None:
        env["parameters"] = {"tier": tier}
    if bypass_cache is not None:
        env["bypass_cache"] = bypass_cache
    return env


def one_hop_lookup_pinned():
    """Cheapest: both ends pinned, lookup mode, specific predicate. Single-hop -> tier 1."""
    return _qg(
        nodes={
            "n0": {"ids": [METFORMIN], "categories": ["biolink:ChemicalEntity"]},
            "n1": {"ids": [T2D], "categories": ["biolink:Disease"]},
        },
        edges={
            "e0": {"subject": "n0", "object": "n1",
                   "predicates": ["biolink:treats"],
                   "knowledge_type": "lookup"},
        },
        tier=1,
    )


def one_hop_lookup_open():
    """One open node (the answer set) -- fans out to many candidates. Single-hop -> tier 1."""
    return _qg(
        nodes={
            "n0": {"categories": ["biolink:ChemicalEntity"]},          # open
            "n1": {"ids": [T2D], "categories": ["biolink:Disease"]},
        },
        edges={
            "e0": {"subject": "n0", "object": "n1",
                   "predicates": ["biolink:treats"],
                   "knowledge_type": "lookup"},
        },
        tier=1,
    )


# ----------------------------------------------------------------------------
# Shepherd (ARA) creative-mode builders.
#
# ARAs answer "inferred" (creative) queries: an open subject node, a pinned
# object, and `knowledge_type: "inferred"` on the edge. The ARA reasons over the
# graph rather than doing an exact lookup, so these are far more expensive than
# any KP lookup. We bypass the cache so repeated identical queries measure real
# reasoning cost instead of cache retrieval. No `tier` -- that's KP-only.
#
# The canonical Translator creative query is "what chemicals treat disease X?".
# Cost varies with the pinned disease, so we sample a disease per request from
# size-tiered pools (heavy/medium/light) and break out latency per tier.
# ----------------------------------------------------------------------------
def _inferred_treats(disease):
    """Creative-mode: open chemical -[treats, inferred]-> pinned disease."""
    return _qg(
        nodes={
            "n0": {"categories": ["biolink:ChemicalEntity"]},          # open answer set
            "n1": {"ids": [disease], "categories": ["biolink:Disease"]},
        },
        edges={
            "e0": {"subject": "n0", "object": "n1",
                   "predicates": ["biolink:treats"],
                   "knowledge_type": "inferred"},
        },
        bypass_cache=True,
    )


def inferred_heavy():
    """Heavy: a common, highly-connected disease hub -> large answer set."""
    return _inferred_treats(random.choice(HEAVY_DISEASES))


def inferred_medium():
    """Medium: a long-tail disease (see pool note above)."""
    return _inferred_treats(random.choice(LONG_TAIL_DISEASES))


def inferred_light():
    """Light: a long-tail disease -> typically small answer set."""
    return _inferred_treats(random.choice(LONG_TAIL_DISEASES))


def one_hop_no_predicate():
    """No predicate constraint -- matches any relationship; broad. Single-hop -> tier 1."""
    return _qg(
        nodes={
            "n0": {"categories": ["biolink:ChemicalEntity"]},
            "n1": {"ids": [T2D]},
        },
        edges={
            "e0": {"subject": "n0", "object": "n1",
                   "knowledge_type": "lookup"},
        },
        tier=1,
    )


def two_hop_lookup():
    """Multi-hop: chemical -> gene -> disease. Graph expansion across two edges.

    Arbitrary multi-hop -> tier 0 (the backend graph that can handle it).
    """
    return _qg(
        nodes={
            "n0": {"categories": ["biolink:ChemicalEntity"]},
            "n1": {"categories": ["biolink:Gene"]},
            "n2": {"ids": [ALZHEIMERS], "categories": ["biolink:Disease"]},
        },
        edges={
            "e0": {"subject": "n0", "object": "n1",
                   "predicates": ["biolink:affects"],
                   "knowledge_type": "lookup"},
            "e1": {"subject": "n1", "object": "n2",
                   "predicates": ["biolink:associated_with"],
                   "knowledge_type": "lookup"},
        },
        tier=0,
    )


def batch_lookup():
    """Batched ids -- multiple diseases pinned in one query. Single-hop -> tier 1."""
    return _qg(
        nodes={
            "n0": {"categories": ["biolink:ChemicalEntity"]},
            "n1": {"ids": DISEASE_BATCH, "categories": ["biolink:Disease"]},
        },
        edges={
            "e0": {"subject": "n0", "object": "n1",
                   "predicates": ["biolink:treats"],
                   "knowledge_type": "lookup"},
        },
        tier=1,
    )


def malformed_query():
    """Error-path latency: edge references a node that doesn't exist."""
    return _qg(
        nodes={"n0": {"ids": [T2D]}},
        edges={"e0": {"subject": "n0", "object": "n_missing",
                      "predicates": ["biolink:treats"]}},
        tier=1,
    )


# ----------------------------------------------------------------------------
# Per-component corpuses. A run targets exactly ONE layer, so each component
# draws from its own list of (qtype label, builder, weight). Weights are
# relative; tune to your traffic.
# ----------------------------------------------------------------------------

# Retriever (KP): lookup-mode only; tier carried by each builder.
RETRIEVER_CORPUS = [
    ("one_hop_lookup_pinned", one_hop_lookup_pinned, 30),
    ("one_hop_lookup_open",   one_hop_lookup_open,   25),
    ("one_hop_no_predicate",  one_hop_no_predicate,  10),
    ("two_hop_lookup",        two_hop_lookup,        10),
    ("batch_lookup",          batch_lookup,          10),
    ("malformed_query",       malformed_query,        5),
]

# Shepherd (ARA): inferred (creative) mode, cache bypassed. A disease is sampled
# per request from size-tiered pools; weights set the production traffic mix
# (heavy/medium/light = 20/30/50). Tune to your real distribution.
SHEPHERD_CORPUS = [
    ("inferred_heavy",  inferred_heavy,  20),
    ("inferred_medium", inferred_medium, 30),
    ("inferred_light",  inferred_light,  50),
]

# ARS: also inferred queries (it fans out to the ARAs). Reuses the Shepherd
# corpus -- same creative query the ARS distributes to its ARAs.
ARS_CORPUS = SHEPHERD_CORPUS

CORPUS_BY_NAME = {
    "retriever": RETRIEVER_CORPUS,
    "shepherd": SHEPHERD_CORPUS,
    "ars": ARS_CORPUS,
}


def corpus_for(name):
    """Return the (qtype, builder, weight) list for a component corpus name."""
    return CORPUS_BY_NAME[name]
