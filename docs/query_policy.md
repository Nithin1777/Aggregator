# Identity-preserving query policy (2.4.0)

## Shared behavior

MCP search/count, local web search and post-filtering of live vendor search use
the same query plan and eligibility relation. Price sorting runs after eligibility.
Canonical listings receive ranking boosts only within that eligible population.
Local web counting and ranking use one prepared request on one read snapshot.

Required text terms remain conjoined. There is no automatic token dropping,
any-term fallback, or execution-error fallback to a broader LIKE query. Complete
token matching replaces general prefix matching. Letter/number separator variants
can be searched without equating different numbers (`LD06` and `LD016`).

The plan can contain identity requirements and engineering predicates together;
these are not mutually exclusive query classes.

## Identity and variants

- Recognize model-shaped tokens, catalog brand claims, curated identity names,
  and capitalized identifier-like words. Identity queries protect all their
  required name/modifier tokens rather than maintaining brand-specific fixes.
- Missing generic category words can be corrected against taxonomy vocabulary.
  Existing words and identifier/variant tokens are not automatically substituted.
  Possible identity corrections remain suggestions requiring a new request.
- Identity is checked against the sold item and its identifiers, separately from
  FTS ranking. A title referring to an identifier only as a supported product
  does not establish that the identifier is the item being sold.
- Explicit GB and wide/standard variants must have matching, unambiguous title
  evidence. A base-model MPN does not establish RAM, storage or lens variant.
- Cached merge labels cannot override conflicting or missing variant evidence.
  Multiple title options under one listing cannot establish an option's price.

This policy is conservative. Title parsing does not model every vendor option
selector or resolve intrinsic features versus included accessories perfectly.
It may abstain when the correct variant is only represented in an option menu or
outside the recognized sold-item title. It does not establish authenticity.

## Engineering requirements

Recognized units become typed predicates only when their property meaning is
clear. Examples include capacitance, resistance, inductance, motor RPM/torque,
battery capacity, and supply/rated voltage in supported category contexts.
Comparisons and ranges remain comparisons and ranges; repeated requirements are
ANDed rather than overwriting each other. Contextual package/dielectric codes use
declared normalized fields when available and otherwise remain required text.

Electrical unit equality alone is insufficient to infer an ambiguous property.
For example, an unspecified regulator voltage or battery current does not
silently become output voltage or continuous discharge current. Unsupported
mandatory engineering requirements return no confirmed matches and preserve
their evidence with a reason. The caller can resubmit a precise structured key.

Runtime requirements remain unresolved application requirements requiring load
and energy reasoning. Candidate retrieval does not verify runtime. Negative
requirements such as `without encoder` require absence evidence and remain
unresolved rather than becoming a positive encoder search.

Budget parsing checks unit boundaries. `under 20A` does not become a ₹20 budget,
and a model-number range does not become a price range. Bare price ranges require
an explicit currency marker; comparison phrases still accept an INR ceiling or
floor when the value has no engineering unit.

Text following `preferably`, `ideally`, `optionally`, or `preferred` is explicitly
optional and contributes a bounded ranking bonus. Unmarked terms are mandatory.

## Response contract

Responses expose `query_contract`, containing original/effective queries,
protected tokens, inferred requirements, unresolved requirements, corrections,
preferences, match tier and the policy version.

| `retrieval_status` | Meaning |
| --- | --- |
| `matches` | Listings satisfy the mandatory query predicates against catalog evidence |
| `corrected_matches` | Same, following a disclosed generic-category correction |
| `no_exact_match` | An identity request has no eligible match |
| `no_match` | No listing satisfies the required query |

`results_kind` is `confirmed_query_matches`. This describes agreement with the
query's catalog predicates, not independent verification of vendor claims.
Runtime results separately retain `requirements_verified=false`.

`alternatives` is a separate array and is empty unless an explicit alternative
retrieval policy supplies candidates. This release does not automatically run a
broader product search. Query suggestions are proposals, never confirmed listings.
Existing specification partial candidates remain separate and never count toward
strict totals or pagination. Corrected generic text is not labeled an exact MPN.

MCP cursors bind the original request, including its budget and identity terms.
Field-weighted FTS5 BM25 favors MPN/SKU and titles over vendor names; weights are
initial policy values, not measured optimal settings.

## Evaluation scope

The earlier bug-fix test results predate this policy. They must not be interpreted
as its validation. Syntax, static symbols, imports and diff checks are separate
from regression testing and relevance evaluation.

A representative judged dataset is still required to quantify Precision@5,
MRR/nDCG, identity violations, constraint violations and latency. Missing relevant
products must be included so that returning no exact match can be judged correct.
Synthetic identifier variants alone cannot establish catalog-wide precision.

Restart running MCP/web processes to load 2.4.0. No derived-index rebuild is
required for the read-time query and identity policy.
