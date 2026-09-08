# Known unverified assumptions

`st_exporter` was built and tested entirely against fixtures — no real ServiceTitan
tenant or Google Sheets credentials existed at the time (Door Serv Pro's
ServiceTitan integration environment hadn't been set up yet). The items below are
places where the code makes a specific, documented guess about ServiceTitan's
actual API behavior, because the guess couldn't be confirmed any other way. Each
is flagged inline in the relevant source file too. **Check these first** once
Paul's ServiceTitan environment (or any real tenant) is available, before trusting
`st_exporter`'s output against real data.

## `appointment-assignments` "removed" status values

`src/st_exporter/denormalize.py`, `_REMOVED_ASSIGNMENT_STATUSES`

Assumed values meaning "this assignment record no longer represents an active
technician assignment": `{"unassigned", "removed", "cancelled", "canceled"}`
(compared case-insensitively). If ServiceTitan uses a different vocabulary (or
encodes "removed" as a separate boolean/timestamp field rather than a status
string), `_active_technician_id` will resolve stale or wrong technicians.

## Technician tie-break rule for concurrent multi-tech assignments

`src/st_exporter/denormalize.py`, `_active_technician_id`

When two assignment events for the same appointment have the same `assignedOn`
timestamp (a real, supported ServiceTitan scenario — multi-technician jobs), the
lowest `technicianId` wins. This is a deliberate, documented simplification
forced by the frozen contract's single `st_technician_id` column — it hasn't been
sanity-checked as the "right" choice against a real multi-tech appointment.

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

## CRM Outbox response envelope

`src/st_exporter/outbox/client.py`, `TradeRatedOutboxClient.claim`

Assumes `GET /crm-outbox` wraps its items as `{"items": [...]}`. The spec names
the per-item shape (`id`, `idempotency_key`, `kind`, `payload`) but not the
envelope around the list. If TradeRated's real response differs (e.g. a bare
array, or a different key), this is the one function to fix.

## CRM Outbox claim limit

`src/st_exporter/outbox/drain.py`, `_DEFAULT_CLAIM_LIMIT`

Defaults to 10 pending items per drain. The spec says "up to N pending items"
without naming N. Unverified against a real deployment; adjust once ticket 07's
real outbox endpoint is live and its actual behavior/limits are known.

## `technician_rating` has no known ServiceTitan write

`src/st_exporter/outbox/actions.py`, `perform_item`

The Outbox contract names two kinds — `referral_lead` and `technician_rating` —
but this CLI's registry has no ServiceTitan endpoint that resembles "post a
rating for a technician." `perform_item` raises `UnsupportedOutboxKindError` for
this kind rather than guessing (a job note? a custom field? something else?).
Every `technician_rating` item will be reported back to TradeRated as `failed`
until this is resolved with the spec owner — raised explicitly in this ticket's
report, not silently worked around.

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
