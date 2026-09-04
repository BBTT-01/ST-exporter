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
