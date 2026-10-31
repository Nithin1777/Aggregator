# Backend quality evaluation

Run the repeatable, offline regression gate from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_backend.py
```

Results are written to `reports/backend-quality/latest.json` and `latest.md`.
The JSON retains per-case judgments, predictions, checks, metrics, source and
dataset fingerprints, package versions, and comparison deltas. Markdown is the
human-readable scorecard. Reports are ignored by Git; the reviewed baseline in
`evaluations/baseline.json` is versioned.
The scorecard includes baseline/current search metrics and their deltas for each
surface; per-query gates prevent an average improvement from hiding regressions.

To compare your working backend with a commit, using the same current evaluator
and judgments for both implementations:

```powershell
.\.venv\Scripts\python.exe scripts/compare_backend_revision.py --ref HEAD
```

This extracts the reference into a disposable directory. It does not reset the
working tree, install dependencies, contact vendors, or use the production DB.
The current Python environment must support both revisions. An incompatible
reference implementation is an evaluation error, never a passing result.

## What the gate means

There are two separate outcomes:

- **Absolute correctness:** every labeled check passes. The initial backend has
  known failures, which remain visible in the baseline and reports.
- **Baseline regression:** no additional failed checks, no newly violating
  entity/offer/pair inside an already failing check, no per-query metric decrease,
  no pagination coverage decrease, and no previously correct matching/grouping
  case becoming incorrect. Improvements on other queries cannot hide a loss.

The default command can pass the regression gate while absolute correctness
fails. Use `--strict` to fail on all remaining debt. Exit codes are **0** for a
passing comparison (or an explicitly requested baseline capture), **1** for a
quality regression/strict failure, and **2** for an incomplete or incompatible
evaluation. Failed evaluations replace stale output with a failure report.

## Metrics and denominators

- Search ranking uses **distinct product entities**, deduplicating vendor offers
  before scoring. HTTP ranking follows comparison-card order; MCP follows its
  serialized listing order. Rankings are evaluated to 50 distinct entities.
- **P@5** divides relevant results by five. A perfect query with only one relevant
  product can score at most 0.2. **Returned P@5** instead divides by the number of
  results actually returned, up to five. Both are reported to avoid ambiguity.
- **Recall@50** divides relevant entities retrieved in the first 50 positions by
  all relevant entities in the closed fixture. **F1@50** combines returned
  precision at that depth with recall. Grade >= 2 is relevant.
- **nDCG@10** uses gain `2^grade - 1` and log2 rank discount; **MRR** measures the
  first relevant entity within the measured ranking. Query scores receive equal
  weight. Surface and category breakdowns are also retained in JSON.
- Queries with no relevant answers have undefined positive-query recall/nDCG.
  They are measured through correct abstention. A request error cannot earn
  successful abstention credit; failed answerable queries receive zero scores.
- Matching reports confusion counts, precision, recall, and F1 separately for V2
  and the hybrid engine, and separately for historical dev/held-out/adversarial
  cases. Undefined scores are `null` / `n/a`, never invented perfect scores.
  Execution errors reduce decision coverage and correct-case rate and fail the
  gate; decision-only metrics retain their actual denominators.
- Final grouping scenarios measure pairwise equivalence and detect incompatible
  groups and dropped listings. Query checks separately detect incorrect groups,
  ineligible offers, and serialized price/stock/vendor violations.
- Paired query bootstrap intervals describe mean metric deltas with a fixed
  random seed. They do not establish production confidence or relax gates.
  Individual call latency is diagnostic and is not a quality/SLO gate.

## Collection and limitations

`evaluations/catalog.json` contains explicit synthetic engineering facts, entity
identities, and vendor offers. `search_cases.json` contains independently authored
query judgments, rationales, contract cases, and pagination checks.
`matching_scenarios.json` adds adversarial cached labels and final-group scenarios.
The existing `tests/data/matching_golden_set.json` supplies historical pair labels.
No expected answer is generated from the backend's current results or classifiers.
`real_matching_labels.json` adds 35 human-verified decisions exported from the
local label store. They are shown as a distinct production stress slice, not a
random held-out estimate; these pairs were selected for review and some may have
informed production labels. The hybrid evaluation clears its label cache before
each pair, so these are measured against the matcher fallback rather than
replaying their stored answer.
The fixture contains a 425-entity pagination population. MCP walks it exhaustively
across the ranking-window boundary. HTTP checks early pages and probes pages 8/9
to detect the 400-listing cutoff without repeatedly clustering every early page.
HTTP discovery and MCP component-identity pagination intentionally have different,
explicit expectations. Runtime suitability has no invented relevance judgment;
it is checked as an uncertainty-preservation contract.

Fifty explicit calibration listings preserve the original evaluation population.
Field discovery now includes single-observation keys rather than using that old
advertisement threshold. Their stored statistics are calculated from fixture rows;
search/planning/filtering functions are not patched. These calibration listings
are nonrelevant to labeled requests.

Each case restores the same isolated database. Live/refresh contract cases use
fake vendor adapters with explicit facts. The suite exercises ASGI parameter
validation and response serialization, plus registered MCP tool argument
validation/envelopes. It does not measure SSE/stdio transport load or real vendor
reliability. Tests guard against opening the production catalog or making external
socket connections.
Matcher mode and refresh enablement are controlled by the suite. Refresh TTL and
timeout use the evaluated revision's source defaults, ignoring local environment
overrides so developer shell settings cannot change the measuring conditions.

These scores measure this controlled regression collection. The historical
held-out pairs have already been used during development. Neither this small
collection nor its bootstrap intervals provide unbiased catalog-wide precision
or recall estimates. Domain-owner review of labels and a separately judged,
representative real-catalog collection remain important follow-up work.

The local catalog currently records 153 distinct search strings but only one
click event, so it cannot supply reliable search relevance labels by itself.
Prepare a blinded review queue from the actual search history and matcher gray
zone without treating the search engine's predictions as truth:

```powershell
.\.venv\Scripts\python.exe scripts/prepare_quality_review.py --db data/components.db
```

This reads the catalog in SQLite read-only mode and writes 153 observed search
queries with frequency counts and up to 20 candidate listings each, plus up to
500 numeric-variant near-duplicate matcher candidates (436 currently). Outputs go to the ignored
`reports/backend-quality-review/` directory. Search candidates are a judging
pool, not an exhaustive catalog judgment; unknown listings remain unknown and
must not be scored as irrelevant. Matcher rows are hard-negative *candidates*,
not labeled negatives: reviewers must mark `same`, `different`, or `unsure`, add
a rationale, and record reviewer/date before importing reviewed examples.

After reviewing the hard-negative JSONL in place, import completed rows into the
benchmark. The importer checks for a judgment, rationale, reviewer, and review
date and never modifies the live matcher database:

```powershell
.\.venv\Scripts\python.exe scripts/import_reviewed_hard_negatives.py
.\.venv\Scripts\python.exe scripts/evaluate_backend.py --write-baseline evaluations/baseline.json --reason "Add reviewed real-catalog matcher challenge labels"
```

The 153 real search strings remain a review queue until product relevance is
independently judged. The evaluator does not count them as search test cases
because relevant listings are unknown and one click event cannot define
relevance.

To build a representative search set, sample from observed query frequency but
stratify rare categories and query types so popular broad queries do not drown
out variant and constraint queries. Keep a blind test split grouped by query
intent; paraphrases and repeated searches for one intent must stay in the same
split. Pool listings from multiple retrieval methods/revisions before judging,
and track unjudged candidates separately. For matching, retain both difficult
false-positive candidates and hard positives (near-duplicate titles that are
actually equivalent), then split by underlying product family to avoid near-copy
leakage. Only reviewed labels should enter scored fixtures.

The existing label store is the source for the versioned matcher stress labels.
After additional human adjudication, refresh them deliberately:

```powershell
.\.venv\Scripts\python.exe scripts/sync_verified_match_labels.py --db data/components.db
```

Only rows marked `human_verified=1` are exported; the 705 unverified/LLM-only
rows are excluded. Review the JSON diff and rerun the evaluation before
accepting a larger label set.

## Adding cases and changing a baseline

Add explicit facts and labels with a rationale; include near misses, missing
evidence, and legitimate empty answers. Keep different packages, quantities, and
electrical variants as different entities. Use `allowed_listings` where different
offers for the same entity have different eligibility. Do not derive labels from
predicted outcomes or weaken them to make a failing backend pass.

Catalog, dataset, evaluator, metric-version, and configuration mismatches reject
a baseline comparison. Deliberate dataset/scorer changes require a new baseline:

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_backend.py --write-baseline evaluations/baseline.json --reason "Describe reviewed changes and remaining failures"
```

Inspect and commit the baseline diff after accepting an improvement so later
reverts are detected. Capturing a baseline records behavior; it does not approve
its correctness. PR CI also measures the PR base implementation under the same
current evaluator to detect regressions even if the committed baseline is stale
or has been replaced in the PR.

## Validation and CI

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_backend_evaluation.py -q
```

`.github/workflows/backend-quality.yml` runs evaluator tests, the versioned
baseline gate, and PR-base comparisons, and uploads JSON/Markdown reports even
when a gate fails. It requires no Git LFS catalog download.
Evaluator tests also inject a broken matcher in an isolated child process and
verify that the complete CLI gate rejects the resulting backend regression.
