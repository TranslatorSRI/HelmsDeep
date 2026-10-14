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

Those tiers are MEASURED, not guessed: `mvp_baselines.json` carries one record
per CURIE from a `helmsdeep-profile` pass (result count, latency, its bin), and
the pools below are built from it at import. The same file is the baseline a run
compares its result counts against -- see `baseline_for`.
"""

import json
import math
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
HUNTINGTON = "MONDO:0007739"
SKIN_VASCULAR_DISEASE = "MONDO:0019293"

DISEASE_BATCH = [T2D, ALZHEIMERS, PARKINSONS, ASTHMA, HUNTINGTON, SKIN_VASCULAR_DISEASE]


# ---------------------------------------------------------------------------
# Measured per-CURIE baselines (mvp_baselines.json).
#
# Written by `helmsdeep-profile`, which sends every entity's query once at low
# concurrency and records what came back. One file, two jobs:
#
#   1. the tiered entity pools below -- each CURIE's `bin` is its measured
#      answer-set size relative to the rest of its pool, so `mvp1_heavy`,
#      `mvp1_medium` and `mvp1_light` finally differ in COST and not just in
#      weight (they shared one long-tail pool until this file existed), and
#   2. the expected result count a run checks against (`baseline_for`), so a
#      query that comes back with a fraction of its answers is detectable.
#
# `usable: false` marks a CURIE the profile measured but the corpus must not
# sample: it answered with fewer than `min_usable_results`. Those queries cost
# the service almost nothing, teach a ramp nothing, and on a target with
# zero_result_is_failure they score as a failure every time they are drawn. They
# stay in the file because "this CURIE has no answers here" is a real
# measurement -- it just isn't load.
#
# Re-profile and copy the new <prefix>_baselines.json over this file to retune;
# nothing below needs editing.
# ---------------------------------------------------------------------------
BASELINES_FILE = "mvp_baselines.json"

# Which baseline record set a corpus qtype is judged against. The three MVP1
# qtypes are the same query against differently-sized diseases, so they share
# one record set. A qtype that is absent (every KP qtype, Pathfinder) simply has
# no baseline, and nothing is checked for it.
BASELINE_QTYPES = {
    "mvp1_heavy": "mvp1_treats",
    "mvp1_medium": "mvp1_treats",
    "mvp1_light": "mvp1_treats",
    "mvp2_chem_affects_gene": "mvp2_chem_affects_gene",
    "mvp2_chem_affects_open_gene": "mvp2_chem_affects_open_gene",
}


def _load_baselines():
    """Load mvp_baselines.json; an empty dict if it is missing or unreadable."""
    path = os.path.join(os.path.dirname(__file__), BASELINES_FILE)
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("entities"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


BASELINES = _load_baselines()
# (baseline_qtype, entity) -> record. Flat because that is how it is read: one
# lookup per query, on the hot path.
BASELINE_BY_ENTITY = {
    (qtype, entity): record
    for qtype, entities in (BASELINES.get("entities") or {}).items()
    for entity, record in entities.items()
}


def _binned(qtype, wanted):
    """CURIEs the profile put in `wanted` for `qtype`, usable ones only."""
    return sorted(
        entity for (qt, entity), rec in BASELINE_BY_ENTITY.items()
        if qt == qtype and rec.get("bin") == wanted and rec.get("usable")
    )


def _load_disease_pool():
    """The unbinned disease pool (MONDO CURIEs) from curie_list.json.

    Still the fallback when a tier has no measured members (a fresh checkout
    whose baselines file is missing, or a profile that covered only part of the
    pool): a ramp that samples unprofiled diseases is better than one that
    cannot start.
    """
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

# The three measured MVP1 tiers. Curated hubs are no longer a separate concept:
# the profile put all six of them in `heavy` on merit (758-1001 results each),
# alongside 300+ others, so repeating the same handful -- and warming their
# caches -- is no longer necessary. They remain as named constants because the
# KP corpus pins them directly.
HEAVY_DISEASES = _binned("mvp1_treats", "heavy") or [
    T2D, ASTHMA, ALZHEIMERS, PARKINSONS, HUNTINGTON, SKIN_VASCULAR_DISEASE]
MEDIUM_DISEASES = _binned("mvp1_treats", "medium") or LONG_TAIL_DISEASES
LIGHT_DISEASES = _binned("mvp1_treats", "light") or LONG_TAIL_DISEASES


# ---------------------------------------------------------------------------
# Gene pool for MVP2 (chemical<->gene "affects") creative queries. A small
# curated set of well-studied human genes by NCBIGene CURIE; extend/replace with
# genes your target service knows. (Not size-tiered -- no gene-degree data.)
# ---------------------------------------------------------------------------
_GENES_CURATED = [
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

# Both pools are filtered by `_usable` (defined below) once the baselines are
# loaded: the profile found every gene answering 490-690 results, and one of the
# eight chemicals answering nothing at all.

# Chemical pool for the pinned-chemical MVP2 variant ("what genes does chemical X
# affect?"). The chemical side is otherwise an open answer set; this small curated
# set of real CHEBI drug CURIEs is only used when the chemical is the pinned
# endpoint. Swap/extend with chemicals your target service actually knows about.
_CHEMICALS_CURATED = [
    METFORMIN,    # CHEBI:6801
    LEVODOPA,     # CHEBI:15765
    DONEPEZIL,    # CHEBI:53289
    ALBUTEROL,    # CHEBI:2549 (salbutamol)
    "CHEBI:15365",  # aspirin
    "CHEBI:5118",   # gefitinib
    "CHEBI:45783",  # imatinib
    "CHEBI:8382",   # prednisone
]


def _usable(qtype, curated):
    """Drop the members the profile measured as answering nothing.

    A pinned entity that comes back empty is not a cheap query, it is a dead
    one: it exercises nothing and, where a 0-result answer is scored as a
    failure, it poisons the error rate with a known-empty question. Anything the
    profile has no record of is kept -- absence of data is not evidence of an
    empty answer set.
    """
    kept = [e for e in curated
            if BASELINE_BY_ENTITY.get((qtype, e), {}).get("usable", True)]
    return kept or list(curated)


GENES = _usable("mvp2_chem_affects_gene", _GENES_CURATED)
# Donepezil (CHEBI:53289) answered 0 results on both probes -- see
# profiles/ -- so it drops out here until a profile finds it answers again.
CHEMICALS = _usable("mvp2_chem_affects_open_gene", _CHEMICALS_CURATED)

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
    # (METFORMIN, T2D),
    # (LEVODOPA, PARKINSONS),
    # (DONEPEZIL, ALZHEIMERS),
    # (ALBUTEROL, ASTHMA),
    ('MONDO:0021095', 'MONDO:0005105'),
    ('CHEBI:9139', 'MONDO:0004975'),
    ('CHEBI:5118', 'MONDO:0100233'),
    ('MONDO:0005180', 'MONDO:0005105'),
    ('MONDO:0019632', 'MONDO:0005340'),
    ('CHEBI:27881', 'NCBIGene:2739'),
    ('CHEBI:45783', 'MONDO:0004979'),  # Imatinib -> Asthma
    ('GO:0006914', 'MONDO:0005265'),
    # ('CHEBI:10033', 'MONDO:0004992'),  # Warfarin -> Cancer, DON'T RUN
    ('NCBIGene:3458', 'CHEBI:16828'),
    ('MONDO:0005532', 'MONDO:0005180'),
    ('CHEBI:15647', 'UNII:31YO63LBSN'),
    ('CHEBI:28364', 'MONDO:0005311'),
    ('NCBIGene:3458', 'MONDO:0100096'),
    ('NCBIGene:27240', 'MONDO:0100096'),
    ('CHEBI:3750', 'MONDO:0013209'),
    ('CHEBI:83766', 'MONDO:0008170'),
    ('CHEBI:45783', 'MONDO:0004784'),
    ('UNII:7SE5582Q2P', 'MONDO:0007037'),
    ('MONDO:0005011', 'MONDO:0005180'),
    ('CHEBI:15365', 'MONDO:0005575'),
    ('CHEBI:50924', 'MONDO:0007256'),
    ('CHEBI:45713', 'NCBIGene:2739'),
    ('NCBIGene:54716', 'MONDO:0100096'),
    ('CHEBI:7465', 'MONDO:0008218'),
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
#         sampled per request from the three MEASURED size tiers
#         (heavy/medium/light -- see the baselines note above). The tiers are
#         real: on the profiled ARA their median response times were 68s / 35s /
#         11s, so which tier a request draws from now changes what it costs.
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
    """MVP1 heavy: a measured large-answer-set disease (top third of the pool)."""
    return _inferred_treats(random.choice(HEAVY_DISEASES))


def mvp1_medium():
    """MVP1 medium: a measured mid-sized answer set (middle third)."""
    return _inferred_treats(random.choice(MEDIUM_DISEASES))


def mvp1_light():
    """MVP1 light: a measured small answer set (bottom third, excluding empties)."""
    return _inferred_treats(random.choice(LIGHT_DISEASES))


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


# ----------------------------------------------------------------------------
# Mixed (ARA/ARS only): a realistic blended workload -- 2/3 inferred MVP1+MVP2
# creative queries, 1/3 Pathfinder. The dedicated `shepherd`/`pathfinder`
# corpuses characterize ONE query class in isolation; this one asks the
# operational question instead: can the system hold a target concurrency when
# both classes arrive together, as they do in production?
#
# The split is computed rather than hand-written: MVP1/MVP2 keep their relative
# SHEPHERD_CORPUS weights (so retuning that mix propagates here) and Pathfinder
# is scaled to half their combined weight, i.e. 1/3 of the whole.
# ----------------------------------------------------------------------------
# Inferred : Pathfinder weight ratio of the blended corpus -- 2/3 MVP1+MVP2,
# 1/3 Pathfinder.
INFERRED_PATHFINDER_RATIO = (2, 1)


def _mixed_corpus(inferred=None, pathfinder=None, ratio=INFERRED_PATHFINDER_RATIO):
    """Blend the inferred and Pathfinder corpuses at a fixed ratio of shares.

    Relative weights *within* each corpus are preserved (so retuning the MVP1/MVP2
    mix in SHEPHERD_CORPUS propagates here); only the split between the two
    corpuses is imposed. Weights are relative, so each side is scaled by an
    integer factor -- cross-multiplying by the other side's total makes the
    requested ratio exact regardless of how either corpus is weighted internally.
    """
    inferred = SHEPHERD_CORPUS if inferred is None else inferred
    pathfinder = PATHFINDER_CORPUS if pathfinder is None else pathfinder
    a, b = ratio
    inferred_total = sum(w for _, _, w in inferred)
    pathfinder_total = sum(w for _, _, w in pathfinder)
    inf_scale = a * pathfinder_total
    pf_scale = b * inferred_total
    g = math.gcd(inf_scale, pf_scale)          # keep the flattened list small
    inf_scale, pf_scale = inf_scale // g, pf_scale // g
    return ([(qt, bld, w * inf_scale) for qt, bld, w in inferred]
            + [(qt, bld, w * pf_scale) for qt, bld, w in pathfinder])


MIXED_CORPUS = _mixed_corpus()

CORPUS_BY_NAME = {
    "retriever": RETRIEVER_CORPUS,
    "shepherd": SHEPHERD_CORPUS,
    "ars": ARS_CORPUS,
    "pathfinder": PATHFINDER_CORPUS,
    "mixed": MIXED_CORPUS,
}


def corpus_for(name):
    """Return the (qtype, builder, weight) list for a component corpus name."""
    return CORPUS_BY_NAME[name]


# ----------------------------------------------------------------------------
# Result-count expectations.
#
# The pinned entity is read back out of the payload rather than carried
# alongside it: the builders stay single-return (payload in, payload out), there
# is no second channel to keep in sync, and the answer is derived from the exact
# bytes that were sent. A query with no single pinned entity -- a batch lookup,
# a Pathfinder query pinning two -- has no expectation, which is correct: the
# profile never measured one.
# ----------------------------------------------------------------------------
def pinned_entity(payload):
    """The one CURIE this query pins, or None if it pins none or several."""
    nodes = (((payload or {}).get("message") or {})
             .get("query_graph") or {}).get("nodes") or {}
    pinned = [ids for node in nodes.values()
              if isinstance(node, dict) and (ids := node.get("ids"))]
    if len(pinned) != 1 or len(pinned[0]) != 1:
        return None
    return pinned[0][0]


def baseline_for(qtype, payload):
    """The profiled baseline for this exact query, or None if there is none.

    Returns the `mvp_baselines.json` record -- `results` is the measured median
    answer count for the pinned entity, which is what a run compares against.
    None whenever the question wasn't profiled: an unmapped qtype (KP,
    Pathfinder), an unprofiled entity, or a query with no single pinned entity.
    """
    baseline_qtype = BASELINE_QTYPES.get(qtype)
    if baseline_qtype is None:
        return None
    entity = pinned_entity(payload)
    if entity is None:
        return None
    record = BASELINE_BY_ENTITY.get((baseline_qtype, entity))
    if not record or not record.get("results"):
        # No record, or a measured-empty entity: nothing to hold it to.
        return None
    return {"entity": entity, **record}
