# Changelog

All notable changes to `st-cli` (the `st` CLI and `st-mcp` MCP server) are
documented here. Format follows [Keep a Changelog](https://keepachangelog.com/);
this project aims for [Semantic Versioning](https://semver.org/).

## [Unreleased] · `mypy src/` passes, and CI now runs it

### Fixed: 91 strict-mode findings, none of them a behaviour change

`pyproject.toml` has said `strict = true` since the start and nothing ran mypy in
CI, so the count only ever grew: 41 tools in `mcp_server.py` were "untyped"
because the `_handle_errors` decorator they all wear had no annotations (it now
carries a `ParamSpec`, so each tool keeps its exact signature for FastMCP's
schema introspection); 24 functions promised `dict[str, Any]` while returning the
client's `Any` (now an explicit `cast` at each call site — the client keeps
returning `Any`, because it hands back whatever JSON ServiceTitan sent and
`feeds/reporting.py` checks the shape); 21 bare `dict` annotations are now
`dict[str, Any]`; two stale `# type: ignore`s are gone; three functions gained
annotations. No runtime path changed. `mypy src/` is a CI step from here on.

## [Unreleased] · Profit Wizard hosted parity: callback flags, sold estimates

### Added: 6 columns on `jobs`, 7 on `technicians`, and a new `sales.estimates` tab

Closes the remaining gap between Profit Wizard's hosted (Export Store) path and
its Direct ServiceTitan path: callback/recall detection, warranty jobs, booked
job totals, technician phone/business-unit/home-location, and sold estimates
were readable Direct but absent from every Sheet.

* **`jobs`** gains `recall_for_id`, `warranty_id`, `no_charge`, `total`,
  `business_unit_id`, `sold_by_id` — appended after the already-appended
  `completed_on`/`total_revenue`, so `jobs.v2`'s
  column list, grain and row key are untouched and the fixture regenerates with
  the existing rows byte-identical plus the new trailing cells.
* **`technicians`** gains `phone`, `business_unit_id`, `business_unit_name`,
  `role_ids`, `home_address`, `home_latitude`, `home_longitude` — same rule,
  `technicians.v1` unchanged. `business_unit_name` resolves against the SAME
  `settings/business-units` reference table `jobs.business_unit` already joins,
  so a business unit's name can never disagree between the two tabs.
* **New tab `sales.estimates`**, one row per estimate ITEM (an estimate with no
  items still writes one row with the `Item*` columns blank), fetched from the
  Sales & Estimates API's `estimates` list and windowed like the rest of the
  `financial` feed. It runs on the `financial` feed's six-hourly cadence and
  behaves exactly like `reporting.jobCosts` for permissions — absent, not empty,
  for a tenant that lacks the Estimates permission — but ships under its OWN
  contract version, `sales.v1`, because it is a wholly new tab rather than an
  appended column: adding a tab to an already-published version is refused by
  `scripts/gen_contract_fixtures.py`.
* Several of the new fields are unverified against a real tenant — see
  `KNOWN_UNVERIFIED.md`, "Profit Wizard hosted-parity columns".
  `jobs.recall_for_id` / `warranty_id` are the well-evidenced exception: Profit
  Wizard's own Direct path already reads these exact JPM job fields in
  production.
* `sales.estimates.SoldById` accepts ServiceTitan's bare-integer `soldBy` (the
  shape this repo's own `estimates-sell` docs use), not only a nested `{id}`.
  `Total` falls back to `subtotal + tax` when the response carries no `total`,
  and only when both parts are present — blank otherwise, never `0`.
* **No contract bump on `jobs` or `technicians`.** `sales.estimates` is a brand
  new tab under a brand new version, `sales.v1`; nothing published under
  `jobs.v2`, `technicians.v1`, `pricebook.v1` or `financial.v1` changed shape.

## [Unreleased] · CI lints all of src/ and tests/, not half of it

### Fixed: `src/st_cli/` and most of `tests/` were never linted

`ci.yml`'s lint step named `src/st_exporter tests/st_exporter` only. Everything
else — the whole of `src/st_cli/`, and every test outside `tests/st_exporter/` —
was checked by nothing. Two E501s duly merged into the release line in `aa0bfb3`
with no check going red, and a third plus an N802 had been sitting in
`tests/test_engine.py` unnoticed.

The exporter imports `st_cli` on every request it makes; there is no reading of
"shared code" under which that half deserves less scrutiny. The step is now
`ruff check src/ tests/` and `ruff format --check src/ tests/`, and the four
existing violations are fixed in the same commit so the widened scope lands
green.

## [Unreleased] · A long throttle no longer looks like a hang

### Added: the run log says when it is waiting, on which page, and for how long

Honouring ServiceTitan's stated `Retry-After` is what made `reporting.jobCosts`
reachable — and it made a successful run look broken. Run 35159471697 on
`BBTT-01/tr-doorservpro` wrote `reporting.jobCosts=1563` and ran from **22:51:34
to 22:58:44**: seven minutes during which the log emitted **nothing at all**. A
throttle and a hung job are the same thing to whoever is watching the Actions log,
and only one of them is worth cancelling.

Three lines, no behaviour change:

- **Each report page, before it is fetched** — report name, page number, rows
  accumulated so far — and again after it lands with the rows that page added and
  whether another page follows (which will be throttled by this one, since
  reporting counts each page as another run of the report).
- **Each rate-limit wait inside a report pull** — how long, which page, and how
  much of the 420s `_MAX_RATE_LIMIT_SECONDS_PER_REPORT` budget is now spent. It is
  emitted from the observer the pull already borrows from the client, so the chain
  to a caller that holds a real shared governor (the image pass) is untouched.
- **Each rate-limit sleep in `st_cli.client`**, which covers every feed rather
  than just reporting. Only waits of 5s or more say anything: the blind 1s/2s/4s
  curve is the ordinary noise of a busy endpoint and the image pass alone would
  earn hundreds of those. The line names the ServiceTitan resource and never the
  request's current url, which on an image fetch has been rebound to a presigned
  blob address whose query string is a credential.

`st_cli` had no logger before this. `configure_logging` now gives `st_cli` the
same level and handler it gives `st_exporter`, so an exporter run shows these
lines; a bare `st` CLI invocation configures no logging and is unchanged.

### Changed: the reusable export workflow caches its dependency install

`pip install -e .` costs ~15s of resolving and downloading on a cold runner, and
`export.yml` is not run once — every connector repo calls it on a five-minute
schedule, several jobs per cycle. `actions/setup-python` now caches pip's
download directory, keyed on this repo's own `pyproject.toml` (the self-checkout
puts it on disk before the cache step reads it, so it is the exporter's file and
not the caller's).

It caches downloads, not an environment: the install still runs and still
resolves, so which code a job runs does not move, and a cold or corrupt cache
costs the 15 seconds back rather than failing. `cache-dependency-path` is written
out rather than defaulted because setup-python's default hashes
`requirements.txt`, which this repo does not have, and a path that matches
nothing FAILS the job — on every connector at once. A test pins that the path
names a file this repo really ships.

## [Unreleased] · The pricebook tabs carry the whole payload

### Added: every scalar ServiceTitan returns on the pricebook item and category tabs

`pricebook.v1` emitted twelve hand-picked columns. `cost` and `hours` were sitting
in the same JSON response, unpicked, and Profit Wizard imported **35,138 items for
the pilot tenant that it could not price a single one of**. The same shape had
already cost three other round trips through an exporter change, a re-run and a
redeploy.

So the judgement about which fields matter moves to the consumer, where it is
cheap to change. The three item tabs go from 12 columns to **42**; the category tab
from 4 to **13**.

`cost` and `hours` are the two that prompted this and the two to check first:

| Column | Source | Present on |
|---|---|---|
| `cost` | `item.cost` | `equipment`, `materials` — **not `services`** |
| `hours` | `item.hours` | all three |

`Pricebook.V2.ServiceResponse` has no cost field of any spelling, so
`pricebook.services.cost` is blank on every row of every tenant by construction.

Also added to the item tabs: `member_price`, `add_on_price`, `add_on_member_price`,
`taxable`, `is_labor`, `is_inventory`, `deduct_as_job_cost`, `pays_commission`,
`commission_bonus`, `unit_of_measure`, `cross_sale_group`, `account`,
`cost_of_sale_account`, `asset_account`, the three warranty pairs
(`warranty_*`, `manufacturer_warranty_*`, `service_provider_warranty_*`),
`primary_vendor_id` / `_name` / `_part` / `_cost`, `other_vendor_ids` /
`other_vendor_names`, `source`, `external_id`. And to the category tab:
`description`, `image`, `position`, `category_type`, `business_unit_ids`,
`sku_image_refs`, `sku_video_refs`, `source`, `external_id`.

Every name is taken from the published Pricebook v2 OpenAPI document, not guessed.
Every value follows the tabs' existing cell rules: null or absent is a **blank
cell**, a real `0` is `"0"`, booleans are lowercase, every cell is text.

Flattening follows conventions these tabs already used — nested objects become
prefixed per-scalar columns, lists of objects become index-aligned CSV pairs like
`category_ids` / `category_names`, lists of scalars become one comma-separated cell
like `image_refs`. No cell is ever a JSON blob; a test asserts it.

**Deliberately not exported**, because a cell that cannot carry the fact honestly
is worse than no column: `externalData` (an arbitrary key/value bag any other
integration can write to — the one field here that could plausibly hold a token),
`serviceMaterials` / `serviceEquipment` / `equipmentMaterials` / `recommendations` /
`upgrades` (bills of materials and cross-sell links: `{skuId, quantity}` per entry,
where a CSV of ids would look like a usable BOM with every quantity silently
dropped — that needs its own tab at its own grain), and `subcategories` (a
recursive tree `parent_id` already carries, one row at a time).

**One column set across three tabs.** The item header is the UNION of the three
resources' fields, so one parser still reads all three and a column a resource does
not have is blank on every one of its rows. `blank_columns.ALL_BLANK_OK` now
distinguishes *structurally absent* (certain, from the spec) from *optional
upstream* (a guess about contractor behaviour); no money or hours column is
exempted on any tab that has the field, so a wrong spelling still trips the
whole-column-blank detector.

**Size.** Google Sheets caps a spreadsheet at 10,000,000 cells across all tabs. At
42 columns the pilot tenant's 35,138 items come to ~1.48M cells, up from ~0.42M —
about 15% of the cap, with headroom for roughly 238,000 item rows before the
pricebook tabs alone would reach it. Noted in `KNOWN_UNVERIFIED.md`; a six-figure
catalogue is now worth measuring rather than assuming.

### Changed: `pricebook.v1` → `pricebook.v2` (append-only, but a bump)

Every original column keeps its name, its meaning and its position; the tabs, the
grain and the row key are untouched. By the contract's own rules an appended column
is additive and does not force a bump.

It is a bump anyway, because a released fixture's bytes may never change and
appending a column changes every row of every `pricebook.v1` fixture.
`--republish pricebook.v1` is refused structurally the moment `columns` moves, and
relaxing that refusal would open exactly the hole the published register closes.
So `pricebook.v1` stays frozen on disk for consumers still pinned to it, and
`pricebook.v2` gets its own fixture directory.

**TrueQuote, TradeRated and Profit Wizard must each widen their supported
`pricebook` range to include `pricebook.v2`.** Until they do, their contract check
refuses the four tabs — loudly, which is the intended order of events, not a
regression. A reader that keeps `pricebook.v1` in its range and looks columns up by
name is otherwise unaffected: nothing it reads moved.
## [Unreleased] · A `0` in `total_revenue` meant "free work", and it shipped

### Fixed: a resolved `0` is now ABSENT, not a zero-dollar job

`jobs.total_revenue` landed reading `job.total` / `job.invoiceTotal` through
`_first_present`, which treats `0` as a value. The commit called the divergence
from Profit Wizard's `||` deliberate, on this contract's "blank is not zero"
rule. The live tenant disproved it. Measured across 1256 rows of
`tr-doorservpro`'s jobs tab: **no row was blank**, 45% of distinct jobs read
exactly `0`, and **41% of COMPLETED jobs reported `$0`**.

ServiceTitan does not send null here — it sends `0` for "no revenue recorded" —
so reading it literally labelled four completed jobs in ten as free work. Profit
Wizard's `(job.total || job.invoiceTotal)` makes the opposite choice, and `||`
rather than `??` is the point: a `0` falls through and the field is omitted when
nothing remains, which is why the direct baseline carries NULLs where hosted was
writing zeros. The two paths disagreed on ~400 jobs and hosted held the harmful
answer.

The asymmetry is what settles it. A blank makes Profit Wizard REFUSE to compute a
margin; a `0` makes it compute one against zero revenue, i.e. **-100%** — a
confident wrong number on a customer's screen rather than a gap. And
`blank_columns` cannot catch it: it fires only on a column empty on EVERY row, so
an all-zero column sails past the one tripwire built for this class of bug.

`_money_or_absent` is deliberately narrow and must stay so. A `0` cost and a `0`
price elsewhere in this export are real facts; "blank is not zero" still holds
everywhere it has not been overridden with live evidence.

Accepted cost, stated rather than hidden: a genuine zero-dollar job (a warranty
callback, a goodwill visit) is now indistinguishable from one with nothing
recorded. This field cannot tell them apart in the first place, the direct path
already makes that trade, and "I cannot tell you" beats "they worked for free".

## [Unreleased] · Wait as long as ServiceTitan asks

### Fixed: a 429 that says "try again in 50 seconds" was retried after 7

`reporting.jobCosts` could never have been written on a tenant whose Job Costing
Summary report is long enough to paginate — not on a manual dispatch, not on the
six-hourly schedule. Reporting allows roughly one run of the same report per
minute per tenant, and **each PAGE counts as another run**, so page 2 is always
throttled by page 1:

> `HTTP 429 {"status":429,"title":"Rate limit is exceeded. Try again in 50
> seconds."}` — page 2, 11 seconds into the report

The client's 429 backoff is `1s + 2s + 4s`: **seven seconds of total patience**
against a fifty-second ask. It failed identically on both runs of
35157864073 / 35158215902 (`BBTT-01/tr-doorservpro`), and would have failed the
same way for ever. The report was resolved correctly by then — the duplicate-name
work picked report 21639096 — so this was the last thing standing between the
tenant and a populated cost tab.

The server states the wait; the client now believes it. `retry_after_seconds`
reads the RFC 7231 `Retry-After` header (seconds **or** HTTP-date) and falls back
to ServiceTitan's problem BODY, which is where this API actually puts the number
and is why reading the header alone would have fixed nothing. With no stated wait
the old exponential curve is unchanged, so no other endpoint's behaviour moves.

Two ceilings, because "wait as long as you are told" is how a run gets SIGKILLed
by the runner with nothing written:

- `_MAX_RATE_LIMIT_WAIT` (90s) — the longest ONE request will park. A longer ask
  is refused as a rate-limit failure rather than slept through.
- `_MAX_RATE_LIMIT_SECONDS_PER_REPORT` (420s) — the total one report pull may
  spend throttled, summed across its pages, measured through the governor hook
  the client already calls. Past it the tab is skipped the way every other
  reporting failure is skipped: loudly, per-tab, non-fatally, with the other
  three financial tabs already written. The hook is borrowed and restored, so a
  caller that holds a real shared governor (the image pass) keeps it.

## [Unreleased] · The job-cost report refusal says which marker skipped a report

### Changed: a name-matching report skipped as CUSTOM now names its evidence

`reporting.jobCosts` is still absent on `tr-doorservpro`. The name half of that
was diagnosed and fixed in 0.2.14 (the tenant carries "Job Costing Summary
Report", not "Job Costing Summary") — but **no financial run has executed since**:
the last one was 18:50 UTC at exporter 0.2.13 and the fix landed at 18:59 in
0.2.14. The connector is pinned at `exporter-v0.2.19`, so the fix is deployed and
untried. The census that diagnosed it listed that name **twice**, which under
0.2.14+ resolves three ways: built-in + custom selects the built-in; two built-ins
refuse as ambiguous; two customs take the custom path.

Two of those three end in a refusal, and one was the least actionable message in
the module: "the only report(s) named X are custom reports". The fields that
judgement rests on (`isCustom`, `reportType`, ...) are guesses at a spelling never
seen on a real tenant. The asymmetry that makes guessing safe — false positive
costs a loud refusal, false negative costs silently wrong money — only holds while
the loud half is actionable, and that message was not: it points the contractor at
a report they can already see.

The refusal now quotes each skipped report by category id, report id, name and
**the marker it was judged on**, and says plainly that the spellings are
unconfirmed, so a false positive reads as one rather than as a missing report.
Selection is untouched: `custom_marker` is the same rule `_looks_custom` was, and
`_looks_custom` is now a wrapper on it.

## [Unreleased] · Billed revenue on the `jobs` tab

### Added: `jobs.total_revenue`, appended (NOT a contract bump)

`total_revenue` is 0 on all 1701 hosted jobs while the direct baseline carries it
on 446, so every margin and profitability surface in Profit Wizard is empty. PW
refuses to compute a margin from half an input rather than show a wrong one, so
the product is hollow rather than wrong — but hollow is most of what a product
called Profit Wizard is for.

Read from the job's `total`, falling back to `invoiceTotal` — **the direct path's
own expression**, `(job.total || job.invoiceTotal)`
(`profitwizard/lib/crm/servicetitan.ts:856`), which is the only code in PW that
writes the column. Using the same source in the same order is what makes hosted
numbers comparable to the baseline the QA sweep measures against; a different
source would produce a different figure for the same job.

One deliberate difference: PW's `||` lets a genuine `0` total fall through to
`invoiceTotal`. This treats `0` as a value, because a zero-dollar job is a real
fact and this contract is explicit that blank is not zero.

Appended last; the feed stays at `jobs.v2` for the same reason `completed_on` did.

**A Profit Wizard-side gap found while sourcing this, which this column does not
fix.** The hosted sync never writes `total_revenue` from any source.
`aggregateInvoiceLines` (`lib/hosted/sync.ts:173`) already parses `ItemTotal`
off the exported `accounting.invoices` tab — 2817 rows live — but uses it only to
detect negative price-modifier lines for `discount_total`, accumulates only
`itemTotalCost` into material/equipment, and returns no revenue field. So the
premise that there is "no revenue feed anywhere in the export" was not right:
billed revenue has been in the Sheet all along and is being discarded on read.
`KNOWN_UNVERIFIED.md` records it as the fallback source if `total`/`invoiceTotal`
turn out to be absent on the export change-feed.

## [Unreleased] · Two reports, one name: the job-cost tab can be unblocked

### Fixed: `reporting.jobCosts` on a tenant with duplicate report names

Run 35155266960 on `BBTT-01/tr-doorservpro` finally answered why this tab has
never been written, and it was none of the assumed causes. The Reporting
permission is fine and always was. The report NAME was wrong and was fixed in
0.2.14 — confirmed working today. The `require_columns` guard everyone assumed
was firing **has never run**. The actual cause:

> `2 distinct reports are named 'Job Costing Summary Report' (category
> operations/report 21131704, category operations/report 21639096). Refusing to
> choose between them.`

Both are in `operations`, neither carries a custom marker, and the exporter
refuses to guess. That refusal is correct and is unchanged. What is added is two
ways past it that are not guesses:

- **Elimination by declared columns.** When one name matches several reports,
  any candidate that does not declare `JOB_COST_COLUMNS` is dropped — it could
  not have produced the tab in any case, since `require_columns` would refuse it
  moments later. If exactly one survives, it is selected. This is elimination,
  not scoring: it removes non-viable candidates, it never prefers one viable
  candidate over another, and **two copies of one report both survive and still
  refuse**.
- **`EXPORTER_JOB_COST_REPORT_ID`**, a new workflow input and env setting. A
  human's recorded decision about which report carries the money. Honoured
  whatever the report is called and whether or not it looks custom — the guard
  exists to stop the CODE choosing, not to overrule the operator — but it does
  not skip the column checks, so a mistyped or stale id fails loudly rather than
  writing an empty tab. An id the tenant does not have is its own refusal and
  never falls back to name resolution, which could re-select the very report the
  pin was added to avoid.

Explicitly **not** added: any tie-break heuristic. Preferring the lower id, the
higher id, or the better column score always returns a winner, and a wrong winner
means a contractor's own report silently supplying their cost numbers — the same
reasoning that rejects Profit Wizard's column-scoring fallback.

Unset, everything behaves exactly as before.

**This does not by itself unblock `tr-doorservpro`.** If both of its reports
declare the frozen column set — likely, if one is a copy — somebody still has to
say which is genuine, either by deleting/renaming the duplicate in ServiceTitan
or by setting the pin.


## [Unreleased] · A real completion timestamp on the `jobs` tab

### Added: `jobs.completed_on`, appended (NOT a contract bump)

The `jobs` tab carried no completion timestamp at all, so Profit Wizard's
`jobs.completed_date` was null on **all 864** jobs whose `jobStatus` is completed
on the `tr-doorservpro` tenant. Everything that filters on that column therefore
read a working contractor as having done nothing: the technicians roster showed
named technicians a fabricated **0% close rate** and **$0** (close rate is
completed/total, counted off `completed_date`), and the Safety System reported
"no completed jobs in the last 90 days" while 864 sat inside the window.

`completed_on` is read from the job's own `completedOn` (falling back to
`completedOnUtc`). It is **not** synthesised from `appointment_end`: a consumer
can already make that fallback itself, and once a guess is written into the
column no consumer can tell it from a fact. An appointment that ended is not a
job that completed. Blank keeps meaning "not completed".

The column is **appended last and the `jobs` feed stays at `jobs.v2`** — additive
per `docs/export-contract.md`, so no consumer has to widen anything or deploy in
any particular order, and nothing goes dark in the meantime. Profit Wizard picks
the column up whenever it is ready to read it.

Whether `completedOn` is the right spelling on the EXPORT change-feed (as opposed
to the list endpoint, where it is evidenced) is not yet confirmed against a live
response — see `KNOWN_UNVERIFIED.md`. If it is wrong the next run says so by
itself: `completed_on` is not exempted in `blank_columns`, so a whole-column
blank raises a `BLANK COLUMN` warning and an Actions annotation.

### Changed: the fixture generator now recognises an APPENDED column

`docs/export-contract.md` has always said appending a column is additive and must
not bump the contract version. `scripts/gen_contract_fixtures.py` did not agree:
it refused every byte change to a released fixture alike, so the only route it
offered was the bump the rule forbids — and that bump is not a harmless
over-signal, because a consumer pinned to the old version must answer
`unsupported_contract` and **stop parsing the tab entirely** until it widens and
deploys. Appending `completed_on` was the first time the two rules met.

A pure append is now accepted, and `contracts.appended_columns` /
`contracts.additive_refusals` are the single definition of "pure" that both the
generator and the test suite ask. The released fixture is **left exactly as
published** — bytes, `published.json` sha and `manifest.json` row_count all
unmoved — so `check_register_append_only.py` is untouched and a consumer pinned
to that version keeps testing against the file it was released with. The
generator prints an `APPENDED COLUMN(S)` notice and writes nothing.

The acknowledged cost: an appended column has **no committed fixture** until the
next version bump. Needing fixture coverage for a new column is now the stated
reason to bump.

Everything else is refused exactly as before, each pinned by a new test: a
rename, a removal, a **re-order** (the check is positional, not set-based, so
`(a, b, c) -> (b, a, c, d)` is not an append), a changed cell in a released
column **including one riding along beside a legitimate append**, a changed row
count, and — the hole found and closed while building this — an append judged
against a released file whose sha no longer matches the register, which would
otherwise have laundered a tampered register into "additive, nothing to see
here". `--republish` now compares and writes at the released width for the same
reason, so an append can neither break the typo-fix hatch nor ride in on one.

## [Unreleased] · Conditional image fetches, and a per-run asset cap


## [Unreleased] · A one-off image backfill that finishes

### Fixed: the same picture was downloaded once per ITEM that used it

`upload_pricebook_images` iterated per item-asset, and the download is keyed by
URL. Run 35145303072 on `BBTT-01/tr-doorservpro` reported `fetched=5 uploaded=5`
— and items 2141068, 2141069, 2141070, 2141071 and 2141078 all named ONE asset
GUID. It fetched the same photograph five times.

The two halves are keyed differently and are now treated so: each distinct
`source_url` is fetched **once per run** and hashed once, and the bytes are
fanned out to every item that references it. The upload stays per item, because
TrueQuote's route takes one `external_item_id` per POST, hardcodes `is_primary`
and has no batch endpoint.

Ledger semantics are unchanged in shape. `asset_ref` is still
`{external_item_id}:{identity}` and `idempotency_key` still hashes the item, the
identity, the url and the payload — both per ITEM — so five items sharing one
picture still hold five rows, five keys and five `seen_keys` entries. That is
what keeps `ImageLedger.keep` honest: a member whose key never reached
`seen_keys` because somebody else made its download would be pruned out and
re-uploaded for ever.

A shared image is fetched **conditionally only when every member agrees** on the
stored validators. One member with none — because it has never been delivered —
means an unconditional GET, since a 304 carries no bytes and would starve it.

`fetched` now counts DOWNLOADS rather than item-assets, and so does
`image_max_assets`. The cap bounds the expensive half: run 35145303072's cap of
5 bought one picture; the same cap now buys five different ones.

### Added: bounded concurrency, and a REAL rate limiter behind it

The pass is ~100% network wait, so it now runs on a bounded pool
(`EXPORTER_IMAGE_CONCURRENCY` / `image_concurrency`, default 8, max 32). A
payload exists only inside a running task, so the worst-case resident set is
`concurrency x 8 MiB`; the dispatcher is held behind a semaphore so submitted
work cannot run away from completed work.

**The deadline, the cap and the least-recently-verified order are still decided
by one thread, in one loop, before a group is handed to a worker.** That is what
makes a bounded run deterministic when downloads finish out of order: which
assets it reaches is fixed by dispatch order, not by which response came back
first. A dispatcher stop ("start nothing more") is deliberately distinct from a
worker abort ("the receiver is refusing work") — a cap of 2 that paid for 2
downloads delivers 2.

There was no rate limiter in this codebase. What existed — and still exists,
unchanged — is a per-request retry backoff in `st_cli/client.py`. That is not a
governor: under concurrency N workers back off on their own clocks, wake
together and hit the endpoint again as a wave. `images/pacing.py` adds a shared
token bucket per service (`EXPORTER_IMAGE_REQUESTS_PER_SECOND`, default 6/s;
TrueQuote's side clamped to their documented 5,000/10min = 8.33/s), and the
reactive backoff now **cooperates** with it: a 429 anywhere penalises the shared
limiter, so every worker is held back rather than only the one that earned it.
`TokenManager` is now thread-safe, so eight workers meeting one expired token
issue one refresh rather than eight.

### Added: mid-pass ledger flushes, so a killed process resumes within a minute

The ledger is a Google Sheet tab and `flush` rewrites the whole grid, so it can
never be written per asset. It is now written on a timer (60s) from the
dispatcher thread while it holds the pass's lock, so every flush is a consistent
snapshot. A killed process loses at most a minute of delivered-upload knowledge
— bytes re-sent, never correctness — instead of the entire run.

### Added: progress, uploaded item ids, and the SIZE of what we upload

A multi-hour run prints a progress line every 30s (processed / total / uploaded
/ fetched / rate / ETA). A run with a cap set — a proving run, whose whole
purpose is "go and look at these" — now names every SUCCESS with its item id and
byte size, where it previously named only failures.

The summary reports `bytes[min/median/max/total]` over everything uploaded, plus
`item_assets`, `distinct_assets` and `dedupe=Nx`. Nobody has ever measured a
real pricebook asset from a live tenant; `MIN_PLAUSIBLE_IMAGE_BYTES`'s "real
assets run 2-4 KiB" is an assumption written alongside the floor, not a
measurement. These numbers settle it on the next run.

### Known, unfixed: the 1 KiB floor cannot catch a blank placeholder

Measured 2026-09-16 against ServiceTitan's web-app image proxy: a missing asset
requested with `?size=1200&default=Default%2F1.png` answers **200 image/webp,
2,798 bytes**, and the bytes are a completely blank white 1200x1200 image. It
clears the 1 KiB floor, and the placeholder's size scales with `size=`, so no
fixed byte threshold can work.

`assets.image_dimensions` now reads width and height from the file header
(PNG/JPEG/WebP, no decode, no dependency) and the pass reports compressed BYTES
PER PIXEL. The measured blank is 0.0019/px; a photograph is one to two orders of
magnitude denser. Uploads below `BLANK_DENSITY_BYTES_PER_PIXEL` are counted as
`images_suspected_blank` and **still uploaded**: no real asset has been measured,
so there is no evidence from which to set a rejection threshold, and a rule
guessed today could silently drop real images. The count is how that evidence
gets collected. Note the scope: we call the AUTHENTICATED
`pricebook/v2/tenant/{id}/images` endpoint and never that proxy, and we send no
`default=`, so it is NOT known that this body ever reaches us.

## [Previously unreleased] · Conditional image fetches, and a per-run asset cap

### Changed: the weekly full re-download of every image is now a conditional request

The image pass skips the download of any asset whose ITEM has a `modifiedOn` no
later than the moment the ledger last confirmed it. That timestamp is a proxy:
`modifiedOn` is on the item, not on the image, and **no live tenant has ever
been used to confirm that replacing an image moves it** (`KNOWN_UNVERIFIED.md`).
The insurance against that was `REVERIFY_AFTER_DAYS = 7` — a full re-download of
every asset every week, for ever, ~7,191 of them on one tenant.

The interval is unchanged; its **price** is not. A successful download now
stores the server's `ETag` / `Last-Modified` in `_image_ledger`, and the weekly
re-verification quotes them back as `If-None-Match` / `If-Modified-Since`. A
**304** is a verification that moved no bytes and sent no upload.

The decision order, cheapest first:

1. `modifiedOn` unchanged since the last verification — skip, **no request**.
2. Otherwise a conditional GET. **304** → verified, no bytes, no upload.
   **200** → new bytes, exactly as before.

**Nothing assumes ServiceTitan honours any of this.** A response with no
validator to store means the next check is a plain GET and a full download —
today's behaviour, unchanged — and a validator the server ignores comes back as
a 200, also unchanged. What the pass insists on is MEASURING which is happening:
`images_not_modified=`, `images_conditional_sent=`, `images_no_validator=` and
`images_signed_urls=` on the run's summary line, plus one `image conditional
requests: … -- <verdict>` log line per run that says in English whether 304s are
being returned, whether any validator is offered at all, and whether the asset
urls look signed (which would make the whole mechanism unusable and is not
fixable from this side). Read that line after the first live run.

`_image_ledger` gains two trailing columns, `etag` and `last_modified`. A
pre-upgrade four-column ledger loads unchanged — rows are padded, not rejected,
because discarding them would re-upload a whole catalogue on the upgrade run.

### Added: `EXPORTER_IMAGE_MAX_ASSETS` — a per-run cap on assets fetched

A second stopping condition beside the time budget, and a separate one: the
deadline bounds the CLOCK, this bounds the WORK. **The default is 0, meaning no
cap** — a number would silently truncate a large catalogue for ever on any
caller that never chose one, and a tenant permanently missing its last N images
looks exactly like a clean run. Exposed as the reusable workflow's
`image_max_assets` input.

Free `modifiedOn` skips do not count against it: the cap bounds fetches, so a
converged catalogue still sweeps end to end. Hitting it is announced as
`images_stopped=per-run asset cap reached`, deliberately distinguishable from
`images_stopped=time budget for the image pass spent` — the two ask for
different dials. Both flush the ledger, both veto the prune, and both compose
with least-recently-verified-first ordering, so successive capped runs sweep
disjoint slices and the union converges.

## [Unreleased] · The image upload is its own feed and its own job

### Fixed: the pricebook image pass could never finish, and failed the pricebook feed while it tried

Run `35130164187` on `BBTT-01/tr-doorservpro`, 2026-09-16: the image pass was
SIGKILLed at **10m35s** (`Terminate orphan process ... (st-export)`) with **0 of
~7,191 images uploaded**. GitHub reports a timed-out job as "cancelled", which
reads like a competing run; `concurrency.cancel-in-progress` is `false`, so it
was the timeout. `.github/workflows/export.yml` hardcoded `timeout-minutes: 10`.

**It could not converge on its own.** `_upload_one` checked `ledger.has(key)`
only AFTER downloading the bytes, because `idempotency_key(asset, payload)`
hashes the payload. So every re-run re-downloaded the whole catalogue to
rediscover what it had already sent, was killed in the same place, and — because
a killed process flushes no ledger — forgot even that. Meanwhile the pass lived
inside the `pricebook` feed's invocation, so its death reddened the hourly
pricebook export too.

Four changes, and it needs all four:

* **`images` is a feed of its own** (`--feeds images`, `run_images`), with its
  own caller job, its own cadence and its own concurrency lock. It writes no
  export tab and no `_meta` row — verified by test, because that is the reusable
  workflow's stated condition for leaving the shared export lock.
* **It re-lists the pricebook from ServiceTitan** rather than reading the
  exported tabs back. Reading them back is not possible against the frozen
  `pricebook.v1` contract: `image_refs` carries the asset's id when there is one
  and only otherwise its url, and the url is what gets downloaded. The
  duplicated listing is ~36 requests against a pass that may download thousands
  of images.
* **`job_timeout_minutes`** (default 10, so every existing caller and every
  other feed is untouched) drives `timeout-minutes` and is handed to the
  exporter as `EXPORTER_JOB_TIMEOUT_MINUTES`. The pass stops two minutes short
  of it and flushes its ledger, instead of being killed with nothing written.
* **The pass resumes.** It works least-recently-verified first, so each run
  starts on the images the last one did not reach; and it skips the DOWNLOAD
  entirely for an asset whose item ServiceTitan has not modified since the
  ledger confirmed it, so a converged catalogue costs no bytes at all.

`_image_ledger`'s fourth column is renamed `uploaded_at` -> `verified_at` in
place: same position, same data, an existing ledger loads unchanged. The summary
line gains `images_pending=` (the number that falls to 0 as a sweep converges)
and `images_revalidated=`.

**`--upload-images` is kept, not removed** — a caller passing
`--no-upload-images` keeps working — but it now applies to `--feeds images`
rather than `--feeds pricebook`. A connector that repins without adding the
`images-feed` job uploads no bytes from its pricebook job and says so at
WARNING; image identifiers still reach the Sheet either way.

## [Unreleased] · Customer phone and email were never exported

### Fixed: `customer_phone` / `customer_email` blank on every row ever exported

2,441 rows on one live tenant and 1,068 on the other, in both columns, for the
life of the feature — the exact failure `KNOWN_UNVERIFIED.md` predicted for this
field and the same shape as the `job_number` bug: a green run, a silent whole-blank
column, and nothing that reads as an error.

The exporter read contact details off the customer RECORD (`customer.phone`,
`phoneSettings[]`, `contacts[]`). ServiceTitan keeps them on a sub-resource —
`crm/v2/tenant/{id}/customers/{customerId}/contacts` — which TradeRated's own live
Direct-path function has been reading in production all along. The jobs feed now
fetches it for the customers behind the windowed rows and overlays the answer on
the two columns.

* **Same selection rule as the Direct path**, so a contractor cannot see a
  different number depending on which path served the row: by `type`, never by
  position, `MobilePhone` before `Phone`, `Email` for the email. **`Fax` is not a
  phone and is never a fallback** — a wrong number on a technician's screen is
  worse than a blank one.
* **The old readers stay.** The contract widens, never narrows: a tenant that does
  carry a flat `phone` still exports it, and the fallback is what fills the cells
  when the contacts call is refused. Precedence only ever decides between two
  POPULATED values — this can fill a blank cell and change a value, never empty one.
* **A 403 costs two cells, not the feed.** Same degradation as job types and
  business units: the `jobs` tab exports in full, the two cells fall back, and a
  `Customer contacts degraded` annotation says so on the run.
* **Neither column is exempted** from the blank-column detector. Silencing it
  would hide the next occurrence of exactly this bug.
* **No contract bump.** `jobs.v2`'s column list, grain and row key are untouched —
  what changed is which ServiceTitan field fills two existing cells. The committed
  fixtures regenerate byte-identically.

### Added: `EXPORTER_CONTACTS_ROUTE` / `EXPORTER_CONTACTS_MAX_CUSTOMERS`

The per-customer route is N+1, so it is deduped by `customerId` (many jobs share a
customer) and capped at 3,000 distinct customers per run — of the order of
1,000–1,500 requests for a tenant this size, inside the 600-per-10s budget.
Hitting the cap is announced, not silently truncated.

A bulk `crm/export/customers/contacts` change-feed would be strictly better and
**could not be shown to exist** without a live tenant (the registry declares the
crm export feeds as customers/locations/bookings; no documentation of a contacts
feed was found). So the route is a setting, not a guess:
`EXPORTER_CONTACTS_ROUTE=export` drains that feed cursor-tracked and cached like
the other five, and a 404/400 from it is announced and falls back to the
per-customer route for that run. Flipping it on once is how the question gets
answered.

### Added: Profit Wizard's `assign_technician` outbox write is implemented

One of the four ServiceTitan writes ticket 15 owns — the other three
(`push_estimate`, `update_job`, `push_prices`) still raise
`UnsupportedOutboxKindError` and remain that ticket's to build.
`perform_profitwizard_item` now resolves the job's target appointment (`jpm`
appointments-list, filtered by `jobId`), reads its current active technician
assignments (`dispatch` appointment-assignments, filtered by
`appointmentId`), and applies the intended crew via `dispatch`
appointment-assignments `assign-technicians` / `unassign-technicians` — the
same endpoints, body shapes and appointment-selection rule Profit Wizard's own
direct-CRM client (`setAppointmentTechnicianSet` in `lib/crm/servicetitan.ts`)
uses, so a Hosted company and a Direct one land on the same ServiceTitan state
for the same dispatch decision.

Unassignment is authorized, never inferred: a currently-assigned technician is
only ever removed when the item's `authorizedRemovalCrmIds` explicitly names
them. A technician a dispatcher added directly in ServiceTitan that Profit
Wizard simply hasn't synced yet is left alone. An intended-and-current set
that already matches makes no ServiceTitan write at all.

**Unverified:** the `unassign-technicians` request body (`{jobAppointmentId,
technicianIds}`) is carried over from Profit Wizard's own client, which itself
flags it as unconfirmed against a live ServiceTitan tenant (ServiceTitan's
developer-portal page for it renders client-side). `assign-technicians` uses
the identical shape and IS confirmed live.

## [0.2.10] — 2026-09-15 · A feed that fails is a run that fails

Three changes, all about the same thing: a feed that did not export must not end
green. 0.2.9's two fixes each caught an exception that used to end the run, and
between them they left one path where the exporter exported nothing and the
contractor saw a tick.

### Fixed: a jobs/technicians feed failure reds the run again

**This is the one to read before bumping.** A non-403 failure on the `jobs` or
`technicians` feed now exits **non-zero** — a red Actions run the contractor is
notified about — and its annotation is `::error` rather than `::warning`.

On 0.2.9 that failure raised out of `run_export` and the run was red by accident
of the crash. The cursor-loss fix below catches it, which is right, but the run
then exited 0: the only trace of a feed that exported nothing was a
`feed_failed=` token in the summary line, next to `jobs=0` and a green tick.

Reddening it costs nothing now, and that is precisely why it was not safe before.
The exception was tolerable only because the crash jumped over the `_meta` write;
`_meta` is now written inside the run before the CLI decides the exit code, and
the drain has already finished. No cursor is lost, no tab is lost, no outbox item
is redelivered.

**Pricebook and financial per-tab failures are unchanged and stay green.** One
tab of a four-tab catalogue feed left at last run's contents was a warning on
0.2.9, and reddening it would be a new regression the other way —
`pricebook.materials` is refused on every TrueQuote-only tenant, every run,
forever. Only `feed_failures` (jobs, technicians) decides the exit code.

### Fixed: the write-back no longer advises a `feeds:` change that would fix nothing

In a `--feeds jobs,outbox` run whose jobs feed ran and *failed* with a non-403,
the write-back reported the deferred case: "this run did not run the `jobs` feed…
the drain job's feeds must be `jobs,outbox`" — which is exactly what the run
already was. That path exists only because the guard now swallows non-403s. It
gets its own branch, `write_back_feed_failed=<n>`, and points at `feed_failed=`
instead. The writes themselves are unaffected: live in ServiceTitan, reported
succeeded, exported by the next successful jobs run.

### Noted: a Google Sheets outage still ends the run

`SheetsClient` does not wrap gspread, so `gspread.exceptions.APIError` is not an
`STCLIError` and the per-feed guard does not catch it — a Sheets 429 on the jobs
tab write ends the run with the technicians feed unattempted. Unchanged from
0.2.9, and left that way deliberately: wrapping it would widen every
`except STCLIError` at once, including the per-tab guard whose failures are
warnings, so a whole-store outage would file itself as four stale tabs behind a
green run. The cursor still trails the data either way. Pinned by test now rather
than asserted away.

### Fixed: a failing feed no longer discards a committed feed's cursor

`_meta` carries the cursors and is written once, after every feed has run. The
`jobs` feed commits five raw-cache grids and the `jobs` tab well before that, so
anything throwing in between — an unguarded `fetch_technicians` returning 403 on
a missing Settings → Technicians permission, or 400 on the unverified `active=Any`
parameter — left the tab freshly written and the cursors exactly where they were.
Every later run then re-drained every change feed from the beginning, forever, and
nothing said so: it presented as a slow exporter rather than as an error. This was
live for two contractors.

- `jobs` and `technicians` now run behind the same per-feed guard the pricebook
  and financial tabs already had. A failing feed is not written, its previous
  `_meta` row (cursor included) is carried forward unchanged, and the feeds that
  already succeeded keep theirs.
- Each feed appends its `_meta` row **after** its tab is on disk, and the guard
  discards any row a feed appended before it threw. The cursor is always the
  trailing edge: a re-drain is slow but correct, whereas a cursor that led the
  data would skip a window of changes permanently.
- A feed that fails now emits a GitHub Actions annotation and a step-summary line
  naming the **consequence** ("its cursor did NOT advance… every run re-drains"),
  the same channel the blank-column detector uses, and the run's summary line
  gains `feed_failed=…`.
- `fetch_job_types` / `fetch_business_units` degrade to an empty lookup instead of
  killing the whole jobs feed — `denormalize` uses both only as a fallback.
- `fetch_technicians` retries without `active=Any` on a 400 (and only a 400),
  announcing that the tab may be active-only. The parameter itself is still
  unverified; see `KNOWN_UNVERIFIED.md`.
- The guard and the per-tab **scope** classification added in 0.2.9 are one path,
  not two. A **403** on `jobs` or `technicians` still goes to `ScopeLedger` — a
  tab never granted is skipped quietly, one whose permission was revoked is loud
  and reds the run — and every other `STCLIError` is the feed failure above. The
  guard rolls back any `_meta` row the feed had already recorded *before* either
  door carries the previous row forward, because `MetaRowSet.carry` is a
  `setdefault` and a half-written fresh row would otherwise silently beat it.
- Consequently a 400 or a 401 on `jobs` no longer ends the run by exception: it
  is guarded, named in `feed_failures`, annotated, echoed as `feed_failed=jobs`,
  and — the whole point — the single `_meta` write is reached. It is still never
  filed as a scope answer; only a 403 is.
### The exporter writes back what it just wrote

When the outbox drain performs a technician assignment against ServiceTitan, the
same run now updates the affected rows in the `jobs` tab instead of waiting for
the next jobs run to rediscover its own write. That removes the middle leg of the
Hosted round trip — worst case falls from roughly 25 minutes to roughly 12.

It applies when `jobs` and `outbox` are named in the **same** invocation
(`--feeds jobs,outbox`). A drain-only run (`--feeds outbox`) logs
`write_back_deferred=<n>` and changes nothing: it makes no Export Store
round-trip at all, which is the stated justification for its concurrency lock
being separate from the export one, and writing an export tab from it would let a
drain replace the `jobs` tab while an export run held the other lock. See
`docs/examples/connector-export.yml` for the one-line opt-in and what it trades.

Bounds, all of them deliberate:

- The write-back goes through the same `build_job_grid` + full-tab `replace_grid`
  path the feed uses, from the feed's own denormalised rows. There is no second
  row-builder, so a written-back row is identical to the row the next feed run
  will produce for it — pinned against the committed `jobs.v2` fixture.
- **No cursor moves and `_meta` is untouched.** `_meta` is still written exactly
  once per run, before every side lane. A write-back fetched nothing, so it may
  not restate `last_cursor` or `last_run_at`; its `row_count` can be out by the
  rows the write-back changed until the next jobs run.
- **A failed write-back is not a failed item.** It runs after the drain, so every
  item it knows about has already been performed, ledgered and reported
  succeeded; a Sheets failure here is logged (`write_back_failed=1`), the run
  stays green, and the next jobs run corrects the tab. Reporting the item failed
  would make the app redeliver it and perform a second real ServiceTitan write.
- **A scope-denied `jobs` tab is never written by the write-back.** If
  ServiceTitan refuses this tenant the `jobs` feed (0.2.9's per-tab 403
  classification), the run logs `write_back_scope_denied=<n>` and writes nothing.
  A "never granted" tab stays absent — a quiet skip means *no tab*, and the
  write-back must not resurrect one out of two ids — and a "revoked" tab stays
  frozen at the last good run, which is the evidence the classification rests on.
  The handle is built only on the one door out of the per-feed guard that means
  "this run's rows are on disk" — a non-None outcome — so a denied or otherwise
  failed `jobs` feed cannot produce one.
- An appointment with no row in this run's output (outside the window, or its job
  not in the raw cache yet) is left for the next feed run rather than invented.

Not done, deliberately: on-demand workflow dispatch. It needs the GitHub Actions
permission removed on 2026-09-10, and permissions are cumulative, so dispatch
cannot be had without run-log read. One exporter cycle is the floor.

## [0.2.9] · Every feed declares a contract version, and the fixtures prove it

Released as **0.2.9**, chosen by the owner over 0.3.0. Note for anyone bumping a
connector: despite the patch-level number this is **not** a drop-in. The reusable
workflow gains inputs and secrets, the `pricebook` input is **removed**, the
outbox drain now fires on `feeds` naming `outbox` rather than on secrets being
present, and the concurrency key changes shape. A caller workflow and this tag
must move together.


The shared reader package is cancelled: TradeRated, TrueQuote and Profit Wizard
each keep their own copy of the Sheet-reading code. That is safe only with this
in place, because **a shared package never prevented drift — fixtures do**. Apps
pin different versions anyway; connector repos in this org already sit eight
releases apart.

### A contract version on every feed

`_meta.contract_version` was blank for `jobs` and `technicians`. It no longer is:

| Feed | Version |
|---|---|
| `jobs` | `jobs.v2` |
| `technicians` | `technicians.v1` |
| `pricebook` | `pricebook.v1` (unchanged) |
| `financial` | `financial.v1` (unchanged) |

**`jobs` is v2, not v1.** 0.2.7 changed that tab from one row per appointment to
one row per assigned technician. The column set did not move, so nothing looked
breaking — but `st_appointment_id` stopped being unique and a consumer broke in
production, silently. Naming today's shape "v1" would give one name to two tab
shapes; every Sheet written by 0.2.8 or older still carries the v1 shape under a
blank version. A blank version is now explicitly its own case for consumers: not
"unrecognised", and for `jobs` not even decidable between the two shapes.

`src/st_exporter/contracts.py` is the one place a tab's columns, grain, row key
and version are declared, so they can no longer be edited in two files and
disagree.

### A committed fixture suite — the actual guard

`contracts/fixtures/<contract_version>/<tab>.json`, plus a `manifest.json` of
versions and sha256s. Language-neutral JSON because three of the four codebases
are TypeScript. One directory per contract version, so `jobs.v3` landing does not
strand a consumer still pinned to `jobs.v2`.

Each file pins the header row and representative data rows, chosen to cover the
cells that have already gone wrong: null price beside a real `0`, an absent
`active` beside explicit `true`/`false`, index-aligned `category_ids`/
`category_names`, a deduped `image_refs`, a cancelled timesheet segment, a
top-level category with a blank `parent_id`, a multi-technician appointment whose
two rows share one `st_appointment_id`, and `job_number` populated from
`jobNumber`.

The exporter's tests assert it PRODUCES those bytes; each consuming app asserts it
READS them. Rename a column without bumping the version and `pytest` goes red with
a message naming the tab, the column, and the only two ways out — written for
someone who did not write this code.

**No real customer data.** This repo is public; every fixture is synthetic. The
suite rejects any email whose DOMAIN (anchored at the `@`) is not one of the four
reserved ones, any run of 7+ digits that is not `555-01xx`, and — the part no
regex can do — any cell that does not trace back to a literal in
`tests/st_exporter/fixtures/`. Names, addresses and prices have no recognisable
shape, so what is checked for them is provenance: a recorded response cannot
reach the committed suite without being hand-transcribed into the synthetic
source records first. The full scrubbing rule for any future recording from a
live tenant is in the contract doc.

### A published version is frozen — `contracts/fixtures/published.json`

Every check above compares the fixtures to what the code produces **today**,
which is a check that regenerating always satisfies. Rename a column, run
`scripts/gen_contract_fixtures.py`, and the suite went green again with `jobs.v2`
still stamped on a tab no consumer's reader can find a column in — every
consumer's version check passing, every consumer reading the old name, zero rows,
four codebases, no error anywhere.

`published.json` records the sha256 of every fixture **as released**. It is the
only file in the suite not derived from the current code:

- the test suite asserts every file under a released version still hashes to it,
  and fails with `jobs.v2 is published — bump to jobs.v3`;
- the generator REFUSES to write a change to a released version (exit 2) and
  names the version to bump. `--republish <version>` exists for a genuine typo in
  a released fixture and needs a CHANGELOG line naming it;
- deleting the register does not rebuild it from today's code: that was the
  one-`rm` bypass, and it is refused too;
- deleting ONE version's key is refused the same way — a version directory that
  already exists on disk is not a brand-new version, and treating it as one
  re-baselined exactly that version on the next run;
- REMOVING a tab from a released version is refused, not just adding one. The
  register is now checked in both directions: a file registered under a version
  the code still writes, that the code no longer produces, is a violation.
  Removing a tab used to regenerate cleanly at the same version — the manifest
  dropped it, the fixture and its register entry stayed on disk, and a consumer
  kept testing itself against a tab the exporter had stopped writing;
- `--republish <version>` is now **structurally** typo-only. The released file
  and the new payload are parsed and compared: if `columns`, `row_key`, `grain`,
  the set of tabs or the number of rows would move, it is refused however the
  CHANGELOG is worded. Only cell text may change. The CHANGELOG line remains as
  the audit trail a consumer can find; it was never a gate, because free text
  copied out of the refusal message cannot tell a transposed digit from a
  renamed column;
- CI adds the trust anchor the branch cannot provide for itself:
  `scripts/check_register_append_only.py` diffs `published.json` against the base
  branch and fails if any already-published `(version, file)` sha changed or
  disappeared. Every other guard is judged by a file living in the branch under
  review; this one is judged by `main`.

**`contracts/fixtures/jobs.v2/jobs.json` changed bytes in this release**, before
any tag carried it: the fixture's customer phone numbers were moved into the
reserved `555-01xx` block during the scrub. No consumer can have fetched it — the
suite post-dates `exporter-v0.2.8` and no `exporter-v0.2.9` tag exists — so this
is not a republish and needs no version bump. It is recorded here anyway, because
the rule this mechanism enforces is that a change to a released file leaves a
trace a consumer can find, and a mechanism that exempts itself from its own rule
is not one anybody should trust.

Failure messages now lead with **bump the version**, and present regeneration as
what you do afterwards. Leading with "regenerate" was pointing at the bypass.

`row_key` is also asserted against the rows themselves — every tab's committed
rows must be unique under it, and the `jobs` fixture must contain at least one
appointment carrying two technicians. Comparing the fixture's `row_key` string to
`contracts.py` only proved the fixture was generated from `contracts.py`. This is
what would have caught 0.2.7, which renamed nothing and changed only uniqueness.

### CI, at last

`.github/workflows/ci.yml` runs `pytest`, `gen_contract_fixtures.py --check` and
ruff on every pull request. Until now `.github/workflows/` held only `export.yml`
— a reusable workflow contractors call, which never runs on a push here — so every
guard in this repo ran only when somebody remembered to type `pytest`. CI is
`pull_request`/`push` only, declares no `workflow_call`, takes no secrets and
never runs `st-export`, so it cannot interfere with a customer's export.

### Fixed — the example caller pinned a tag that does not exist

`docs/examples/connector-export.yml` — the file whose own header calls it the
SOURCE OF TRUTH, and which every connector repository copies — had all five
`uses:` lines on `@exporter-v0.3.0`. Tags stop at `exporter-v0.2.8` and this
release is 0.2.9, so a contractor copying it got a workflow GitHub cannot
resolve: every feed and the drain stop, and the only symptom is runs that do not
happen. Now pinned to `exporter-v0.2.9`, asserted equal to `export.yml`'s
`EXPORTER_TAG` by a test (the old one only checked the five agreed with each
other), and rewritten by `scripts/release.sh`, whose comment claimed "exactly
two" version literals while this was a third it never touched. The header table's
`financial-feed daily` is also corrected to six-hourly, and a test now pins that
table to the crons.

### Documentation

- `jobs.v1` has **no fixture** and cannot have one — the code that produced that
  grain was replaced in 0.2.7, before this suite existed. The contract doc said
  old version directories stay "so a consumer still pinned keeps a fixture" and
  named `jobs.v1`; it now says plainly that a consumer on a Sheet last written by
  0.2.6 or older has prose and nothing else.
- `denormalize._contact_detail` claimed inserting the `contacts[]` layer meant "no
  shape that resolved a value before resolves a different one now". False:
  `{"contacts": [{"type": "Phone", "value": "A"}], "phone": "B"}` gave `B` and now
  gives `A`. Deliberate — `contacts[]` is the documented shape — but a changed
  value, not a filled blank. Both that docstring and the test that repeated the
  claim are corrected.
- `_resolve` and the parity test enumerated "three deliberate differences" from
  `_contact_detail`. There are four: `{"phoneSettings": [{"phone": ""}]}` is `''`
  to one and `None` to the other. Pinned by a test.

### For the three consuming apps

`docs/export-contract.md` is the stand-alone document they work from: the version
per feed, what forces a bump, how to fetch the fixtures at a pinned tag in CI
without vendoring a copy that can itself drift, and what to do on an unknown
version — stop with a named `unsupported_contract` error, never parse
optimistically, never return empty. An empty result is indistinguishable from a
quiet day, which is the whole failure this defends against.

## [Unreleased] · Three outbox lanes, one drain

The exporter drained one queue. It now drains the queue of every product the
contractor bought — TradeRated, TrueQuote and Profit Wizard — in one run, with
each lane isolated from the others and from the export feeds.

### `outbox` is now a feed, and that is what stops a double drain

`st-export --feeds outbox` drains. **Nothing else does.** Before this, the
exporter drained on any invocation whose outbox secrets happened to be set, and
the only thing keeping the `technicians-feed` job from draining the same queue a
second time every cycle was an explanatory comment in the caller workflow — a
comment that had already been stripped from one live connector repo. With three
queues that trap was about to get three times worse.

The capability is now keyed on an explicit request rather than on the incidental
presence of a secret, so handing the secrets to a second job is **inert**. The
concurrency group is the second guarantee: every drain names the same feeds
string, so every drain takes the same lock and two of them could never run at
once even if someone did add `feeds: outbox` to a second job. And the caller
workflow no longer hands any feed job a product **machine token**, so a second
job that asked for `outbox` would build no lane and drain nothing — three
independent things now have to be true at once, where there used to be a comment.

**This is a required migration for existing connectors.** A caller that bumps to
this version without adding `outbox` to exactly one job stops draining. That
cannot be silent, so every run carrying a product's secrets without the `outbox`
feed logs a WARNING naming that product. `--feeds outbox` on its own skips the
export half entirely, so a dedicated drain job costs no Sheets round-trip.

### One drain job, four feed jobs, and the lock split in two

The caller workflow every connector runs is now kept here, at
`docs/examples/connector-export.yml`, and the suite asserts its shape: which
jobs a contractor runs per product bought, the cadences, and that exactly one
job drains. It used to be reviewed by eye, once per connector repository, which
is how the one-drain comment came to be deleted from a live one.

**Every feed job runs on its schedule, for every contractor**, and nothing in
the YAML says which feeds a connector exports. There are no per-feed repository
variables — see "ServiceTitan's scopes decide which feeds run" below. The drain
is not a feed and is not scope-gated: it runs iff `feeds` names `outbox`.

The reusable workflow's concurrency group is now **two** groups per connector
repository rather than one, split by what a run WRITES rather than by which job
asked for it. Every run with an export feed keeps the shared `…-export` lock,
because they all read and rewrite the whole `_meta` grid; a run asking for
`outbox` and nothing else takes `…-outbox`, because it never touches `_meta` at
all. That is what takes the 5-minute drain off the back of the hourly pricebook
run. It is not a per-FEED key: per-feed locks would let two feeds rewrite
`_meta` over each other, and the prerequisite for them is a merge-on-write
`_meta` in Python, not a change to the YAML.

### ServiceTitan's scopes decide which feeds run — no per-feed variables

The four repository variables that gated the feed jobs (`JOBS_FEED`,
`TECHNICIANS_FEED`, `PRICEBOOK_FEED`, `FINANCIAL_FEED`) are **deleted**. They
duplicated the ServiceTitan scopes: the boxes a contractor ticks when they create
the app already state what they bought, and a hand-maintained second copy of one
fact in a different place drifts —

* variable **on**, scope **missing** → a red run every hour, forever;
* variable **off**, scope **granted** → a feed they paid for silently not
  running, on green runs. That is the same silent-stop failure the one-drain rule
  exists to prevent, and it also made applying a caller patch order-dependent: set
  the variables first or a live contractor's export stops with no symptom.

The defence of the variables was that a 403 cannot tell "never bought" from
"permission revoked". **`_meta` answers that**, because it already records the
last successful run of every tab (`src/st_exporter/scopes.py`):

**The unit is the output TAB, not the feed.** ServiceTitan grants per *entity*:
`permissions.md`, the team's own tick-box runbook, gives a TrueQuote contractor
Pricebook Services, Equipment, Categories and Images and deliberately **not**
Materials (that arrives with Profit Wizard), and Reporting is a section of its
own. So "one tab of a feed is refused while its siblings answer 200" is not an
edge case — it is the ordinary, every-cycle state of a TrueQuote-only tenant and
of a Profit Wizard tenant without Reporting. A 403 therefore rules out exactly the
tab that earned it; the feed's remaining tabs are still attempted, and each is
judged on the evidence for itself.

| What the exporter sees | What it means | What it does |
|---|---|---|
| 403, and nothing has ever evidenced a run of this TAB | the entity was never granted | skip **quietly**: that tab is not written, its siblings are, `not_granted=<tab>` in the run's summary line, run stays green |
| 403, and a `_meta` row names a past run of this TAB (or the tab still exists) | the permission was **revoked** | `::error` annotation naming the tab and the permission, that tab keeps its last good contents, `scope_revoked=<tab>` in the summary, **run exits non-zero** |

Either way that tab's previous `_meta` row is carried forward unchanged, exactly
as `_TabGuard` does for a failed tab — that evidence is what makes the *next*
403 decidable, so it is never deleted. It is never carried over a row this run
wrote fresh: `_meta` holds exactly one row per tab by construction
(`meta.MetaRowSet`), and `build_meta_grid` refuses a grid that would hold two,
because `_meta` is parsed last-wins and a duplicate reads not as an error but as
the wrong `last_run_at` — the very cell `docs/export-contract.md` tells consumers
to trust for freshness.

The permission strings the annotation names are per tab and match what the code
actually calls: the `jobs` string now names **Settings → Business Units** and
**JPM → Job Types**, because a jobs-feed 403 is as likely to come from those two
reference lookups as from the five exports; `technicians` names Settings →
Technicians and nothing else, because `fetch_technicians` calls nothing else.

The pricebook **image pass** keys on `catalogue_complete` — "did every item tab
produce a grid" — and not on any permission verdict. A TrueQuote-only tenant is
refused Materials on every run and must still receive its services and equipment
images; the only thing a missing item tab may veto is the ledger *prune*, and
`catalogue_complete` carries exactly that.

**Only HTTP 403 takes this path.** A 400 (`KNOWN_UNVERIFIED.md` records one on
`active=Any`), a 401, a 404, a 429 or a transport error behaves exactly as before:
the per-tab guard fails that tab loudly, or the exception ends the run. Treating
any of them as "not bought" would turn an outage into a silent skip.

On a **first-ever run** no tab has a `_meta` row and no tab exists, so nothing is declared revoked
and a 403 is "never granted" — which is what it is. `PRICEBOOK_CATEGORY_IDS`
stays: it narrows *what* the pricebook feed exports and never decides *whether* it
runs.

### Three apps, three path shapes, three scope vocabularies — none of it shared

    TradeRated     GET|POST {base}/crm-outbox     POST {base}/crm-outbox/{id}/result
    TrueQuote      POST     {base}/booking/claim  POST {base}/booking/result
    Profit Wizard  POST     {base}/claim          POST {base}/result

All three are correct. TradeRated's base is a Supabase Functions origin where
the whole edge function is one route and the id is a path segment; the other two
are Next.js route handlers under `https://<host>/api/outbox`, and TrueQuote's
booking queue sits a level deeper because its base already carries
`/pricebook-image`. Paths are per-lane configuration (`routes.py`), overridable
by `{PREFIX}_OUTBOX_CLAIM_PATH` / `_RESULT_PATH` without an exporter release.

The result vocabularies are per-lane too, and deliberately so: TradeRated settles
a credit hold on `{"status": "succeeded", "st_id": ...}`; TrueQuote attaches a
booking to a lead on `{"status": "succeeded", "booking_id": ...}` and does not
read `st_id` at all. One shared shape would have meant one app silently losing
the id of the thing the exporter had just created for it.

Every accepted vocabulary only widens. `TRADERATED_OUTBOX_BASE_URL` still works
alongside the ticket's `TRADERATED_OUTBOX_URL`, and `TRADERATED_IMAGE_TOKEN`
alongside `TRUEQUOTE_IMAGE_TOKEN`.

### The image lane was pointed at the wrong host

`{TRADERATED_OUTBOX_BASE_URL}/pricebook-image` could only ever have worked for a
contractor who had set TradeRated's secret to TrueQuote's host: the route is
TrueQuote's, under TrueQuote's base. It now reads `TRUEQUOTE_OUTBOX_URL` /
`TRUEQUOTE_IMAGE_TOKEN` first and falls back to the old names.

### `technician_rating` writes, at last

It used to raise `UnsupportedOutboxKindError` on the claim that no ServiceTitan
rating endpoint existed. That was false when it was written: the registry has
shipped `customer-interactions technician-ratings` with create since 2026-06-01,
and every contractor grants the scope for it at setup. So every rating on a
Hosted company was reported failed against a permission already given.

The payload is mapped rather than forwarded — string ids to integers, and the
1-5 star rating doubled to ServiceTitan's 0-10 scale. Forwarding it unmapped
would have posted every five-star review as 5/10: a wrong number that looks
right.

### Fixed — one unwritable ledger used to cost one unledgered write PER LANE

`drain_lanes` hands the SAME `OutboxLedger` to each lane in turn, but "the ledger
stopped accepting rows" was a local variable inside `drain_outbox`. So it died
with that call: lane 2 performed one real ServiceTitan write before hitting the
identical failing flush, and so did lane 3 — three lanes, two extra unledgered
writes, each one a booking or a lead that duplicates when the app redelivers it.
The state now lives on the ledger (`OutboxLedger.unwritable`), so the first
failed flush stops every remaining lane. Those lanes claim nothing, perform
nothing and report nothing: the app's lease expires and it redelivers, which
neither duplicates a write nor loses one.

### Blank-column warnings are visible in a green Actions run

The detector logged a warning, and a warning in the log of a **successful** run is
invisible — which is exactly how the 2431-row `job_number` bug lasted the life of
the feature. Under Actions the same message is now also a `::warning` annotation
on the run and a line in the step summary, the way `export.yml` already surfaces
the drain notice. A test also asserts every `ALL_BLANK_OK` key names a real column
of a real tab: a typo'd entry exempts nothing, silently.

### Idempotency is now keyed by `(product, idempotency_key)`

Three apps mint keys with no coordination, so a collision was a question of when.
The `_outbox_ledger` tab gains a `product` column; existing four-column rows are
read as TradeRated's (they are, by construction) rather than skipped as
malformed, which is what stops an upgrade re-creating every referral.

### Profit Wizard

Its outbox is live and its lane drains — claim, ledger, report, isolation, and
its `200 + matched:false` case, which is treated as neither success nor a hard
failure. The four ServiceTitan **writes** its items carry belong to ticket 15 and
are raised as named failures until then. See KNOWN_UNVERIFIED.md before pointing
a live Profit Wizard tenant at this.

### Fixed — `job_number` was emitted on every jobs row but never filled

The `jobs` tab read the job number from `job["number"]`. ServiceTitan's JPM job
object names it **`jobNumber`**, so the column was blank on every row the
exporter has ever written (0 non-empty cells across 2431 live rows). Profit
Wizard's `public.jobs.job_number` is NOT NULL, so all 1694 hosted job inserts
failed with `23502`. It reads `jobNumber` now, falling back to `number` — the
contract only ever widens.

The suite did not catch this because every job fixture in it also used `number`,
so the tests asserted our mistake rather than ServiceTitan's shape. The job
fixtures now use the real field name and there are explicit regression tests that
fail if anyone reads only `number` again. (`number` fixtures on invoices and
projects are untouched — it is the correct field name on those entities.)

### Fixed — a repeated `st_technician_id` could reach the `technicians` tab twice

`technicians` rows are now deduplicated on `st_technician_id`, first-seen. The
key is deliberately **not** `email`: two distinct technician ids sharing one
address is a real ServiceTitan state, both can be assigned to jobs, and dropping
one would leave `jobs` rows pointing at a technician missing from the tab. That
collision is now logged with both ids, for a consumer whose schema requires
unique emails to resolve on its side.

### Fixed — a review pass on one failure class: succeeding quietly with no data

Every item below is the same shape of bug. Something goes wrong, and the system
reports success with blank or missing data instead of failing loudly. A blank
cell is indistinguishable from a contractor who genuinely has none, and the
consumers index tabs by column NAME, so a wrong name yields zero rows rather than
an error.

- **Transport failures now raise `TransportError`, an `STCLIError`.**
  `ServiceTitanClient._send` wrapped nothing, so an `httpx.ReadTimeout` on the
  report POST — the most timeout-prone call in the repo — walked straight past
  the financial feed's per-tab guard and discarded invoices, timesheets and
  business units that had already been fetched, leaving every `_meta` row stale.
  Transport errors on a **read** are also retried on the same budget a 429 gets,
  since a timeout is more often a blip than a verdict; a write is retried only
  when the failure proves nothing was sent (see "a write is never re-sent"
  below). Every `except STCLIError` in the repo is now a complete guard rather
  than one that holds until the network hiccups; `st` prints a clean error
  instead of a traceback.
- **A TrueQuote image upload that never reaches an HTTP status is a retryable
  rejection, not an exception.** `upload.py` promised "401/429/5xx ends the pass,
  not the run", but a DNS failure is neither. It aborted the run *after* four
  pricebook tabs were written and before their `_meta` rows were, forgot every
  upload the pass had already made, and skipped the outbox drain. The image
  ledger now flushes in a `finally`, and the image lane as a whole is strictly
  non-fatal: `_meta` for four tabs never depends on a side lane.
- **The job-costing report's SHAPE is now checked, not just its name.** The
  metadata fetched before the data POST was discarded. A contractor's namesake
  report carrying none of the (unverified) custom markers passes the name guard
  as a single unambiguous match; its columns then don't match, every money cell
  comes out blank, and the tab is written with a healthy `row_count`. The
  report's declared fields are checked against `JOB_COST_COLUMNS` — in its
  metadata and again on the first data page — and a mismatch raises
  `ReportColumnsMismatchError` naming the missing columns. It is equally the
  tripwire for ServiceTitan renaming a field on the genuine built-in report.
- **The pricebook feed gained the financial feed's per-tab isolation.** One of
  the four fetches failing aborted all four. They are four independent full
  replaces a consumer joins by id, so a failed tab now keeps its previous
  contents and `_meta` row, and is named in the summary as `pricebook_failed=`.
- **A transient image download failure no longer prunes the ledger.** A CDN 500
  meant that key never reached `seen_keys`, `keep()` dropped it, and the next run
  re-uploaded identical bytes. `complete` now counts `download_failed`, so "could
  not fetch it" is never read as "it is gone".
- **`fetch_timesheets` follows `hasMore`.** It read one page per job and ignored
  the rest, silently dropping labour hours off the end of a long job — which
  reads downstream as a cheaper job, not as an error.
- **A pricebook record with no `st_id` is dropped, not written blank.** The
  contract says `st_id` is non-empty and the category-filtered path already
  dropped these; the unfiltered path wrote them and counted them. `row_count` is
  now taken from the grid, so it always means what a reconciler thinks it means.
- **`st jobs list` showed a permanently blank Number column.** `JOB_COLUMNS` read
  `number`; ServiceTitan's JPM job object names it `jobNumber` — the same bug as
  the exporter's, almost certainly copied from here. Fixed the same widen-only
  way (`jobNumber` preferred, `number` kept as a fallback), and the job fixture
  now carries the real field name. `number` on invoices and projects is untouched.
- **`customer_phone` / `customer_email` read the settings arrays.** ServiceTitan
  returns the details as `phoneSettings[]` / `emailSettings[]` arrays; Profit
  Wizard's production client treats the flat scalars only as a fallback. Reading
  only the scalars is the `job_number` failure again. Widen-only in both
  `st_exporter.denormalize` and `st crm customers-list`, so nothing that worked
  before can stop working. **The element's own field name is a guess, not a
  fact**: this originally said `phoneSettings[].phone` as though it were
  established, while ServiceTitan's documented `CustomerPhoneSettings` element
  looks like `{phoneNumber, doNotText}` and every fixture here happened to say
  `phone`, so the suite could not tell the difference. All the plausible
  spellings are now read and the guess is tracked in `KNOWN_UNVERIFIED.md`.
- **A tripwire for the unverified date-filter parameter names.** ServiceTitan
  ignores query parameters it does not recognise, so a wrong spelling exports the
  tenant's entire history and says nothing. The feed now logs a WARNING naming
  the parameter when the oldest record it saw predates the window. It only logs:
  filtering locally would mask the very symptom that proves the name is wrong.
- **Deleted three tests that asserted `f(x) == f(x)`** and replaced them with
  tests that fail when the behaviour regresses.

### Fixed — round two: two of those fixes were incomplete, two were new defects

An adversarial re-review of the pass above. Same failure class, and two of the
entries are the fixes themselves.

- **A write is never re-sent after a transport failure.** Retrying every
  `httpx.HTTPError` on the same budget as a 429 was safe for a GET and wrong for
  everything else: a `ReadTimeout` or a `RemoteProtocolError` after a POST means
  the request very likely *reached* ServiceTitan and only the answer was lost, so
  the retry created a second job, a second booking, a second lead — and each
  outbox lane writes its idempotency ledger only after `perform` returns, so
  nothing downstream could de-duplicate it. Reads are still retried freely; a
  write is retried only on `ConnectError`/`ConnectTimeout`, which prove the
  request never left. Anything else raises `TransportError` immediately, as it
  did before the branch.
- **The image lane now runs AFTER the `_meta` write, not just inside a guard.**
  `ImageLedger.flush()` sat in a `finally`, outside the lane's `except`, so a
  routine Sheets 429 on the raw-cache spreadsheet escaped anyway and left four
  fresh pricebook tabs with an absent or stale `_meta` and the outbox drain
  skipped. The flush now has its own guard — but the real fix is structural: the
  side lane runs after `_meta`, so no exception, hang or `timeout-minutes` kill
  in it can get between a written tab and the row that describes it.
- **A report data page that declares no fields is refused.** Metadata with no
  `fields` deferred to the data page; if the data page had none either, the
  column guard checked nothing, every row zipped to `{}`, and
  `reporting.jobCosts` was written with zero rows, a fresh `_meta` row and no
  recorded failure — the exact silent-blank-money case the guard exists for,
  reached *through* the guard. The data page is the backstop and is never a
  second deferral.
- **A failed pricebook tab no longer lets the image pass prune the ledger.**
  Isolating the four tabs meant a failing `pricebook.equipment` left its items
  out of the records handed to the image pass — which still called itself
  complete and dropped every equipment image key as "gone", re-uploading
  identical bytes next run. The pass is told whether the catalogue was whole.
- **An unreachable ServiceTitan images endpoint stops the image pass.** A
  per-asset `TransportError` was swallowed and the next asset tried, at a full
  retry budget of timeouts each; a handful of them exceeded the workflow's
  `timeout-minutes` on their own. It now stops the pass the way a 403 does.
- **`fetch_timesheets` raises at its page cap instead of writing a truncated
  tab.** A server ignoring `page` answers `hasMore` forever with the same page,
  which was written as a complete `payroll.timesheets`. It is now a named
  failure, so the tab keeps last run's contents.
- **`a|b` column alternation picks the first NON-EMPTY alternative.** "First
  not-None" hid a populated flat `phone` behind a `phoneSettings: [{"phone":
  ""}]`, which is a real ServiceTitan answer — the mini-DSL producing the blank
  column it was added to prevent. `""` is still returned when every alternative
  is blank, so "present but blank" stays distinguishable from "absent".

## [Unreleased] · Financial feed

`st-export --feeds financial` writes four new tabs for Profit Wizard —
`accounting.invoices`, `payroll.timesheets`, `settings.businessUnits` and
`reporting.jobCosts` — each with its own `_meta` row carrying
`contract_version: financial.v1`.

**The column names are Profit Wizard's, not this exporter's.** Its reader already
exists (`lib/hosted/tabs.ts` on `feat-servicetitan-hosted`) and indexes these tabs
by header name using ServiceTitan's own PascalCase spellings, so the headers here
were transcribed from that parser rather than designed. Two consequences worth
knowing: `accounting.invoices` is **one row per invoice LINE ITEM**, not per
invoice, and `reporting.jobCosts` carries the report's own field names verbatim.

### Window: 90 days, for a different reason than the jobs window's 90

Not inherited from the jobs feed. The jobs window is 90 because a five-month-old
job scheduled for today must appear on a technician's screen; nothing about that
applies to an invoice. This window is 90 because that is the **shortest** window
still covering every Profit Wizard surface fed from ServiceTitan — measured
against Profit Wizard's own code, not picked as a round number:

- `OPERATING_METRIC_WINDOW_DAYS = 90` (warranty %, financing %)
- `ANALYSIS_WINDOW_DAYS = 90` in the safety engine, with 7/30/90 buckets
- the six-hourly `sync-job-costs` cron pulls invoices, hours and quotes with
  `maxLookbackDays: 90`
- the dashboard (30 days), technicians page (30) and goals (month-to-date) all
  sit inside 90

It is a separate, separately-configurable knob (`EXPORTER_FINANCIAL_WINDOW_DAYS`,
workflow input `financial_window`) so that a tenant needing a longer financial
history can never drag the jobs window along with it. Known gap: Profit Wizard's
reports page offers a 365-day timeframe, which 90 does not cover — raise the knob
for a tenant that uses it. The 12-month forecasting inputs and 36-month history
come from QuickBooks, not ServiceTitan, and are not this feed's problem.

### Job costing comes from the built-in report, located BY NAME

`/accounting/v2/.../jobs/{id}/costing` 404s on every tenant tried, so per-job cost
comes from the **Job Costing Summary** report instead. Its report id differs per
tenant, so it is discovered at runtime — by exact name, case-folded and
whitespace-collapsed, and by nothing else:

- **no fingerprint fallback and no best-match scoring.** Profit Wizard prefers a
  name match and then falls back to scoring every report by its columns; taking
  the best fingerprint match can silently resolve to a contractor's own report and
  produce wrong money numbers with no error anywhere.
- a report marked user-defined is skipped even when the name matches exactly;
- no match raises `JobCostingReportNotFoundError`, two distinct matches raise
  `JobCostingReportAmbiguousError`. Both are refusals, never a guess.

`POST .../reports/{id}/data` is a **read** — its parameters are in the body only
because a report run has more of them than a query string holds — and is never
gated behind a mutation or dry-run guard. Reporting is throttled far harder than
the rest of the API, so a 429 surviving the client's backoff aborts the whole
report pull rather than writing a truncated tab, and pagination is capped.

### One tab failing never costs the other three

Each of the four tabs is built behind its own guard. A tab that fails is not
written: its previous contents stay exactly as they were and its previous `_meta`
row is **carried forward unchanged**, so `last_run_at` still says when that tab was
last genuinely refreshed. The other three land, and the failure is named in the
run's output as `financial_failed=<tab>`, not buried in a log. Only `STCLIError`
is caught — a bug in the row mapping still crashes loudly rather than quietly
emptying a money tab.

- `financial` is **not** in the default feed set. It is a six-hourly cadence,
  matching the Profit Wizard cron it replaces, not the jobs feed's ~5 minutes.
- `payroll.timesheets` costs **one request per completed job**: the bulk
  `payroll/timesheets` list returns the payroll shape
  (`employeeId`/`startedOn`), while the consumer reads the dispatch shape
  (`jobId`/`technicianId`/`arrivedOn`/`doneOn`/`canceledOn`), which only
  `payroll/v2/.../jobs/{jobId}/timesheets` supplies. Capped by
  `EXPORTER_FINANCIAL_MAX_JOBS` (default 500); hitting the cap is logged.
- `settings.businessUnits` is written here for the first time — the jobs feed
  already *fetched* business units for its denormalisation join, but never wrote
  a tab, so nothing is written twice.
- Requires the ServiceTitan scopes `accounting.invoices:r`, `payroll.timesheets:r`
  and `settings.businessUnits:r`, plus the Reporting permission (exact portal name
  still unconfirmed — see KNOWN_UNVERIFIED.md) and `jpm.jobs:r` for the job list
  the timesheet pass walks.

## [Unreleased] · Pricebook feed

`st-export --feeds pricebook` writes four new tabs to the Export Store —
`pricebook.services`, `pricebook.equipment`, `pricebook.materials` and
`pricebook.categories` — each with its own `_meta` row carrying
`contract_version: pricebook.v1`. TrueQuote reads `equipment` (doors); Profit
Wizard reads `materials`. The three item tabs share one column set and one code
path; only the tab name carries the meaning.

Pricebook is a catalogue, so the feed is a **full replace every run**: no window,
no cursor. It is NOT in the default feed set — it is opted into explicitly,
because it has nothing like the jobs feed's ~5-minute cadence.

- The `--pricebook` no-op flag is **removed** (and the reusable workflow's
  `pricebook` boolean input with it). It never did anything; the capability it
  reserved is now the `pricebook` feed.
- `_meta` gains a `contract_version` column, blank for `jobs`/`technicians`
  (whose contract predates versioning) and `pricebook.v1` for the four new rows.
  A `_meta` tab written by an older exporter reads back as blank, not an error.
- New optional `EXPORTER_PRICEBOOK_CATEGORY_IDS` (workflow input
  `pricebook_category_ids`) restricts the feed to specific categories. Ids are
  requested **one per request and merged** — ServiceTitan's `categoryIds` filter
  silently ignores every id after the first.
- Requires the ServiceTitan scopes `pricebook.services:r`,
  `pricebook.equipment:r`, `pricebook.materials:r`, `pricebook.categories:r` and
  `pricebook.images:r`.

### Image upload (`--upload-images`, on by default with `--feeds pricebook`)

Image bytes still never enter the Sheet — `image_refs` carries identifiers only.
The bytes are downloaded on the contractor's own runner and POSTed to TrueQuote's
`{TRADERATED_OUTBOX_BASE_URL}/pricebook-image` endpoint with a **second** machine
token, `TRADERATED_IMAGE_TOKEN` (scope `image_upload` — not interchangeable with
the booking outbox's token). No token, no upload, no error: the tabs are written
either way.

- Both identifier forms are resolved: a public `https://` url is fetched
  directly, an authenticated `Images/Pricebook/<uuid>.jpg` path through
  `pricebook/v2/tenant/{id}/images?path=…`.
