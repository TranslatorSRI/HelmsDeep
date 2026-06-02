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
"""

# A few real-ish CURIEs from the Biolink/MONDO/CHEBI space used in TRAPI docs.
# Swap these for entities your target service actually knows about.
T2D = "MONDO:0005148"          # type-2 diabetes
METFORMIN = "CHEBI:6801"
ALZHEIMERS = "MONDO:0004975"
PARKINSONS = "MONDO:0005180"
ASTHMA = "MONDO:0004979"

DISEASE_BATCH = [T2D, ALZHEIMERS, PARKINSONS, ASTHMA, "MONDO:0007739"]  # +Huntington


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
# Cost varies with the pinned disease (answer-set size / reasoning depth), so we
# vary the disease across a few entities and break out latency per qtype.
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


def inferred_treats_t2d():
    """What treats type-2 diabetes? (common disease, large answer set)."""
    return _inferred_treats(T2D)


def inferred_treats_alzheimers():
    """What treats Alzheimer's?"""
    return _inferred_treats(ALZHEIMERS)


def inferred_treats_parkinsons():
    """What treats Parkinson's?"""
    return _inferred_treats(PARKINSONS)


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

# Shepherd (ARA): inferred (creative) mode, cache bypassed. Weighted toward the
# common disease (the heaviest answer set); vary the entity to spread cost.
SHEPHERD_CORPUS = [
    ("inferred_treats_t2d",        inferred_treats_t2d,        40),
    ("inferred_treats_alzheimers", inferred_treats_alzheimers, 30),
    ("inferred_treats_parkinsons", inferred_treats_parkinsons, 30),
]

# ARS: also inferred queries (it fans out to the ARAs). Reuses the Shepherd
# corpus for now; wired when the ARS async submit/poll/merge pipeline lands.
ARS_CORPUS = SHEPHERD_CORPUS

CORPUS_BY_NAME = {
    "retriever": RETRIEVER_CORPUS,
    "shepherd": SHEPHERD_CORPUS,
    "ars": ARS_CORPUS,
}


def corpus_for(name):
    """Return the (qtype, builder, weight) list for a component corpus name."""
    return CORPUS_BY_NAME[name]
