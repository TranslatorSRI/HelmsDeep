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
LEVODOPA = "CHEBI:15765"
DONEPEZIL = "CHEBI:53289"
ALBUTEROL = "CHEBI:2549"       # salbutamol

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


# ---------------------------------------------------------------------------
# Gene pool for MVP2 (chemical<->gene "affects") creative queries. A small
# curated set of well-studied human genes by NCBIGene CURIE; extend/replace with
# genes your target service knows. (Not size-tiered -- no gene-degree data.)
# ---------------------------------------------------------------------------
GENES = [
    "NCBIGene:1956",   # EGFR
    "NCBIGene:7157",   # TP53
    "NCBIGene:3845",   # KRAS
    "NCBIGene:673",    # BRAF
    "NCBIGene:4609",   # MYC
    "NCBIGene:207",    # AKT1
    "NCBIGene:5290",   # PIK3CA
    "NCBIGene:1499",   # CTNNB1
    "NCBIGene:348",    # APOE
    "NCBIGene:351",    # APP
    "NCBIGene:7124",   # TNF
    "NCBIGene:3569",   # IL6
]

# Chemical pool for the pinned-chemical MVP2 variant ("what genes does chemical X
# affect?"). The chemical side is otherwise an open answer set; this small curated
# set of real CHEBI drug CURIEs is only used when the chemical is the pinned
# endpoint. Swap/extend with chemicals your target service actually knows about.
CHEMICALS = [
    METFORMIN,    # CHEBI:6801
    LEVODOPA,     # CHEBI:15765
    DONEPEZIL,    # CHEBI:53289
    ALBUTEROL,    # CHEBI:2549 (salbutamol)
    "CHEBI:15365",  # aspirin
    "CHEBI:5118",   # gefitinib
    "CHEBI:45783",  # imatinib
    "CHEBI:8382",   # prednisone
]

# Canonical MVP2 object-qualifier values (mirrors the Translator TestHarness
# generate_query.py): the aspect is the combined `activity_or_abundance` value the
# real MVP2 acceptance assets send; direction is increased/decreased.
_ASPECT = "activity_or_abundance"
_DIRECTIONS = ["increased", "decreased"]


# ---------------------------------------------------------------------------
# Drug<->disease endpoint pairs for the Pathfinder corpus (ARA/ARS only). Each
# Pathfinder query pins BOTH endpoints and asks for connecting paths, so we use
# pairs that plausibly connect (drug relevant to disease) -- otherwise paths come
# back empty, and on ARS a zero-result Done counts as a failure. Swap these for
# (chemical, disease) pairs your target service actually knows about.
# ---------------------------------------------------------------------------
CHEM_DISEASE_PAIRS = [
    (METFORMIN, T2D),
    (LEVODOPA, PARKINSONS),
    (DONEPEZIL, ALZHEIMERS),
    (ALBUTEROL, ASTHMA),
]


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
# Shepherd (ARA) / ARS creative-mode builders (also used as the ARS corpus).
#
# ARAs/ARS answer "inferred" (creative) queries with `knowledge_type:"inferred"`
# and an open node they reason out. These are far more expensive than any KP
# lookup; we bypass the cache so repeated queries measure real reasoning cost.
# No `tier` -- that's KP-only. Two Translator creative templates are covered:
#
#   MVP1  "what chemicals treat disease X?"  -- open chemical -[treats]-> disease.
#         Cost tracks the pinned disease's answer-set size, so the disease is
#         sampled per request from size-tiered pools (heavy/medium/light).
#   MVP2  chemical -[affects]-> gene with object aspect/direction qualifiers on
#         the gene. The edge is ALWAYS oriented chemical(subject) -> gene(object)
#         (matching the Translator TestHarness generate_query.py); the two
#         variants differ only in which endpoint is pinned -- pin the gene and
#         leave the chemical open ("what chemicals affect gene X?"), or pin the
#         chemical and leave the gene open ("what genes does chemical X affect?").
#         The entity + direction vary per request.
# ----------------------------------------------------------------------------
def _inferred_treats(disease):
    """MVP1: open chemical -[treats, inferred]-> pinned disease."""
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


def mvp1_heavy():
    """MVP1 heavy: a common, highly-connected disease hub -> large answer set."""
    return _inferred_treats(random.choice(HEAVY_DISEASES))


def mvp1_medium():
    """MVP1 medium: a long-tail disease (see pool note above)."""
    return _inferred_treats(random.choice(LONG_TAIL_DISEASES))


def mvp1_light():
    """MVP1 light: a long-tail disease -> typically small answer set."""
    return _inferred_treats(random.choice(LONG_TAIL_DISEASES))


def _affects(subject_node, object_node, aspect, direction):
    """MVP2: an inferred `affects` edge with object aspect/direction qualifiers.

    The edge is always oriented chemical(subject) -> gene(object), so the object
    node is the gene and the qualifiers always describe that gene. Only the two
    qualifiers the reference emits are sent (object aspect + direction); the
    `qualified_predicate` carried on real test assets is intentionally omitted.
    """
    return _qg(
        nodes={"n0": subject_node, "n1": object_node},
        edges={
            "e0": {"subject": "n0", "object": "n1",
                   "predicates": ["biolink:affects"],
                   "knowledge_type": "inferred",
                   "qualifier_constraints": [{"qualifier_set": [
                       {"qualifier_type_id": "biolink:object_aspect_qualifier",
                        "qualifier_value": aspect},
                       {"qualifier_type_id": "biolink:object_direction_qualifier",
                        "qualifier_value": direction},
                   ]}]},
        },
        bypass_cache=True,
    )


