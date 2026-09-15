# Changelog

All notable changes to `st-cli` (the `st` CLI and `st-mcp` MCP server) are
documented here. Format follows [Keep a Changelog](https://keepachangelog.com/);
this project aims for [Semantic Versioning](https://semver.org/).

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
  one-`rm` bypass, and it is refused too.

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

Every feed job is off until a repository **variable** turns it on
(`JOBS_FEED`, `TECHNICIANS_FEED`, `PRICEBOOK_FEED`, `FINANCIAL_FEED`), so a
reviews-only contractor exports no price book, no invoices and no timesheets.
Variables rather than secrets because GitHub does not expose the `secrets`
context to a job-level `if:`. The drain has no variable: every product writes
through its outbox, and a drain that can be switched off by forgetting a
variable is a drain that stops silently.

The reusable workflow's concurrency group is now **two** groups per connector
repository rather than one, split by what a run WRITES rather than by which job
asked for it. Every run with an export feed keeps the shared `…-export` lock,
because they all read and rewrite the whole `_meta` grid; a run asking for
`outbox` and nothing else takes `…-outbox`, because it never touches `_meta` at
all. That is what takes the 5-minute drain off the back of the hourly pricebook
run. It is not a per-FEED key: per-feed locks would let two feeds rewrite
`_meta` over each other, and the prerequisite for them is a merge-on-write
`_meta` in Python, not a change to the YAML.

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
