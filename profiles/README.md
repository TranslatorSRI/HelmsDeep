# Corpus profiles

Per-CURIE profiling runs from [`helmsdeep-profile`](../README.md#profiling-the-corpus-helmsdeep-profile),
kept for provenance: `helmsdeep/mvp_baselines.json` — which supplies both the
inferred corpus's tiered entity pools and the expected result counts a run is
checked against — is derived from the roll-up here, and can be re-derived with
different cuts at any time.

| file | what it is |
|---|---|
| `mvp_profile_entities_2026-10-14.csv` | Every MVP1 disease (995) and MVP2 gene/chemical (20), profiled through a Shepherd ARA (`POST /query`, sync): median/min/max result count, response time, knowledge-graph size, and the suggested light/medium/heavy bin. |

## What this profile found

- **The tiers are real.** Split at the 33rd/67th percentiles of result count,
  the three MVP1 tiers have median response times of **68s / 35s / 11s**
  (329 / 337 / 243 diseases). Result count correlates 0.72 with latency, so
  answer-set size is a good cost proxy — which is the assumption the corpus was
  built on, now measured rather than assumed.
- **All six previously-curated "heavy" diseases are genuinely heavy** (758–1001
  results), so the curation was right — but 323 other diseases are too, and
  sampling from 329 instead of 6 stops the ramp warming the same caches.
- **86 diseases answer 0 results** and are excluded from sampling (`usable:
  false` in the baselines). They cost ~10s each and exercise nothing; on a
  target with `zero_result_is_failure` they would score as a failure every time
  they were drawn. Donepezil (`CHEBI:53289`) is the same story on the MVP2
  chemical side.
- **The same query is not deterministic.** Across the 20 entities profiled
  twice, repeats of the identical query against an idle service differed by up
  to **41%**, with one landing 20% below its own median. That is why
  `RESULT_SHORTFALL_TOLERANCE` defaults to 0.5 rather than something tight —
  see ["Did it answer with the right number of results?"](../README.md#did-it-answer-with-the-right-number-of-results).

## Re-binning or re-profiling

```bash
# Re-cut this profile without sending a single query, then adopt the result:
helmsdeep-profile --summarize-only profiles/mvp_profile_entities_2026-10-14.csv \
    --out /tmp/rebin --bin-cuts 0.25,0.75
cp /tmp/rebin_baselines.json helmsdeep/mvp_baselines.json

# Profile a different service (which CURIEs answer is a property of the
# service, not of the corpus), then adopt that:
helmsdeep-profile --host https://your-ara.example.org --out profiles/new
cp profiles/new_baselines.json helmsdeep/mvp_baselines.json
```

A stale baseline makes a healthy service look like it is dropping answers, so
re-profile before switching `--result-check` to `enforce`.
