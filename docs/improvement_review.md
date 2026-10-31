# Verified improvement review

Changes address reproduced search, comparison and ingestion failures. The
versioned evaluation baseline and judgments were not changed.

## Behavior changes

- Malformed/non-object JSON filters return HTTP 400 instead of disappearing.
- Unknown filter keys return a structured HTTP 400 instead of a server error.
  HTTP validation now uses the shared filter planner, including vendor routing
  and aliases; database connections are explicitly closed.
- Live search applies vendor, category, price, stock and specification filters
  through the same eligibility engine used for local search.
- Synchronous price/stock refresh reruns eligibility and retrieval before
  returning results. An offer that becomes too expensive or unavailable is
  removed, page counts are updated, and remaining eligible listings can fill it.
- Shopify continuation uses the page limit actually sent to the vendor, so a
  configured cap below 250 no longer makes a full page appear to be the last.
- Malformed Shopify catalog envelopes raise a fetch error and preserve a
  resumable error checkpoint, rather than reporting catalog completion.
- Saved equivalence labels cannot merge boards with conflicting pin counts.
- A query such as `ESP32 30 pins` accepts `ESP32 WiFi Bluetooth Development Board
  30 Pins` without requiring the words `ESP32 30` to be adjacent; the required
  pin count remains enforced.
- Explicitly different passive package sizes (such as 0402 and 0603) do not
  share a comparison group, including when a saved label says they are equal.
- Explicitly different continuous battery discharge ratings do not share a
  comparison group. Equal numeric ratings such as 10A and 10.0A still compare.
  This guard requires battery context and the words continuous discharge; it
  does not equate capacity, peak current and continuous current.

Two broken test fixtures were also repaired: dataset mutation tests now include
the required reviewed-label file, and the alias-conflict test imports the
current shared planner instead of a removed MCP private helper.

## Evidence

`tests/test_practical_regressions.py` adds 24 cases with temporary databases and
fake vendors. They exercise HTTP responses, persisted refreshed values,
multi-page ingestion and checkpoints, saved labels, and final comparison groups.
They include positive controls so rejecting invalid matches cannot pass merely
by rejecting every product.

The final focused run of that file plus `test_semantic_constraints.py`,
`test_adapters.py` and `test_price_stock_refresh.py`: **90 passed**.

The unchanged offline evaluator (`python scripts/evaluate_backend.py`) reports:

- 260 cases; baseline regression gate passes.
- 31 improved metric/check results, zero regressions.
- Failed checks: 50 before, 41 after.
- Critical failed checks: 25 before, 18 after.

The generated details are in `reports/backend-quality/latest.md` and
`latest.json`. These are controlled-fixture measurements, not production-wide
accuracy estimates. Absolute correctness still fails. Remaining debt includes
deep HTTP pagination and other matcher compatibility cases.

Broader testing is not completely green. Existing failures include expectations
for automatic identity-query correction/relaxation and older status/warning
names, bare numeric ranges interpreted as budgets, a matcher held-out recall
threshold, and an exact veto-reason expectation. Those assertions were not
weakened to claim a passing suite. The backend evaluator's own verification
tests, including deliberate regression injection, passed in the broader run.

The catalog database changes caused by legacy tests were restored; no catalog
mutation is part of this patch. No real vendor reliability or transport-load
claim follows from fake-vendor and offline checks.