def mvp2_chem_affects_gene():
    """MVP2 (gene pinned): what chemicals change gene X's activity/abundance?

    Open ChemicalEntity subject -> pinned Gene object; qualifiers on the gene.
    """
    return _affects(
        {"categories": ["biolink:ChemicalEntity"]},                 # open subject
        {"ids": [random.choice(GENES)], "categories": ["biolink:Gene"]},  # pinned object
        _ASPECT, random.choice(_DIRECTIONS),
    )


def mvp2_chem_affects_open_gene():
    """MVP2 (chemical pinned): what genes does chemical X affect?

    Pinned ChemicalEntity subject -> open Gene object. Same chemical -> gene edge
    orientation as the gene-pinned variant; qualifiers still describe the gene.
    """
    return _affects(
        {"ids": [random.choice(CHEMICALS)], "categories": ["biolink:ChemicalEntity"]},  # pinned subject
        {"categories": ["biolink:Gene"]},                           # open object
        _ASPECT, random.choice(_DIRECTIONS),
    )


# ----------------------------------------------------------------------------
# Pathfinder builders (ARA/ARS only -- its own run type).
#
# A Pathfinder query pins TWO endpoint nodes and asks for connecting paths via a
# `paths` map in the query_graph (not `edges`). The service combines lookup +
# inferred reasoning to find multi-hop paths between them, so this is the most
# intensive query class -- hence its own heavier ramp / looser SLO in config.
# `predicates` on a QPath conveys the *desired* path type (not a hard filter);
# `constraints[].intermediate_categories` hints at what may sit on the path.
# Pathfinder has no `knowledge_type` (that's edge-only) and no `tier` (ARA/ARS).
# ----------------------------------------------------------------------------
def _pathfinder_qg(nodes, paths, bypass_cache=True):
    """Wrap a Pathfinder query graph (nodes + paths) in the TRAPI envelope.

    An empty ``edges`` map is included for validators that still require the key;
    the ``paths`` map carries the actual query.
    """
    env = {
        "message": {
            "query_graph": {"nodes": nodes, "paths": paths}
        }
    }
    if bypass_cache is not None:
        env["bypass_cache"] = bypass_cache
    return env


def pathfinder_drug_disease():
    """Pathfinder: find paths between a pinned drug and a pinned disease."""
    chem, disease = random.choice(CHEM_DISEASE_PAIRS)
    return _pathfinder_qg(
        nodes={
            "n0": {"ids": [chem], "categories": ["biolink:ChemicalEntity"]},
            "n1": {"ids": [disease], "categories": ["biolink:Disease"]},
        },
        paths={
            "p0": {
                "subject": "n0",
                "object": "n1",
                # Hint that gene-mediated mechanism paths are of interest.
                # "constraints": [{"intermediate_categories": ["biolink:Gene"]}],
            },
        },
    )


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

# Shepherd (ARA): inferred (creative) mode, cache bypassed. An even MVP1/MVP2
# split (50/50). MVP1 (treats-disease) keeps its tiered disease sampling
# (heavy/medium/light = 20/30/50 within its half); MVP2 (chemical -[affects]->
# gene) covers both pinning variants evenly. Tune all weights to your traffic mix.
SHEPHERD_CORPUS = [
    # MVP1 -- "what treats disease X?" (tiered by disease answer-set size): 50%.
    ("mvp1_heavy",  mvp1_heavy,  10),
    ("mvp1_medium", mvp1_medium, 15),
    ("mvp1_light",  mvp1_light,  25),
    # MVP2 -- chemical -[affects]-> gene with qualifiers, gene-pinned and
    # chemical-pinned variants: 50%.
    ("mvp2_chem_affects_gene",      mvp2_chem_affects_gene,      25),
    ("mvp2_chem_affects_open_gene", mvp2_chem_affects_open_gene, 25),
]

# ARS: also inferred queries (it fans out to the ARAs). Reuses the Shepherd
# corpus -- same creative query the ARS distributes to its ARAs.
ARS_CORPUS = SHEPHERD_CORPUS

# Pathfinder (ARA/ARS only): its own run type. A single drug<->disease shape with
# the endpoint pair varied per request. Far heavier than the inferred corpus, so
# its targets ship a gentler ramp and looser SLO (see config.py).
PATHFINDER_CORPUS = [
    ("pathfinder_drug_disease", pathfinder_drug_disease, 100),
]

CORPUS_BY_NAME = {
    "retriever": RETRIEVER_CORPUS,
    "shepherd": SHEPHERD_CORPUS,
    "ars": ARS_CORPUS,
    "pathfinder": PATHFINDER_CORPUS,
}


def corpus_for(name):
    """Return the (qtype, builder, weight) list for a component corpus name."""
    return CORPUS_BY_NAME[name]
