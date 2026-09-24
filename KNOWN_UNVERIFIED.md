# Known unverified assumptions

`st_exporter` was built and tested entirely against fixtures — no real ServiceTitan
tenant or Google Sheets credentials existed at the time (Door Serv Pro's
ServiceTitan integration environment hadn't been set up yet). The items below are
places where the code makes a specific, documented guess about ServiceTitan's
actual API behavior, because the guess couldn't be confirmed any other way. Each
is flagged inline in the relevant source file too. **Check these first** once
Paul's ServiceTitan environment (or any real tenant) is available, before trusting
`st_exporter`'s output against real data.

## Financial feed: date-filter parameter spellings

`src/st_exporter/feeds/financial.py`, `INVOICE_DATE_PARAM`, `JOB_COMPLETED_PARAM`

Assumed `invoicedOnOrAfter` on `accounting/v2/.../invoices` (Profit Wizard uses
this exact spelling, so it is well-evidenced) and `completedOnOrAfter` on
`jpm/v2/.../jobs`.

**`completedOnOrAfter` is now CONFIRMED** (2026-09-16): the published
`tenant-jpm-v2` OpenAPI description of `GET /tenant/{tenant}/jobs` carries both
`completedOnOrAfter` and `completedBefore` — *"Return jobs that are completed
after a certain date/time (in UTC)"*. The spelling the feed sends is the right
one; the tripwire below stays as a standing check. `invoicedOnOrAfter` remains
inferred.

A wrong parameter name is the dangerous kind of wrong here: ServiceTitan ignores
unknown query parameters rather than rejecting them, so the feed would quietly
export the **entire** invoice or job history instead of the window. Check the
row counts against the window on the first real run.

There is now a tripwire for exactly this: after fetching, the feed logs a
WARNING naming the parameter if the oldest `invoiceDate` / `completedOn` it saw
predates the window start (`warn_if_older_than_window`). It **only logs** — it
deliberately does not filter the rows out locally, because doing so would hide
the one symptom that proves the parameter name is wrong.

`sort: "-completedOn"` on the job list was inferred and was **WRONG** — RESOLVED
2026-09-16. Run `35034278334` on `tr-pioneer-overhead-door` answered:

```
HTTP 400 {"errors":{"sort":["The value '-completedOn' is not valid for Sort."]}}
```

which cost the whole `payroll.timesheets` tab (the job list drives the per-job
timesheet calls). The endpoint's own description names the closed list —
*"Available fields are: Id, ModifiedOn, CreatedOn, Priority."* — so completion
date is not sortable at all. The feed now sends `sort: "-Id"` (`JOB_SORT`), the
stable proxy for "newest first"; the `max_jobs` cap therefore keeps the newest
jobs in the window rather than the oldest.

## Jobs feed: WHERE the export change-feed puts a job's completion instant

`src/st_exporter/denormalize.py`, `_COMPLETED_ON_KEYS`

The `jobs` tab's `completed_on` column (appended in 0.2.20) reads `completedOn`,
falling back to `completedOnUtc`.

**What is evidenced.** `completedOn` is the documented spelling on the JPM job
object, and it is not a bare guess. Three independent lines of evidence:

1. **Profit Wizard's own Direct path reads it in production, for these two
   contractors.** `profitwizard/lib/crm/servicetitan.ts:852`:

       completed_date: job.completedOn ? new Date(job.completedOn) : undefined,

   That is the same precedent that resolved the customer-contacts entry below —
   a live, shipped reader against the same tenants is the strongest evidence
   available short of a recorded response. It is also what fills
   `completed_date` on the direct baseline this exporter is being compared
   against, so matching it is what makes the two comparable at all.
2. The sibling query parameter `completedOnOrAfter` on
   `GET /jpm/v2/tenant/{tenant}/jobs` is CONFIRMED against the published
   `tenant-jpm-v2` OpenAPI description (see the first entry on this page) — a
   parameter that by construction filters on a field of that name.
3. `financial.warn_if_older_than_window` already reads `completedOn` off live
   job records from that same endpoint.

**What is NOT verified.** That the **export change-feed** —
`/jpm/v2/tenant/{tenant}/export/jobs`, which is what actually fills `_raw_jobs`
and therefore this tab — spells it identically to the LIST endpoint. All three
evidence lines above are about the LIST/detail endpoint; the two are different
endpoints with separately-generated response models, and no live response from
the export feed has been inspected for this field. The alternate
spelling is carried for the same reason `job_number` still reads `number`
underneath `jobNumber`; both names mean the same fact, so accepting both cannot
pick up a different one.

**How the first live run answers it, without anyone remembering to look.**
`completed_on` is deliberately NOT in `blank_columns.ALL_BLANK_OK`, so if neither
spelling matches, the next `jobs` run (every 5 minutes) emits

    BLANK COLUMN: jobs.completed_on is empty on all N rows

as a WARNING, a GitHub `::warning` annotation and a step-summary line. Check the
first run after this ships. The measured baseline to check it against: on
`tr-doorservpro`, 864 jobs have `jobStatus` = completed, so a correct reading is
several hundred non-empty cells, not zero.

One false-positive shape is worth knowing about: a tenant with genuinely no
completed jobs inside the window would also trip that warning. It is left
checked rather than exempted anyway — an exemption would silence the exact signal
this entry exists to produce, and "no completed jobs at all in 90 days" is itself
worth a look.

## Jobs feed: WHERE a job's billed revenue lives

`src/st_exporter/denormalize.py`, `_TOTAL_REVENUE_KEYS`

The `jobs` tab's `total_revenue` column reads `total`, falling back to
`invoiceTotal`.

**Why these two and in this order.** It is the DIRECT path's own expression.
Profit Wizard fills `jobs.total_revenue` from
`(job.total || job.invoiceTotal) ?? undefined`
(`profitwizard/lib/crm/servicetitan.ts:856`), and that is the ONLY code path in
Profit Wizard that writes the column. Reading the same two fields in the same
order is what makes hosted numbers comparable to the direct baseline: the QA
sweep that found this gap is a direct-vs-hosted comparison, so a different source
would produce a different figure for the same job and break the comparison even
when it was arguably a better number.

**What is inferred.** That these fields exist on the JPM job record at all. The
inference is not weak — the direct baseline carries revenue on 446 jobs and line
856 is the only thing that could have put it there, so at least one of the two is
real and populated on ~446 of that tenant's jobs. But Profit Wizard's `STJob`
interface is a statement of belief about the API, not proof, and it has been
wrong before in exactly this way: the comment above `jobTypeId` in that same file
records that its `type`/`jobType` fields are "near-always undefined in practice",
which silently sent all 2894/2896 production job rows down a `custom` fallback.
And as with `completed_on`, the EXPORT change-feed has not been inspected for
either field.

**There is a second, larger source already in the export if this one is empty.**
`accounting.invoices` carries `ItemTotal` per invoice LINE — 2817 rows live on
`tr-doorservpro` — which is billed revenue at a finer grain and covers more jobs
than 446. It is not used here because summing it would produce numbers that
disagree with the direct baseline, and because Profit Wizard already parses
`ItemTotal` into `itemTotal` and then discards it: `aggregateInvoiceLines`
(`lib/hosted/sync.ts:173`) uses it only to detect negative price-modifier lines
for `discount_total`, accumulates only `itemTotalCost` into material/equipment,
and returns `{material, equipment, discount}` with no revenue field at all. So
the hosted path never writes `total_revenue` from any source. **That is a Profit
Wizard-side gap and it is not fixed by this column** — it is simply a different,
already-available route to the same fact, worth taking if `total`/`invoiceTotal`
turn out to be absent.

**How the first live run answers it.** `total_revenue` is deliberately NOT in
`blank_columns.ALL_BLANK_OK`, so if neither field exists the next `jobs` run
emits `BLANK COLUMN: jobs.total_revenue is empty on all N rows` as a WARNING and
an Actions annotation. The baseline to check against: the direct pull has revenue
on 446 of ~999 jobs, so a correct reading is a few hundred populated cells, not
zero — and notably not all of them either.

## ~~Jobs feed: does a `0` in `job.total` mean $0, or "not recorded"?~~ — MEASURED

`src/st_exporter/denormalize.py`, `_TOTAL_REVENUE_KEYS`, `_money_or_absent`

**"Not recorded", and the first cut of `total_revenue` got this wrong and
shipped.** It read a resolved `0` as a real zero, on this contract's "blank is
not zero" rule, and called the divergence from Profit Wizard's `||` deliberate.

Measured on `tr-doorservpro` across 1256 rows of the live jobs tab:

| | share |
|---|---|
| rows with a BLANK `total_revenue` | **0** |
| distinct jobs reading exactly `0` | 45% |
| **COMPLETED** jobs reading `$0` | **41%** |

Non-zero values ranged `70.00`–`30,302.10`, so the field spelling is right and
the column is genuinely populated. But ServiceTitan never sends null here — it
sends `0` for "no revenue recorded" — so reading `0` literally labelled four
completed jobs in ten as free work.

Profit Wizard's `(job.total || job.invoiceTotal) ?? undefined` makes the opposite
choice, and `||` rather than `??` is the whole point: a `0` falls through, and
the field is omitted when nothing is left. That is why the direct baseline
carries revenue on 446 of ~999 jobs as NULL rather than as zeros, and why the two
paths disagreed on roughly 400 jobs with hosted holding the harmful answer.

The asymmetry settles it: a blank makes Profit Wizard REFUSE to compute a margin,
a `0` makes it compute one against zero revenue — **-100%** — and
`blank_columns` cannot catch that, because it only fires on a column empty on
EVERY row. An all-zero column passes straight through the tripwire.

Accepted cost: a genuine zero-dollar job is now indistinguishable from one with
nothing recorded. This field cannot tell them apart anyway, the direct path
already makes that trade, and "I cannot tell you" beats "they worked for free".

**Still open:** whether `reporting.jobCosts.TotalRevenue` is the better source.
That tab only started working today (run 35159471697, 1563 rows) and its
`TotalRevenue` is already in the frozen `JOB_COST_COLUMNS`, but nobody has
measured how well it is populated. Do that before adopting it.

## Financial feed: how ServiceTitan marks a report as custom

`src/st_exporter/feeds/reporting.py`, `_CUSTOM_BOOLEAN_FIELDS`, `_CUSTOM_KIND_FIELDS`

The exporter refuses to build `reporting.jobCosts` from a contractor-authored
report, but ServiceTitan's actual field for "this report is user-defined" has not
been seen on a real tenant. Several plausible spellings are accepted (`isCustom`,
`custom`, `isUserDefined`, `userDefined`, and a `type`/`reportType`/`kind`/`source`
of `Custom`/`UserDefined`/`Tenant`). The asymmetry is deliberate: a false positive
costs a loud refusal, a false negative costs silently wrong money numbers. **If
ServiceTitan marks custom reports some other way, or not at all, the name guard
alone cannot see an unmarked namesake** — it comes back as a single unambiguous
match. So the report we settle on must also DECLARE the columns
`financial.JOB_COST_COLUMNS` names, checked against both its metadata document
and the first page of its data (`reporting.require_columns`); a mismatch raises
`ReportColumnsMismatchError`, which skips that one tab and names the missing
columns. That check does not depend on any unverified spelling, and it is what
turns "wrong report, blank money, reported as success" into a loud refusal.
Confirm on a tenant that has custom reports.

The refusal now carries its own evidence, so one live run is enough to tell a
missing report from a false positive in these guesses: a name-matching report
skipped as custom is quoted by category id, report id, name **and the marker it
was judged on** (`isCustom=true`, `reportType='Custom'`, ...), and the message
says outright that the spellings are unconfirmed. The asymmetry above only holds
if the loud half is actionable, and "the only reports with that name are custom"
was not — it points the contractor at a report they can already see.
`reporting.custom_marker` is that rule; `_looks_custom` is now a thin wrapper on
it, so selection is unchanged.

## ~~Financial feed: why `reporting.jobCosts` is absent on tr-doorservpro~~ — DIAGNOSED

`src/st_exporter/feeds/reporting.py`; `src/st_exporter/config.py`,
`job_cost_report_id`

**Answered 2026-09-16 by run 35155266960, and it was none of the things it was
assumed to be.** The chain, in the order each was eliminated:

1. *Reporting permission* — fine. Never the problem. Enumeration reads 12
   categories and 265 reports.
2. *Report name* — was wrong, fixed in 0.2.14: this tenant carries "Job Costing
   Summary Report", not "Job Costing Summary". Confirmed working.
3. *`require_columns` refusing a mismatched report* — the assumed cause, and it
   has never run. Resolution fails before it.
4. **The actual cause: the tenant has TWO distinct reports with that exact
   name** — ids `21131704` and `21639096`, both in category `operations`,
   neither carrying any custom marker — and `find_builtin_report` refuses to
   choose between them.

The refusal is correct and stays. What is new is that there are now two ways
past it that are not guesses, and a third that is and remains rejected:

- **Elimination by declared columns** (`capable_of`). A candidate that does not
  declare `JOB_COST_COLUMNS` could not produce the tab at all — `require_columns`
  would refuse it moments later — so dropping it removes a non-viable candidate
  rather than preferring one viable one. If exactly one survives it is selected.
  **This does not resolve two copies of the same report**, which both declare the
  same columns; that case still refuses, by design.
- **An explicit pin** (`EXPORTER_JOB_COST_REPORT_ID`). A human's recorded
  decision. Honoured whatever the report is named and whether or not it looks
  custom, but it does NOT skip `require_columns`, so a mistyped or stale id fails
  loudly instead of writing an empty tab. An id the tenant does not have is its
  own refusal and never falls back to the name — falling back could re-select the
  very report the pin was added to avoid.
- **Rejected: any tie-break heuristic.** Lower id, higher id, best column score.
  Each always returns a winner, and a wrong winner is a contractor's own report
  silently supplying their cost numbers. This is the same reasoning the module
  docstring uses to reject Profit Wizard's column-scoring fallback.

**Still unknown, and needs a human:** which of `21131704` / `21639096` is the
genuine built-in. Nobody has looked in the tenant's ServiceTitan UI. If both
declare the frozen column set — likely, if one is a copy of the other — the
exporter will keep refusing until somebody either deletes/renames the duplicate
there or sets the pin here.

## Financial feed: the Reporting permission's portal name

The ticket flags this too. `reporting/v2/...` needs a Reporting permission whose
exact name on the ServiceTitan app page is unconfirmed. Until it is granted, the
`reporting.jobCosts` tab is skipped with `financial_failed=reporting.jobCosts`
and the other three tabs still land — the failure is visible, not silent.

## Financial feed: the job-timesheets response envelope

`src/st_exporter/feeds/financial.py`, `_as_records`

`payroll/v2/.../jobs/{jobId}/timesheets` has been observed answering with a bare
JSON array on some tenants and a `{"data": [...]}` envelope on others (Profit
Wizard accepts both). Both are accepted here. Whether it paginates on a job with
very many segments is unknown, so the envelope form is now followed while it
reports `hasMore` (up to a 20-page stop); a bare array is taken as the whole
answer, since there is nowhere for a cursor to live. Reaching the 20-page stop
**raises** `TimesheetPaginationError` rather than truncating: the only ways to
get there are an absurd job or a server that ignores `page` and hands back the
same page forever, and neither may be written as a complete tab. The tab is
skipped for that run, keeps its previous contents and is named in the summary.

## ~~CRM customer contact details: WHERE a phone number and an email live~~ — RESOLVED

`src/st_exporter/feeds/contacts.py`; `src/st_exporter/denormalize.py`,
`apply_customer_contacts` / `_contact_detail`; `src/st_cli/commands/crm.py`,
`CUSTOMER_COLUMNS`

**Resolved 2026-09-15, and it was a real bug — the first-run signal this entry
predicted is exactly what happened.** `customer_phone` and `customer_email` were
blank across the WHOLE tab on BOTH live tenants (2,441 rows on one, 1,068 on the
other). None of the three layers this entry described ever matched, because none
of them is where ServiceTitan keeps the data.

The details live on a separate sub-resource:

    GET /crm/v2/tenant/{tenantId}/customers/{customerId}/contacts
    -> {"data": [{"type": "Email" | "MobilePhone" | "Phone" | "Fax", "value": ...}]}

This is no longer a guess: TradeRated's own live **Direct-path** function has
been reading that endpoint in production for the same two contractors
(`traderatedapp/supabase/functions/get-servicetitan-technician-jobs/index.ts`,
"Fetch customer contacts (email/phone are stored separately)"). The exporter now
fetches it for the customers behind the windowed rows and overlays the result on
top of the old readers, which stay in place — the contract only ever widens, so a
tenant that does carry a flat `phone` still exports it, and it is what fills the
cells if the contacts call is refused.

Selection is by `type`, never by position: `MobilePhone` first, then `Phone`;
`Email` for the email column. **`Fax` is not a phone and is never a fallback.**
One selector (`feeds.contacts.select_contact_value`) serves both the endpoint and
the customer-record fallback, so the two cannot drift.

**A 403 costs the two cells, not the feed.** The contacts pull degrades the way
job types and business units do (`run._customer_contacts`, mirroring
`_optional_reference`): a WARNING plus a `Customer contacts degraded` Actions
annotation, the `jobs` tab written in full with those cells falling back to the
customer record. Neither column is in `ALL_BLANK_OK`, so the blank-column
detector still reports them if they end up empty tab-wide — which is what must
happen if this fix is ever wrong again.

### Still open: whether a BULK contacts route exists

`src/st_exporter/feeds/contacts.py`, `EXPORT_FEED`; `EXPORTER_CONTACTS_ROUTE`

The per-customer route is N+1 — deduped by `customerId` and capped by
`EXPORTER_CONTACTS_MAX_CUSTOMERS` (default 3000), so of the order of 1,000–1,500
requests per run for a tenant this size, inside the 600-per-10s budget but not
free. A bulk `crm/v2/tenant/{id}/export/customers/contacts` change-feed would be
strictly better, and **it could not be established that one exists**:
`st_cli/registry.py` declares the crm export feeds as `customers`, `locations`,
`bookings`; no public documentation of a contacts export feed was found; and
third-party ServiceTitan connectors list customer contacts only as the
per-customer CRUD sub-resource.

So it is a setting rather than a guess. `EXPORTER_CONTACTS_ROUTE=export` drains
that feed, cursor-tracked in the jobs `_meta` cursor bundle under
`customer-contacts` and cached in `_raw_customer_contacts` exactly like the other
five feeds. **Trying it is safe and is how the question gets answered**: a
404/400 means the feed does not exist, which is announced (`Bulk contacts feed
absent`) and falls back to the per-customer route for that run rather than
blanking two columns. Flip it on a live tenant once, read the annotation, and
record the answer here.

### RESOLVED (2026-09-17): a single inactive customer's 409 blanked the WHOLE run

`src/st_exporter/feeds/contacts.py`, `fetch_contacts_per_customer`

The RESOLVED entry above fixed the READING side and was correct as far as it
went, but exporter-v0.2.32 hit a second bug on the same tenant (Door Serv Pro,
2471 job rows, live run today): `customer_phone`/`customer_email` were blank on
**every** row again, and this time every customer involved was active.

Root cause: `fetch_contacts_per_customer` only treated a **404** on one
customer as skippable; every other status — a **409**, which ServiceTitan
answers for an inactive customer ("Customer ID = &lt;id&gt; is not active") —
was re-raised. That `APIError` propagated out of the per-customer loop,
through `run._customer_contacts`'s degradation guard, and was logged as
"DEGRADED: could not read customer contacts", which returns `{}` for **every**
customer in the run, not just the one that 409'd. One inactive customer among
2471 job rows was enough to blank the contacts overlay for the whole tenant —
the exact "self-inflicted outage that repeats every run" the function's own
docstring already warned a 404 would cause if mishandled, just for a status
code the fix didn't cover yet.

Fixed by treating 409 the same as 404 in `fetch_contacts_per_customer`: skipped
per-customer, counted, and logged once per run with a count (`"%d customer(s)
answered 409 (inactive customer) and keep blank contact cells"`) rather than
once per occurrence. Every other status (403 above all) still propagates for
`_customer_contacts`'s degradation guard, unchanged. See
`tests/st_exporter/feeds/test_contacts.py::TestPerCustomerFetch::test_a_409_on_one_customer_does_not_lose_the_others`
and the run-level
`tests/st_exporter/test_customer_contacts.py::test_a_409_on_one_inactive_customer_does_not_blank_every_rows_contacts`.

**This fully explains today's blank `customer_phone`/`customer_email` columns.**
The field-reading side (`denormalize._contact_detail`/`_typed_contact`,
`apply_customer_contacts`) needed no change: it already matches Profit Wizard's
direct client (`lib/crm/servicetitan.ts`, `phoneSettings[].phone` /
`emailSettings[].email`, with `contacts[]` selected by `type` beneath it) and
already matches the ordering `KNOWN_UNVERIFIED.md`'s RESOLVED entry above
records. With the 409 no longer aborting the whole fetch, the per-customer
contacts overlay reaches every active customer's row again, and only the
handful of genuinely inactive customers fall back to the (usually blank)
customer-record scalars — which is correct, not a bug.

### Still open: the CLI table lags the exporter

`st crm customers-list`'s Phone/Email columns resolve through the `a|b`
alternation DSL, which can express a path and an index but not "the entry whose
`type` is Phone", and cannot make a second HTTP call for a sub-resource at all.
So those two CLI columns are blank on these tenants while the exporter's tab is
right. Fixing it means teaching the DSL type selection or giving `crm.py` a
bespoke resolver; see the parity note in `st_cli/output._resolve` and
`tests/test_contact_resolution_parity.py` before touching either.

## `appointment-assignments` "removed" status values

`src/st_exporter/denormalize.py`, `_REMOVED_ASSIGNMENT_STATUSES`

Assumed values meaning "this assignment record no longer represents an active
technician assignment": `{"unassigned", "removed", "cancelled", "canceled"}`
(compared case-insensitively). If ServiceTitan uses a different vocabulary (or
encodes "removed" as a separate boolean/timestamp field rather than a status
string), `_active_technician_id` will resolve stale or wrong technicians.

## ~~Technician tie-break rule for concurrent multi-tech assignments~~ — RESOLVED

`src/st_exporter/denormalize.py`, `_active_technician_ids`

**Resolved in 0.2.7 (2026-09-09).** The sanity check this entry asked for
arrived: ServiceTitan job 21465348 (Pioneer Overhead Door) is a three-technician
install — one appointment, three live assignments. It exported as a single row
carrying only the technician assigned last, and the job was invisible to the
other two in TradeRated.

Discarding technicians was never the right answer; the single `st_technician_id`
column forced it. The `jobs` tab is now one row **per assigned technician**, so a
crew of three yields three rows sharing an `st_appointment_id`. The column set is
unchanged. The old recency-then-lowest-id rule survives only as row ORDER, to keep
runs deterministic.

**Consequence for consumers:** `st_appointment_id` is no longer unique in the
`jobs` tab. Key on (`st_technician_id`, `st_job_id`) — already what
`sync-hosted-jobs` upserts on.

## `jobTypeName` / `businessUnitName` presence on job records

`src/st_exporter/denormalize.py`, `_job_type_name`, `_business_unit_name`

Assumed job records may carry `jobTypeName`/`businessUnitName` directly (used
preferentially if present), falling back to a reference-table join by
`jobTypeId`/`businessUnitId` otherwise. Whether the real API embeds these names on
job records at all — and if not, whether the fallback reference lookups
(`st_exporter/feeds/reference.py`) return records shaped as assumed — is
unconfirmed.

## Location coordinate JSON path

`src/st_exporter/denormalize.py`, `_coordinate`

Tries `address.latitude`/`address.longitude` first (nested under the address
object, matching how the rest of the address is shaped per
`st_cli/commands/crm.py`'s `LOCATION_COLUMNS`), then a top-level
`latitude`/`longitude`/`lat`/`lng`/`lon` fallback. The real field path — and
whether it exists at all for every location — is unconfirmed.

## `job_status` values that mean cancelled/deleted

`src/st_exporter/denormalize.py`, `_EXCLUDED_JOB_STATUSES`

Assumed values meaning a job is cancelled/deleted and must be excluded from the
Export Store: `{"canceled", "cancelled"}` (compared case-insensitively, matched
against `job.jobStatus`). Without this exclusion, a cancelled job with a
future-dated appointment would keep being re-written to the `jobs` tab forever —
see the PR #1 review's finding #1. Whether ServiceTitan actually surfaces
cancellation this way (a `jobStatus` value) versus a separate `active`/`deleted`
flag, and whether there are other status values that should also be excluded
(e.g. a distinct "Deleted" status), is unconfirmed.

## `job_status` sourced from the job entity, not the appointment

`src/st_exporter/denormalize.py` — `row["job_status"] = job.get("jobStatus")`

The frozen contract's `job_status` column is mapped to the *job's* status field,
not the appointment's own (separate) status field, purely because of the column
name. ServiceTitan may carry meaningfully different information on each; this
mapping hasn't been confirmed against real data.

## Technician `active` filter parameter

`src/st_exporter/feeds/reference.py`, `fetch_technicians`

Passes `params={"active": "Any"}` on the assumption that (a) ServiceTitan
settings list endpoints default to active-only without an explicit filter, and
(b) `active=Any` is the correct parameter name/value to request both active and
inactive technicians. Neither half of that assumption is confirmed against a
real tenant — see the PR #1 review's finding #6.

**Still unverified, but no longer able to fail a run (ticket 21).** The guess is
deliberately NOT removed: dropping the parameter would silently make the tab
active-only, and that is no better founded than the guess it replaces — a
deactivated technician missing from the tab is indistinguishable, downstream,
from one who never existed. What changed is the failure mode. On an HTTP **400**,
and only a 400 — ServiceTitan saying it does not accept this filter — the list is
re-fetched without the parameter, and both a WARNING and a GitHub Actions
annotation say that the tab may now be active-only. Every other status (403 on a
missing Settings → Technicians permission, 429, 5xx, a transport failure) is about
the request's fate rather than the parameter and is re-raised for the feed guard
in `run.py` to handle.

**What a tenant still needs to settle:** whether `active=Any` is accepted at all,
and if not, what the real spelling is. If the annotation ever fires on a real run,
that is the answer — the fallback path is a degradation, not a fix, and it should
be deleted in favour of the confirmed parameter.

## `modified_on` sourced from the appointment, falling back to the job

`src/st_exporter/denormalize.py` —
`row["modified_on"] = appointment.get("modifiedOn") or job.get("modifiedOn")`

The contract describes `modified_on` only as "for drift debugging" without
specifying which entity's timestamp it should reflect. This mapping is a
reasonable guess, not a confirmed requirement.

## `jobs.booking_id` read from the export feed's `bookingId`

`src/st_exporter/denormalize.py` — `row["booking_id"] = job.get("bookingId")`

Confirmed on the JPM **list/get** endpoints (2026-09-24, Door Serv Pro): every
job carries a top-level `bookingId`, e.g. job `115409266` → `115382913`, and 15
of the 200 most recent jobs were non-null. NOT yet confirmed on
`jpm/v2/tenant/{tenant}/export/jobs`, the change-feed that fills `_raw_jobs` —
the same list-vs-export gap that left `recall_for_id`/`warranty_id` near-blank
(see below). `booking_id` stays OUT of `blank_columns.ALL_BLANK_OK`, so a
whole-column blank on a tenant that does take bookings is reported. **Check on
the first run after release:** the `jobs` tab should carry `115382913` on job
`115409266`'s rows.

## Profit Wizard hosted-parity columns (jobs, technicians, sales.estimates)

`src/st_exporter/denormalize.py`, `src/st_exporter/format.py`, `src/st_exporter/sales.py`,
`src/st_exporter/feeds/sales.py`

Six columns appended to `jobs`, seven appended to `technicians`, and a new
`sales.estimates` tab — all read straight from `export-columns-spec.md`, built for
Profit Wizard's hosted (Export Store) path to reach parity with its existing
Direct ServiceTitan path. Each field's confidence is different:

- **`jobs.recall_for_id`, `jobs.warranty_id`** — well evidenced on the DIRECT
  path, still unconfirmed on the EXPORT feed, and now with live signal pointing
  at a real gap rather than a wrong spelling. Profit Wizard's own Direct path
  reads `job.recallForId` / `job.warrantyId` off the JPM job object in
  production (`profitwizard/lib/crm/servicetitan.ts`), but it reads that object
  from `GET jpm/v2/tenant/{tenant}/jobs` (the **list** endpoint, filtered by
  `ids`/`page`) — never from `jpm/v2/tenant/{tenant}/export/jobs` (the change-feed
  that actually fills `_raw_jobs` here). What is NOT confirmed is that the export
  feed spells these fields identically to the list endpoint's response, or
  carries them at all — the same open question `completed_on`'s sibling PRs on
  this repo already flag.

  **Live evidence (2026-09-17, Door Serv Pro, exporter-v0.2.32, 2471 job rows):**
  `recall_for_id` is populated on exactly **1** of 2471 export-feed rows, while
  Profit Wizard's Direct path (reading the list endpoint for the same tenant)
  marks **111** of ~6000 jobs as recalls. `warranty_id` is blank on **all**
  2471 rows. Both point the same way: the export feed is very likely missing
  or misnaming these two fields relative to the list endpoint, rather than this
  tenant genuinely having almost no recalls and zero warranty jobs — 1-in-2471
  is far below the 111-in-6000 direct-path rate for the same population, and a
  literal 0 for warranty across every row is the same shape of "wrong field
  name" signal `job_number` and `customer_phone`/`customer_email` gave before
  they were fixed.

  **Not fixed here, deliberately.** There is no ServiceTitan documentation
  confirming what the export feed's job record actually calls these two facts
  (if it carries them at all), and guessing a second spelling risks the "widen
  enough spellings and you stop being able to tell right from wrong" trap this
  file's blank-column entries warn about elsewhere. A per-delta lookup against
  the list endpoint (mirroring `outbox/profitwizard._resolve_job_appointment_id`'s
  shape — fetch the export feed's job ids in batches of 50 against `jpm/.../jobs?ids=`)
  would settle this without guessing, but it is a new API surface and a new
  request budget, not a one-line field-name change, so it is left as the
  documented next step rather than built speculatively. `recall_for_id` and
  `warranty_id` stay OUT of `ALL_BLANK_OK` — the detector firing on them is
  working as intended, not "crying wolf": there is no live-tenant confirmation
  that either field is genuinely optional for every tenant (unlike, say,
  `pricebook.services.cost`, which is absent from a documented schema).
  **Action for whoever has ServiceTitan API access next:** capture one real
  `export/jobs` response for a job known to be a recall or under warranty via
  the Direct path, and diff its keys against the list endpoint's response for
  the same job id.
- **`jobs.no_charge`, `jobs.total`, `jobs.business_unit_id`, `jobs.sold_by_id`** —
  read from the spec's literal field names (`noCharge`, `total`, `businessUnitId`,
  `soldById`) with no fallback spelling, because the spec gives exactly one name
  for each and a widened guess without a second candidate is just a guess dressed
  up. `jobs.total` is deliberately narrower than the `total_revenue` column
  that precedes it: that column widens to `job.invoiceTotal` as a fallback, this
  one does not — the spec asks for `job.total` verbatim, nothing else.
- **`technicians.phone`, `technicians.business_unit_id`, `technicians.role_ids`,
  `technicians.home_address`, `technicians.home_latitude`,
  `technicians.home_longitude`** — genuinely unverified. `phone` widens
  `phoneNumber`/`phone`, matching `settings.businessUnits.Phone`'s existing
  widen. `businessUnitId` is a single guess (ServiceTitan's technician list
  sometimes nests `businessUnit: {name}` instead — see
  `docs/integrations/servicetitan-api.md` in the Profit Wizard repo — so a flat
  `businessUnitId` may not exist on every tenant's response; if it doesn't, the
  column is blank, not wrong). `roleIds` is assumed to be a bare array of ids.
  The technician's home address object's own key is unknown — `homeAddress` is
  tried first, `home` second, and either shape's `latitude`/`longitude` read the
  same way `denormalize._coordinate` reads a job location's. If the blank-column
  detector fires on any of these six on a real tenant, that is the answer.
- **`sales.estimates` — every column beyond `EstimateId`.** There was no real
  ServiceTitan tenant to record an Estimates API response from, so every field
  name in `src/st_exporter/sales.py` is inferred from the shape ServiceTitan uses
  for the SAME fact elsewhere in this exporter (an item's `sku` object mirrors an
  invoice line's; `qty` mirrors the Estimates API's documented request body) with
  a flatter fallback tried second. The date-filter parameter on the fetch side
  (`feeds/sales.py`, `ESTIMATE_DATE_PARAM = "modifiedOnOrAfter"`) is likewise a
  guess, guarded by the same `warn_if_older_than_window` tripwire the invoices
  and job-completion filters already use. **Check this whole tab first** once a
  real tenant with estimates is available — a wrong guess here costs blank
  `Item*` columns and/or an unbounded window, not wrong money (every money/hours
  cell still goes through `_money`, blank when null). Two shapes are read
  deliberately wider than the first draft: `soldBy` is accepted as a bare
  employee id as well as `{id}` (the bare id is what this repo's own
  `estimates-sell` examples send), and `Total` falls back to `subtotal + tax`
  when the response has no `total` — the Estimates response is believed to
  carry those two separately and no total of its own.

## ~~CRM Outbox response envelope~~ — RESOLVED 2026-09-14

`src/st_exporter/outbox/client.py`, `TradeRatedOutboxClient.claim`

Confirmed by reading the deployed edge function
(`supabase/functions/crm-outbox/index.ts:320-325`, branch
`feat-st-export-reader`): the envelope is
`{"success": true, "count": N, "items": [...]}` and each item is
`{id, kind, idempotency_key, payload, attempts}` (`toClaimedItem`, :207-217).
The `{"items": [...]}` assumption was right; `success`/`count`/`attempts` are
extra fields this client ignores.

Also confirmed at the same time: the **base URL** is the Supabase Functions
origin, `https://<project-ref>.supabase.co/functions/v1`, because the gateway
strips `/functions/v1` before `parseOutboxRoute` sees the path (:32-45). That is
a different host and a different path shape from TrueQuote's and Profit Wizard's
— see "Three outbox path shapes" below.

## CRM Outbox claim limit

`src/st_exporter/outbox/drain.py`, `_DEFAULT_CLAIM_LIMIT`

Defaults to 10 pending items per drain. The spec says "up to N pending items"
without naming N. Unverified against a real deployment; adjust once ticket 07's
real outbox endpoint is live and its actual behavior/limits are known.

## ~~`technician_rating` has no known ServiceTitan write~~ — WRONG, FIXED 2026-09-14

`src/st_exporter/outbox/actions.py`, `_perform_technician_rating`

**The premise was false when it was written.** `registry.py` has declared
`Module("customer-interactions", resources=(Resource("technician-ratings",
ops="LRC"),))` since `e08a801` (2026-06-01) — three months before `actions.py` —
and `C` is create. The URL it generates,
`/customer-interactions/v2/tenant/{id}/technician-ratings`, is
character-for-character the one TradeRated's Direct path posts to
(`update-servicetitan-rating/index.ts:88`), and `SETUP.md:147` has every
contractor grant Customer Interactions -> Technician Rating -> WRITE before
their first run. Every `technician_rating` item was being reported `failed`
against a permission the contractor had already given.

The exporter now maps TradeRated's payload — `technicianId =
int(servicetitan_technician_id)`, `jobId = int(servicetitan_job_id)`,
`rating = float(rating) * 2` clamped to 0-10, mirroring `convertRating`
(:117-120) — and posts it. `st_id` is the synthetic `"<technicianId>:<jobId>"`,
because the endpoint is create-or-update keyed on that pair and its response
carries no id.

**Still unverified against a real tenant.** Nothing has ever been queued: the
production `crm_outbox` table was empty on 2026-09-09, and the enqueue is
unreachable because `update-servicetitan-rating` answers 401 to its own
service-role caller (`verifyAuth`, confirmed from the production function log
2026-09-10). So this write will fire for the first time only after TradeRated
fixes that caller. Two specific things to check on that first real item:

- the rating lands as **10** for a five-star review, not 5;
- a 404 (job not in the tenant) is reported `failed` with no `permanent` flag,
  which costs five attempts before the row goes terminal. Their result endpoint
  already accepts `permanent: true` (`crm-outbox/index.ts:178`) — sending it on
  a 4xx is a worthwhile follow-up, not done here.

## ~~`referral_lead` payload passed through unmapped~~ — RESOLVED 2026-09-08

`src/st_exporter/outbox/actions.py`, `_perform_referral_lead`

**No longer a guess: it was tested against a real tenant and the assumption was
wrong.** The first live attempt (Door Serv Pro, 2026-09-08) returned:

```
ServiceTitan 400: campaignId and summary required
```

TradeRated's payload is its own snake_case shape (`name`, `phone`, `address`,
`referred_by`, `notes`) and carries neither required field, so a pass-through can
never succeed. The exporter now supplies both — `campaignId` from the referral
campaign resolver, `summary` derived from the payload — and forwards everything
else untouched. Only absent keys are filled, so a `campaignId` TradeRated later
chooses to send still wins.

Still unconfirmed: whether ServiceTitan **ignores** the remaining snake_case keys
or has other required fields it did not mention in that first rejection. It
reported only these two, which suggests the rest of the body is tolerated — but
the error may simply be reporting the first failing validation. If a second 400
appears naming different fields, the payload needs a real field-by-field mapping
and that mapping's ownership (this repo vs TradeRated) has to be settled.

## ~~Referral campaign creation body~~ — PARTLY RESOLVED 2026-09-08

`src/st_exporter/outbox/campaign.py`, `ReferralCampaign._create`

**Tested against a real tenant; a name-only body is refused.** The first create
attempt returned:

```
categoryId:     Required property 'categoryId' not found
businessUnitId: Required property 'businessUnitId' not found
```

Both are now resolved from the tenant — lowest active id from
`settings/business-units` and `marketing/categories` respectively — so a new Hosted
customer's first referral succeeds with nothing configured. Each choice is logged,
and either can be pinned with `TRADERATED_CAMPAIGN_BUSINESS_UNIT_ID` /
`TRADERATED_CAMPAIGN_CATEGORY_ID`.

Still unconfirmed:

- **Whether those two are the only additions required.** The same 400 also carried
  `"request": ["The request field is required."]`, which reads like the ASP.NET model
  binder naming the root object rather than a real field, but it may not be. `dnis`
  is a plausible further requirement.
- **Whether lowest-active-id is the right business unit.** It is deterministic and
  reproducible, not correct: campaign is the dimension ServiceTitan reports revenue
  by, so a customer with several business units may see referral revenue attributed
  to the wrong one. The pin exists for that, but nothing prompts them to set it.
- ~~Whether `marketing/categories` is the correct resource for a campaign's
  `categoryId`~~ — moot. Asking for it returned `403 Scope validation failed`: the
  app is not granted that endpoint, and requesting the scope would send every
  already-onboarded customer back to their Developer Portal. The category is now
  borrowed from an existing campaign's own `categoryId` (campaigns read is already
  granted), and `marketing/categories` remains only as a fallback whose 403 is
  swallowed. **Unverified:** whether the campaigns list returns `categoryId`, a
  nested `category: {id}`, or neither — all three are handled, the last by raising
  and naming the pin.

Also unconfirmed: whether the campaign list endpoint supports a server-side `name`
filter. `_find` deliberately lists all campaigns and matches locally instead, because
a filter ServiceTitan silently ignored would return page one of every campaign and
could match the wrong row.

## Which tabs a tenant is allowed to read — decided by a 403, and unverified

The caller workflow no longer carries a repository variable per feed. Every feed
job runs for every contractor and ServiceTitan's scopes decide what exports: a
**tab** the tenant's app was never granted answers **403** and is skipped quietly
(that tab is not written, its siblings still are, run stays green), while a tab
that HAS been written successfully before — proved by its `_meta` row, or failing
that by the tab still existing — is treated as a **revoked** permission and turns
the run red. `src/st_exporter/scopes.py`.

Unverified, and worth knowing before trusting the quiet half:

- **Whether every ungranted read endpoint really answers 403.** One is confirmed:
  `marketing/categories` returned `403 Scope validation failed` on an app without
  that scope (see above). The rest is inference from the same platform behaving
  the same way. An endpoint that answered 401 or 404 for a missing scope would be
  a loud failure rather than a quiet skip — noisy, not silent, which is the safe
  direction to be wrong in.
- **Whether a 403 is ever transient.** Nothing suggests it is, but if
  ServiceTitan ever returned one under load, a feed that had run before would be
  announced as revoked for that cycle. It would recover by itself on the next
  run, and its tabs and `_meta` row are untouched meanwhile.
- ~~**Whether a partially-granted module is possible**~~ — **not unverified: it
  is prescribed.** `permissions.md`, the team's own tick-box runbook, grants per
  ENTITY, not per module. Its TrueQuote block ticks Pricebook Services, Equipment,
  Categories and Images and deliberately omits **Materials**, which arrives only
  when the contractor also buys Profit Wizard; and Reporting is a section of its
  own that the runbook's author could not even name the box for. So a half-granted
  "module" is the ORDINARY state of a TrueQuote-only tenant and of a Profit Wizard
  tenant without Reporting, on every run, forever. The exporter therefore decides
  at TAB level: a 403 rules out exactly the tab that earned it, the feed's other
  tabs are still attempted, and each is judged on the evidence for itself. What
  remains genuinely unverified is only which *portal* box name maps to each tab —
  the strings in `scopes.TAB_PERMISSIONS` are derived from the runbook and from the
  code's own call sites, not from the portal UI.
- **Whether a tab can exist with no `_meta` row.** It should not, but the
  evidence read is a Sheet somebody can edit: `read_grid` answers `[]` for a tab
  that is not there, so a deleted or renamed `_meta` tab would otherwise turn every
  later 403 into "never bought" — quiet, and the wrong direction. The ledger
  therefore accepts an EXISTING export tab as second-line evidence of a past run.
  Untested against a real Sheet whose `_meta` a contractor has renamed.
- **A 400 is deliberately NOT treated as "not bought"** — `active=Any` below is
  exactly that case, and a sibling branch handles it. Only 403 is an
  authorization answer here.

## Pricebook list endpoints: `active=Any`, and the `assets` shape

`src/st_exporter/feeds/pricebook.py`, `src/st_exporter/pricebook.py`

- **`active=Any` — CONFIRMED 2026-09-16** against `tenant-pricebook-v2`'s
  OpenAPI: `active` on `/services`, `/materials` and `/equipment` is
  `ActiveRequestArg` with values `[True, Any, False]`, defaulting to active-only.
  The spelling was right and withdrawn items do export as `active=false`.
- **`assets[].id` — RESOLVED 2026-09-16, and the guess was WRONG:**
  `Pricebook.V2.SkuAssetResponse` has no `id` at all. Its fields are `alias`,
  `fileName`, `isDefault`, `type` and `url`. So `image_refs` is **always**
  url/path-shaped in production (the contract permits that — "identifiers"), the
  `assets[].id` branch in `asset_identifier` only ever fires on fixtures, and the
  asset dedupe is effectively a dedupe by url. Left in place: it costs one `or`
  and it is the right identity if ServiceTitan ever adds ids.
- **`name` is never blank.** The contract guarantees it; ServiceTitan could
  return both `displayName` and `name` as null. The builder falls back to `code`
  and then the item id rather than emit a blank cell. Whether that fallback ever
  fires in practice is unknown.

Also unverified: that the four tabs' row counts are small enough that a full
replace every run stays well inside a Sheets write. A very large catalogue has
never been measured — and `pricebook.v2` widened the item tabs from 12 columns to
42, so the arithmetic moved. Google Sheets caps a spreadsheet at 10,000,000 cells
across all tabs; the pilot tenant's 35,138 items now come to ~1.48M (was ~0.42M),
around 15% of the cap, with room for roughly 238,000 item rows before the
pricebook tabs alone reach it. Still comfortable, no longer irrelevant. What has
never been tested is a single `values.update` write of that size, or the write
time for a six-figure catalogue.

## Pricebook full payload — spellings verified against the spec, not a tenant

`src/st_exporter/pricebook.py`

Every column `pricebook.v2` added is named after a field in the published Pricebook
v2 OpenAPI document — `Pricebook.V2.{Service,Equipment,Material,Category}Response`
and the nested `SkuWarrantyResponse` / `SkuVendorResponse` — rather than guessed or
copied from a consumer's client type. `cost` and `hours` are the two that matter
most:

- `Pricebook.V2.MaterialResponse` and `Pricebook.V2.EquipmentResponse` both
  declare `cost` (decimal, "The cost paid to acquire the material") and `hours`
  (decimal, "The number of hours associated with the installing the …").
- `Pricebook.V2.ServiceResponse` declares `hours` ("Hours needed to complete this
  service") and **no cost field of any spelling**. So `pricebook.services.cost` is
  blank on every row of every tenant by construction, and `blank_columns` exempts
  it by name with that reason.

What is still unverified is the same thing as everywhere else on this page: that a
live tenant's payload matches its own document. The tripwire is in place either
way — `cost` is NOT exempted on `pricebook.equipment` or `pricebook.materials`, so
a wrong spelling there fires the whole-column-blank warning above 25 rows, and
`hours` is exempted on no tab at all.

The wider column set makes that detector work harder, and the exemption list is
where it can be blunted. Two kinds of entry now sit in
`blank_columns.ALL_BLANK_OK` for the item tabs and they are NOT the same strength
of claim:

- **structurally absent** — the resource's schema has no such field, so the column
  is blank on every row of every tenant forever (`cost` on services, `is_labor` on
  equipment). Certain, from the spec.
- **optional upstream** — the field exists and is commonly unset catalogue-wide
  (`cross_sale_group`, `external_id`, `account`). A guess about contractor
  behaviour, and the weaker one: if a spelling in that group is wrong, the
  exemption is what hides it. Every money and hours column is deliberately left
  OUT of both groups on every tab that has the field.

Deliberately NOT copied from Profit Wizard's direct integration: its
`item.price || item.memberPrice || item.addOnPrice` price fallback. Those are three
different prices in the API (list, member, add-on), not three spellings of one, and
a `||` chain fires on a real `0` — it would turn a genuinely free item into its
member price. The exporter exports `price` and leaves the reconciliation to the
consumer, which is the same reason `cost` and `hours` are blank-when-null here.

## Pricebook image upload — what could not be confirmed without a live TrueQuote

`src/st_exporter/images/`

The wire format was derived by READING TrueQuote's receiving route
(`apps/admin/app/api/outbox/pricebook-image/route.ts` and
`lib/integrations/hosted-pricebook-image.ts`) on branch
`feat/servicetitan-hosted`, **which was uncommitted working-tree code at the time
(2026-09-14)**. Nothing was exchanged with a running instance. Specifically:

- **The base URL's shape is assumed.** This client posts to
  `{TRADERATED_OUTBOX_BASE_URL}/pricebook-image`, i.e. the base is expected to be
  `https://<truequote-host>/api/outbox`. **Confirmed 2026-09-14** against
  `apps/admin/next.config.js` (no `basePath`, no rewrite touching `/api/outbox`).

  The apparent conflict with the booking lane's `{base}/crm-outbox` paths is
  **RESOLVED, and it was a real bug**: `/crm-outbox` is TRADERATED's route on
  TradeRated's own Supabase host, and it was only ever sharing this setting by
  accident. The image lane now reads `TRUEQUOTE_OUTBOX_URL` /
  `TRUEQUOTE_IMAGE_TOKEN` first, falling back to the `TRADERATED_*` spellings so
  a connector already deployed with the old names keeps working. See "Three
  outbox path shapes".
- **TrueQuote reads no idempotency field.** Its dedupe is intrinsic —
  `storage_path = sha256(source_url)`, uploaded with `upsert: true`, and the row
  keyed `(external_item_id, asset_id | sha256(source_url))`. The
  `Idempotency-Key` header this exporter sends is therefore ignored today. What
  actually stops bytes being re-sent is our own `_image_ledger` tab, because the
  endpoint offers **no GET, no HEAD and no manifest** to ask "do you have this
  already?" before sending.
- **One asset per item, by necessity.** The receiving route hardcodes
  `is_primary: true` and the reconcile RPC clears `is_primary` for the whole item
  first, so a second upload for one item MOVES its primary rather than adding a
  second image. The exporter therefore sends only the asset TrueQuote's own
  `selectDefaultPricebookImage` would have chosen. If TrueQuote later wants every
  asset, its route has to stop asserting `is_primary`.
- **Tie-break ordering may differ in the last digit.** TrueQuote sorts the
  `id|fileName|alias|url` tuple with `localeCompare`; this sorts by code point.
  They can disagree only about which of several equally-default images wins.
- **`active=Any` items are uploaded too.** The pricebook feed lists withdrawn
  items so the `active` column can say `false`; their images are uploaded like
  any other. Whether TrueQuote wants bytes for inactive items was not asked.
- Nothing here has met a real ServiceTitan tenant: the `Pricebook → Images`
  permission, the real `Content-Type` ServiceTitan returns for a storage-path
  image, and whether real assets ever exceed the 8 MiB cap are all unconfirmed.


## Three outbox path shapes — confirmed, not a bug to reconcile

`src/st_exporter/outbox/routes.py`

    TradeRated     GET|POST {base}/crm-outbox     POST {base}/crm-outbox/{id}/result
    TrueQuote      POST     {base}/booking/claim  POST {base}/booking/result
    Profit Wizard  POST     {base}/claim          POST {base}/result

All three are correct, for structural reasons: TradeRated's base is a Supabase
Functions origin where the whole edge function is one route and the id is a path
segment; TrueQuote's and Profit Wizard's are Next.js route handlers under
`https://<host>/api/outbox`, and TrueQuote's booking queue is one level deeper
because its base already carries `/pricebook-image`. Paths are therefore per-lane
configuration, overridable without a release via `{PREFIX}_OUTBOX_CLAIM_PATH` /
`{PREFIX}_OUTBOX_RESULT_PATH`.

The **token scopes** do not share a vocabulary either — `crm_outbox`,
`booking_outbox` + `image_upload`, `servicetitan_outbox` — and every app answers
a wrong-scope token with a flat 401. There is no "one token per app".

## TrueQuote's booking lane is transcribed from uncommitted-at-the-time code

`src/st_exporter/outbox/truequote.py`

Everything about this lane — `item_id` not `id`, `booking` not `payload`, no
`kind` field, `booking_id` rather than `st_id` on the result, the JSON-body
claim limit — was read from TrueQuote's `feat/servicetitan-hosted` branch on
2026-09-14. That branch is **committed** (all of it lands in `2731a615`) but
**not merged**: TrueQuote's `main` has no `apps/admin/app/api/outbox` directory
at all. Treat it as provisional and confirm with their session before a real
contractor is pointed at it.

One consequence worth stating separately: **the queued `payload` is TrueQuote's
own `ServiceTitanBookingInput`, not a ServiceTitan request body.** `dispatch.ts:240`
enqueues the input and the direct path applies `createBookingPayload`
(`server.ts:840`) afterwards, at push time — which for a Hosted company happens
on this runner instead. `build_booking_body` re-expresses that transform. If
TrueQuote ever moves the transform to *before* the enqueue, this exporter would
double-transform and every booking would lose its contacts. That is the one
change on their side that would silently break this lane.

## Profit Wizard's items: three of the four are now performed; `update_job` is not

`src/st_exporter/outbox/profitwizard.py`, `src/st_exporter/outbox/profitwizard_writes.py`

The lane is real and drained: claim, ledger, report, isolation and the
`matched: false` handling are all exercised. `update_job` has no Profit Wizard
producer at all yet (`lib/outbox/types.ts` names the kind but nothing enqueues
it), so it still raises a named `UnsupportedOutboxKindError` and is reported
`failed` — that message now says explicitly that no producer exists yet,
rather than pointing at a ticket.

`assign_technician`, `push_prices` and `push_estimate` are all implemented,
each copied from Profit Wizard's own direct-CRM client
(`lib/crm/servicetitan.ts`) so a Hosted company and a Direct one land on the
same ServiceTitan state for the same decision:

* `assign_technician` (`_perform_assign_technician`, in `profitwizard.py`):
  resolve the job's appointment (`jpm` appointments-list), read its active
  assignments (`dispatch` appointment-assignments), and POST
  `assign-technicians` / `unassign-technicians` for the authorized diff —
  copied from `setAppointmentTechnicianSet`. One caveat carries over from that
  source: the `unassign-technicians` body shape was never confirmed against a
  live ServiceTitan tenant (their own TODO says ServiceTitan's developer-portal
  page for it renders client-side); `assign-technicians` IS confirmed live.
* `push_prices` (`perform_push_prices`, in `profitwizard_writes.py`): PATCH
  `pricebook/.../services/{id}` (for a `svc_`-prefixed `crm_item_id`, prefix
  stripped) or `pricebook/.../materials/{id}` otherwise, body
  `{"price": new_price}` — copied from `pushPrices`. No equipment branch
  exists on PW's own direct client either.
* `push_estimate` (`perform_push_estimate`, in `profitwizard_writes.py`): POST
  `sales/v2/tenant/{id}/estimates` with `{jobId, name, items}` — copied from
  `pushEstimate`. Unlike the other two, this is a plain create with no
  idempotent semantics on ServiceTitan's side, and the created estimate's id is
  assumed to come back under `id` (unverified against a live tenant).

**Do not set `PROFITWIZARD_*` on a contractor whose Profit Wizard is already
enqueueing `update_job` items** until that performer exists — the other three
kinds are safe to drain today, modulo the two `unverified` items above.

Also unverified: Profit Wizard's **claim response field names**. The client reads
several spellings for each field (`item_id`/`itemId`/`id`, `payload`/`body`/`data`,
and so on) rather than assuming one, in the same widen-don't-narrow posture their
result endpoint takes. Confirm the real names on the first live claim.

## `categories` has two shapes — RESOLVED 2026-09-16, it was a real bug

`src/st_exporter/feeds/pricebook.py`, `src/st_exporter/pricebook.py`

Run `35134016237` on `BBTT-01/tr-doorservpro` (exporter 0.2.11) exported 61
pricebook categories and then reported `category_ids` AND `category_names` blank
on all 10041 `pricebook.equipment` rows and all 4990 `pricebook.materials` rows,
while `pricebook.services` populated both.

`tenant-pricebook-v2`'s OpenAPI says why: `Pricebook.V2.ServiceResponse.categories`
is an array of `Pricebook.V2.SkuCategoryResponse` objects (`id`, `name`, `active`),
but `Pricebook.V2.EquipmentResponse.categories` and
`Pricebook.V2.MaterialResponse.categories` are arrays of **bare `int64` ids**. The
reader accepted only the object form, so two thirds of the catalogue lost its
category linkage entirely. Fixed by normalising both shapes in the fetch layer and
resolving the names from the categories endpoint (one extra request, and only when
nameless ids actually arrived).

Still unverified: `categoryIds` is documented as taking a comma-separated list
(`"example": "123,456"`), which contradicts the one-id-per-request quirk TrueQuote
learned the hard way. The serial-request behaviour is deliberately kept — a
live-tenant lesson outranks an example string — but it is worth re-measuring on a
real tenant, because batching would cut the filtered fetch's request count.

## `settings.businessUnits.Code` has no field behind it — CONFIRMED ABSENT 2026-09-16

`src/st_exporter/financial.py`, `src/st_exporter/blank_columns.py`

Run `35132986620` (same tenant) reported `Code` blank on all 189 business units.
It is not a tenant that left it empty: `TenantSettings.V2.BusinessUnitResponse` in
`tenant-settings-v2`'s OpenAPI has no `code` property, and neither does its export
twin. The only code-ish fields are `accountCode`/`conceptCode` (the TENANT's
franchise account and concept — identical on every unit, so not a substitute) and
`certifiedSentriconSpecialistCode`. Profit Wizard's reader reads only
`BusinessUnitId`, `Name`, `Address` and `Active`, so nothing downstream is waiting
on it. Exempted in `ALL_BLANK_OK` with that reason; **the column should be dropped
at the next `financial.v2` bump**, which is a contract change and is not being made
here.

## The general tripwire: whole-column-blank detection

`src/st_exporter/blank_columns.py`

Every guess on this list fails the same silent way: the exporter reads a field by
a guessed name, the hand-written fixture spells it the same way, the suite goes
green, and a real tenant's Sheet carries a **whole blank column** that looks
exactly like a contractor with no data. That is how `job_number` reached 2431 live
rows undetected.

So every feed now runs `check_blank_columns` over the grid it just built — the
`jobs` and `technicians` tabs directly, the eight pricebook/financial tabs through
`_TabGuard.attempt`. If a column is in the header and empty on **every** data row
across at least 25 rows, it logs a WARNING naming the tab, the column and the row
count. It only logs: a genuinely empty column on a real tenant must still export,
so nothing is filtered and no tab is ever failed.

Columns that are legitimately blank for a whole tenant are listed in `ALL_BLANK_OK`
with the reason, per tab. **Add to that list only for a column that is optional by
contract** — never to quiet a column from this document, which is precisely what
the detector exists to find.

## The contract guard is advisory until CI is a required status check

`.github/workflows/ci.yml`, `docs/export-contract.md` ("What CI cannot do for itself")

Nothing in this repository can make its own CI run. GitHub honours `[skip ci]`,
`[ci skip]` and `[no ci]` in a head commit message and does not start the workflow
at all — and a workflow that never ran is not a failed one, so a pull request
carrying that text is mergeable by default. `if:` conditions cannot help: they are
evaluated only once a run exists.

**Human action, outside this repo:** Settings → Branches → branch protection rule
for `main` → "Require status checks to pass before merging" → add `test`. A
required check that never reported blocks the merge, which is what turns the
contract fixtures, the published register and the append-only anchor from a
courtesy into a gate. Until that is set, every one of them is advisory — including
the anchor, whose whole purpose is to be the one check the branch cannot subvert.

There is no in-repo tripwire for this, deliberately: any test that tried to assert
it would itself be running inside the run that was skipped.

## Images feed: does `modifiedOn` move when an item's IMAGE is replaced?

`src/st_exporter/images/upload.py`, `_is_still_fresh` / `REVERIFY_AFTER_DAYS`

The image pass skips the DOWNLOAD of any asset whose item has a `modifiedOn` no
later than the moment the ledger last confirmed that asset's bytes. This is the
only pre-download check available — the idempotency key hashes the payload, so
`ledger.has(key)` cannot be asked until the bytes are already in hand, which is
what used to make a ~7,191-asset tenant re-download the whole catalogue on every
run just to rediscover it had already sent it (run 35130164187).

What is assumed: ServiceTitan bumps a pricebook item's `modifiedOn` when one of
its `assets` changes, and not only when a scalar field like `price` does. That
is the behaviour the `pricebook.*` tabs' own full-replace-every-run design makes
moot, so nothing in this repo has ever had to depend on it before.

If the assumption is wrong, the failure is a DELAY, not a wrong picture, and it
is bounded on purpose: `REVERIFY_AFTER_DAYS` (7) re-verifies every asset at
least that often whatever the timestamps say, so a swapped image reaches
TrueQuote within a week at worst. Check on a real tenant by replacing one item's
image and watching whether the next `images-feed` run re-uploads it or reports
it in `images_revalidated`. If it does not, lower `REVERIFY_AFTER_DAYS`; do not
remove the check, or the pass stops converging.

**This entry is NOT resolved by conditional requests** (below), and the two are
easy to conflate. A conditional fetch only happens once the `modifiedOn`
shortcut has already declined to skip the asset — so if `modifiedOn` never moves
when an image is replaced, the thing that still catches it is the weekly
re-verification, not the `ETag`. What conditional requests changed is what that
weekly re-verification COSTS, not whether it happens.

## Images feed: does ServiceTitan (or its CDN) honour conditional requests?

`src/st_exporter/images/conditional.py`, `src/st_exporter/images/upload.py`
(`_fetch`), `src/st_exporter/images/ledger.py` (`etag` / `last_modified`)

The weekly re-verification above used to be a full re-download of every asset.
It now sends `If-None-Match` / `If-Modified-Since` built from whatever the
server returned last time, and treats a **304** as a verification that moved no
bytes. **Nobody has measured whether that works against a real tenant.** Three
separate things are unknown, and they can have different answers on the
authenticated `pricebook/v2/tenant/{id}/images` endpoint and on the public CDN
urls ServiceTitan hands out:

1. Do responses carry an `ETag` or a `Last-Modified` at all?
2. If we quote one back, does anything answer 304, or is the header ignored?
3. Are the asset urls **signed** (`?X-Amz-Signature=…`, `?sig=…`)? A signed url
   defeats the mechanism twice over: the validator belongs to a url we will
   never request again, and for an asset with no ServiceTitan `id` the ledger's
   own `asset_ref` is built from the url, so the previous run's entry is not
   even found. This one cannot be worked around from this side.

**The code does not depend on any of the three answers.** No stored validator
means a plain GET and a full download — exactly the behaviour that existed
before — and an ignored validator means a 200, also exactly that behaviour. The
feature can only make the pass cheaper, never wrong.

**How to read the answer off one live run.** Every `images` run logs

```
image conditional requests: fetches=… conditional_sent=… not_modified=…
conditional_missed=… validators_present=… validators_absent=… weak_etags=…
signed_urls=… unstable_refs=… -- <one English sentence>
```

and the run's own summary line carries `images_not_modified=`,
`images_conditional_sent=`, `images_no_validator=` and `images_signed_urls=`.

**What a good result looks like**, on the second run after this ships (the first
run only STORES validators; it cannot yet test them):

- `validators_absent=0` — every response offers something to quote back;
- `conditional_sent` ≈ the number of assets due for re-verification, and
  `not_modified` equal or close to it, so the verdict reads
  `conditional requests WORK: N/N (100%)`;
- `signed_urls=0`, and `unstable_refs=0` above all.

**What each bad result means.**

- `validators_absent` high, `conditional_sent=0` → the server offers nothing;
  conditional requests are inert on this tenant and the weekly sweep still costs
  a full re-download. Nothing here is broken; the ticket simply did not buy
  anything, and the next lever is a longer interval or an asset-level `modifiedOn`
  if ServiceTitan ever exposes one.
- `conditional_sent` high, `not_modified=0` → the headers are being ignored (or
  the urls rotate). Check `signed_urls` before concluding anything.
- `unstable_refs > 0` → those assets can never be deduplicated at all, by any
  mechanism in this repo, because their ledger identity changes every listing.
  That is a much bigger finding than this ticket and should be raised on its own.

---

## Nobody has ever measured a real pricebook image

**Status: unverified, and now instrumented.**

`MIN_PLAUSIBLE_IMAGE_BYTES = 1024` is justified in `images/assets.py` with "the
smallest real pricebook assets seen are 2-4 KiB". That figure is an
**assumption**: it was written alongside the constant in commit `2298a15`, and
no asset from any live tenant has ever been sized. The only payload anyone has
measured is a 246-byte placeholder. Run 35145303072 uploaded five real images
and logged not one byte count.

If 2-4 KiB is accurate, ServiceTitan is serving THUMBNAILS and this whole lane
is shipping unusable pictures into contractors' catalogues.

**How it gets settled.** `ImageUploadSummary` now carries every uploaded
payload's size, and the run reports
`images_bytes[min=… median=… max=… total=…]`. A capped run additionally names
each upload with its item id, its byte size and its decoded dimensions. One
uncapped run, or one run with `image_max_assets: 20`, answers it:

- median in the low single-digit KB → thumbnails, and the product question is
  whether ServiceTitan can be asked for a larger rendition at all;
- median in the hundreds of KB → photographs, and the floor's justification was
  merely unsourced rather than wrong.

## The blank-placeholder defence does not hold, and cannot be fixed with a constant

**Status: MEASURED as insufficient. Not fixed. Reported per run.**

Measured by hand on 2026-09-16 against ServiceTitan's **web-app image proxy**:

```
GET .../Image/Images%2FService%2F<guid>.png                          -> 404, 272 bytes
GET .../Image/Images%2FService%2F<guid>.png?size=1200
        &default=Default%2F1.png                                     -> 200, image/webp, 2798 bytes
```

The 2,798-byte body is a **completely blank white 1200x1200 image**. It clears
the 1 KiB floor with room to spare, and the placeholder's size scales with the
`size=` parameter — so no fixed byte threshold can be both above every
placeholder and below every photograph. The floor cannot do the job it was
written for.

**The signal that does scale is DENSITY**, compressed bytes per pixel.
`assets.image_dimensions` reads width and height from the file header
(PNG/JPEG/WebP; no decode, no image-processing dependency, no pixel touched) and
`assets.looks_blank` compares byte count against pixel count. The measured blank
is 0.0019 bytes/pixel; a photograph, however aggressively compressed, is one to
two orders of magnitude denser.

**It is reported, not enforced**, and deliberately so. Setting a rejection
threshold requires knowing what a real asset's density looks like on this
tenant, which is the unverified item above; a rule guessed today could silently
drop real images, which is strictly worse than uploading a blank. The pass
counts `images_suspected_blank=N` and uploads them anyway. A run reporting
`images_suspected_blank=16000` is the finding, and the rule can then be written
from evidence. (The sha256 of every payload rejected by the existing floor is
still logged, so the hash-blocklist route remains open if density ever proves
too blunt for a specific tenant.)

**SCOPE — read before acting on this.** The exporter calls the **authenticated**
`pricebook/v2/tenant/{id}/images?path=…` endpoint, never that web-app proxy, and
it sends neither `size=` nor `default=` (`is_storage_path` forbids a `?` in the
ref ServiceTitan hands us, so we could not add one by accident). `default=` is
what turns a missing asset into a 200 OK placeholder instead of an honest 404,
and we never send it. Run 35145303072 reported `placeholders=0 unsupported=0`
on five real uploads, which is consistent with the authenticated endpoint
behaving honestly.

**What is NOT known** is what the authenticated endpoint returns for an asset
the caller may not see, or one that no longer exists. Reading the code, a 4xx
would surface as `NotFoundError`/`APIError` and be counted as
`download_failed` — but that is an inference from `st_cli/client.py`, not an
observation. **What would settle it:** one run against a tenant with a known-bad
asset path, or simply an uncapped sweep whose `images_download_failed`,
`images_placeholders` and `images_suspected_blank` counts are read together.