- A 403 on that images endpoint is a **named, non-fatal** outcome
  (`images_permission_denied=true` in the run's output): the contractor may not
  have granted `Pricebook → Images`. Public images and every tab still land.
- Safe to run twice. A new `_image_ledger` tab on the private raw-cache Sheet
  records a key derived from the asset identity plus a hash of its bytes, so a
  re-run sends nothing and a genuinely changed image is re-sent.
- One failed or refused image never aborts the run.

## [0.2.8] — 2026-09-09 · One place to bump the version

The version was written in four places — `pyproject.toml`, `EXPORTER_VERSION`,
the workflow's `EXPECTED_EXPORTER_VERSION`, and the workflow's checkout `ref:`.
Cutting 0.2.7 missed two of them in a row, and 0.2.1 shipped the wrong code
because the same bump was missed silently.

Now two, and never edited by hand:

- `EXPORTER_VERSION` reads the installed distribution metadata, so
  `pyproject.toml` is its single source. The workflow installs the code it just
  checked out, so it always describes *that* code.
- The workflow has one literal, `EXPORTER_TAG`. The checkout ref and the guard's
  expected version are both derived from it, so they cannot disagree. A tag not
  matching `exporter-vX.Y.Z` now fails the run rather than deriving nonsense.
- `scripts/release.sh <version>` bumps both files and verifies both landed.

The remaining literal stays because no *proven* GitHub context names a called
reusable workflow's own ref — `GITHUB_WORKFLOW_REF` was tried and resolved to the
caller's branch. A diagnostic step now records what `github.job_workflow_ref`
actually resolves to on a real run, so it can be removed on evidence.

## [0.2.7] — 2026-09-09 · Multi-technician jobs reach the whole crew

**The `jobs` tab is now one row per assigned technician, not one per appointment.**

ServiceTitan supports multi-technician appointments — an install crew of three is
one appointment with three live assignments. The exporter previously resolved a
single `st_technician_id` per appointment (latest `assignedOn`, ties on lowest id)
because the contract has one such column, so the rest of the crew silently lost
the job. Found on job 21465348, a three-technician install where two of the three
technicians could not see their own work.

- `_active_technician_id` → `_active_technician_ids`, returning every assigned
  technician; `build_job_rows` emits one row each.
- Removal is now resolved **per technician**. The assignment feed is append-only,
  so an unassigned technician still has a live `Active` record in it; only their
  LATEST event counts. Filtering removal rows alone would have resurrected them.
- An appointment with no assigned technician still emits one row with a null
  `st_technician_id`, unchanged.

**Column set is unchanged**, but `st_appointment_id` is no longer unique in the
tab. Consumers must key on (`st_technician_id`, `st_job_id`). Expect row counts to
grow with average crew size.

Closes the `KNOWN_UNVERIFIED.md` entry on the multi-tech tie-break rule.

## [0.2.0] — 2026-06-03 · Full ServiceTitan API coverage

The headline release: `st` and `st-mcp` now span the **entire ServiceTitan REST
API v2** — **all 24 modules**, **393 CLI commands**, and **394 MCP tools** — up
from ~40 hand-written endpoints across 7 modules. The expansion is generated
from a single declarative spec, so the surface stays consistent and cheap to
extend instead of being hand-maintained one function at a time.

### TL;DR

| | Before (0.1.x) | After (0.2.0) |
|---|---|---|
| Modules | 7 | **24** |
| CLI commands | ~40 | **393** |
| MCP tools | ~40 | **394** |
| Source of truth | hand-written per endpoint | one declarative registry |
| Tests | — | **248 passing** |

### Added

- **Registry-driven coverage of all 24 modules.** A new `registry.py` declares
  every module → resource → operation, and a new `engine.py` turns each
  `Resource` into both Typer commands and FastMCP tools from the same spec.
  Newly covered modules include: `pricebook`, `inventory`, `salestech`
  (estimates), `payroll`, `marketing`, `marketing-ads`, `timesheets`,
  `equipment-systems`, `findings`, `task-management`, `telecom`,
  `customer-interactions`, `forms`, `job-booking` (`jbce`),
  `marketing-reputation`, `scheduling-pro`, and `service-agreements` — plus
  gap operations layered onto the existing `crm`, `jobs` (`jpm`), `dispatch`,
  `accounting`, `memberships`, and `settings` groups.
- **Full CRUD + domain actions.** Beyond list/get, generated resources support
  create (`POST`), update (`PATCH`), replace (`PUT`), delete, and documented
  domain actions (e.g. `salestech estimates-sell`, `inventory
  purchase-orders-approve`, `dispatch assignments-assign-technicians`).
- **Export change-feeds.** Modules with change-feeds expose `export-<feed>`
  commands/tools that stream the dataset and return a `continueFrom` token for
  incremental resume (`pagination.fetch_export_page` / `fetch_export_all`).
- **HTTP verbs.** `client.py` gained `put()` and `delete()` (delete accepts
  query params and an optional body) to back the new write operations.
- **Generic list filtering.** Generated list commands take a repeatable
  `--filter key=value` (MCP: a `filters` dict), with light bool/int coercion.
- **Docs set.** A multi-page `docs/` tree (installation, configuration, usage,
  CLI reference, **API coverage**, MCP server, architecture, development) plus a
  per-module **[Examples & recipes](docs/examples.md)** page with timeframes and
  paired CLI ↔ MCP samples. `scripts/gen_api_coverage.py` regenerates the
  coverage inventory from the live command/tool trees.
- **Tests.** New `test_registry.py` (spec invariants, collision and routing
  checks) and `test_engine.py` (factory + generated-command behavior), alongside
  expanded client/pagination/MCP coverage — 248 tests total.

### Fixed

- **Estimates routing.** `estimates` now correctly live under `salestech`
  (`/salestech/...`), not `accounting`. The old `accounting estimates-*`
  commands/tools were removed.
- **Job types routing.** `job-types` now correctly live under `jpm`/`jobs`
  (`/jpm/...`), not `settings`. The old `settings job-types-*` command was
  removed.
- **Reporting rate-limit note** corrected to "1 of the same report per minute
  per tenant."

### Changed

- Both `main.py` (CLI) and `mcp_server.py` (MCP) are now thin wrappers that wire
  the registry through the engine; the 7 original modules keep their hand-tuned
  typed filters as bespoke escapes where the generic archetypes don't fit.
- A resource named after its module gets bare names (`st findings list`,
  `st_findings_list`) rather than a redundant slug.
- MCP tool names follow `st_{module}_{resource}_{action}`; note `jobs`→`jpm` and
  `estimates`→`salestech` in the module segment.

### Migration notes

- `st accounting estimates-list/get` → **`st salestech estimates-list/get`**
  (MCP: `st_accounting_estimates_*` → `st_salestech_estimates_*`).
- `st settings job-types-list` → **`st jobs job-types-list`**
  (MCP: `st_settings_job_types_*` → `st_jpm_job_types_*`).
- No changes to auth, configuration (`ST_*` env vars), or the seven original
  groups' existing commands.

## [0.1.0] — Initial release

- `st` Typer CLI and `st-mcp` FastMCP server over a shared core (client, auth,
  pagination, human-friendly date ranges, rich table output).
- Hand-written coverage of `crm`, `jobs`, `dispatch`, `accounting`,
  `memberships`, `reporting`, and `settings`, including dispatch availability
  (`who-busy`, `capacity`).

[0.2.0]: https://github.com/utkukaynar/Unofficial-ServiceTitan-CLI-MCP/releases/tag/v0.2.0
