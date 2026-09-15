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
`jpm/v2/.../jobs` (inferred — Profit Wizard filters its own local `completed_date`
column rather than ServiceTitan's parameter, so nothing confirms the API spelling).
A wrong parameter name is the dangerous kind of wrong here: ServiceTitan ignores
unknown query parameters rather than rejecting them, so the feed would quietly
export the **entire** invoice or job history instead of the window. Check the
row counts against the window on the first real run.

There is now a tripwire for exactly this: after fetching, the feed logs a
WARNING naming the parameter if the oldest `invoiceDate` / `completedOn` it saw
predates the window start (`warn_if_older_than_window`). It **only logs** — it
deliberately does not filter the rows out locally, because doing so would hide
the one symptom that proves the parameter name is wrong.

`sort: "-completedOn"` on the job list is likewise inferred; if it is rejected or
ignored, the `max_jobs` cap would truncate to an arbitrary set of jobs rather than
the most recently completed ones.

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

## CRM customer contact details: WHERE a phone number and an email live

`src/st_exporter/denormalize.py`, `_contact_detail` / `_typed_contact`;
`src/st_cli/commands/crm.py`, `CUSTOMER_COLUMNS`

**This is unverified for the phone as much as for the email, and the doubt is
now about the container, not only the spelling.**

ServiceTitan's documented v2 customer object is `id, active, name, type,
address, contacts, balance, doNotMail, doNotService, hasActiveMembership,
memberships, customFields, createdOn, modifiedOn, mergedToId, externalData` —
with `contacts[] {id, type ∈ Phone | MobilePhone | Email | Fax, value, memo}`
and **none** of `phone`, `phoneNumber`, `email`, `emailAddress`, `phoneSettings`
or `emailSettings` on the customer at all. `phoneSettings {phoneNumber,
doNotText}` appears to belong to the *contact* record rather than the customer,
and to be an object rather than an array. The Profit Wizard citation this repo
leaned on (`lib/crm/servicetitan.ts:903-906`) is itself an unverified guess
against a different endpoint, so it is not evidence either way.

This is the `job_number` trap exactly, twice over: every fixture in this repo
spells the source the way the code guesses, so the suite stays green whichever
reality holds, and a wrong guess is a column blank on **every** row — which
reads as "this contractor has no phone numbers" rather than as an error.

So the code widens rather than choosing, three layers deep, first non-empty
wins and nothing is ever removed:

1. `customer.phoneSettings[] / emailSettings[]` — each entry tried for `phone`,
   `phoneNumber`, `number` / `email`, `emailAddress`;
2. `customer.contacts[]` selected by `type` — `Phone`/`MobilePhone` for the
   phone column, `Email` for the email, matched case-folded. **`Fax` is
   deliberately not a phone** and an untyped entry is skipped: an email or a fax
   in the phone column is a wrong answer, which is worse than a blank one;
3. the flat `customer.phone` / `customer.email` scalars.

Fixtures cover all three shapes, so whichever one a live tenant returns is
tested and no future narrowing can pass the suite.

**Check on the first real tenant:** which of the three containers a customer
actually carries. **The first-run signal for both halves is the same: the column
blank across the WHOLE tab.** `customer_phone` blank everywhere, or
`customer_email` blank everywhere, means none of the three layers matched — look
at one raw customer object before assuming the contractor has no contact
details.

**If even `contacts[]` on the customer turns out to be empty**, the details live
only on the `crm/v2/tenant/{id}/customers/{id}/contacts` sub-resource and this
feed needs an extra pull: an `export/customers/contacts`-style list call joined
back by `customerId`, cached like the other raw feeds. That is a new feed, not a
widening, and is deliberately NOT built here — build it only once a real tenant
shows both columns blank.

**The CLI table lags the exporter here.** `st crm customers-list`'s Phone/Email
columns resolve through the `a|b` alternation DSL, which can express a path and
an index but not "the entry whose `type` is Phone", so they read layers 1 and 3
only. If `contacts[]` is the real shape, the exporter's tab is right and the
CLI's two columns are blank. Fixing that means teaching the DSL type selection
or giving `crm.py` a bespoke resolver; see the parity note in
`st_cli/output._resolve` and `tests/test_contact_resolution_parity.py` before
touching either.

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

## `modified_on` sourced from the appointment, falling back to the job

`src/st_exporter/denormalize.py` —
`row["modified_on"] = appointment.get("modifiedOn") or job.get("modifiedOn")`

The contract describes `modified_on` only as "for drift debugging" without
specifying which entity's timestamp it should reflect. This mapping is a
reasonable guess, not a confirmed requirement.

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

## Pricebook list endpoints: `active=Any`, and the `assets` shape

`src/st_exporter/feeds/pricebook.py`, `src/st_exporter/pricebook.py`

Three guesses, none confirmable without a tenant:

- **`active=Any`.** Assumed the pricebook list endpoints take the same
  `active` parameter as the settings endpoints, so withdrawn items export as
  `active=false` instead of vanishing. If the real parameter differs, the tabs
  silently become active-only — which consumers cannot distinguish from a
  contractor deleting items, and they are forbidden from deleting rows.
- **`assets[].id`.** Taken from TrueQuote's own client type, where it is
  `string | null`. `image_refs` falls back to `assets[].url` when the id is
  absent, and that url is either an HTTPS URL or an authenticated storage path
  (`Images/Pricebook/<uuid>.jpg`). Whether ServiceTitan supplies stable asset ids
  at all on these payloads is unconfirmed; if it never does, `image_refs` is
  entirely url/path-shaped, which the contract still permits ("identifiers").
- **`name` is never blank.** The contract guarantees it; ServiceTitan could
  return both `displayName` and `name` as null. The builder falls back to `code`
  and then the item id rather than emit a blank cell. Whether that fallback ever
  fires in practice is unknown.

Also unverified: that the four tabs' row counts are small enough that a full
replace every run stays well inside a Sheets write. A very large catalogue has
never been measured.

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

## Profit Wizard's items cannot be performed yet

`src/st_exporter/outbox/profitwizard.py`, `perform_profitwizard_item`

The lane is real and drained: claim, ledger, report, isolation and the
`matched: false` handling are all exercised. The four ServiceTitan **writes** its
items carry — `push_estimate`, `update_job`, `push_prices`, `assign_technician` —
belong to ticket 15, which is blocked by this ticket, so their request bodies are
not knowable here. Each is raised as a named `UnsupportedOutboxKindError` that
says which write is missing and which ticket owns it, and is reported `failed`.

The queue is empty by construction until ticket 15 also builds the enqueue side,
so nothing burns attempts today — but **do not set `PROFITWIZARD_*` on a
contractor whose Profit Wizard is already enqueueing** until those four
performers exist.

Also unverified: Profit Wizard's **claim response field names**. The client reads
several spellings for each field (`item_id`/`itemId`/`id`, `payload`/`body`/`data`,
and so on) rather than assuming one, in the same widen-don't-narrow posture their
result endpoint takes. Confirm the real names on the first live claim.
