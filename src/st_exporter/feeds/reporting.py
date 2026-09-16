"""Locating and pulling ONE ServiceTitan built-in report: Job Costing Summary.

`/accounting/v2/.../jobs/{id}/costing` 404s on every tenant tried, so per-job cost
comes from the **Job Costing Summary** report instead — a report ServiceTitan
ships and owns, not one a contractor builds. Its report *id* differs per tenant
(ServiceTitan assigns ids per account) so it has to be discovered at runtime; its
*columns* do not, which is why ``financial.JOB_COST_COLUMNS`` can be frozen like
every other tab.

**The guard that matters.** Discovery is by NAME and by nothing else. ServiceTitan
lets contractors create their own reports and edit their columns, and which custom
reports exist depends on the contractor's package — so "the report whose columns
best match what we expect" can silently resolve to a contractor's own spreadsheet
and produce wrong money numbers with no error anywhere. Profit Wizard's current
code prefers a name match and then falls back to scoring every report; this module
deliberately has **no such fallback**:

- exact name match after case-folding and whitespace collapsing — never
  "contains", never fuzzy, never a best-of score;
- a report carrying any marker that says it is user-defined is skipped even when
  the name matches, because a contractor can name their own report anything;
- no match at all raises :class:`JobCostingReportNotFoundError`;
- more than one distinct match raises :class:`JobCostingReportAmbiguousError` rather
  than picking one;
- and because none of the above can catch a contractor's namesake report that
  carries no marker at all, the report we settle on must also DECLARE the
  columns the tab is built from — ``require_columns`` — or it is refused. A
  wrong report with a plausible name is otherwise indistinguishable from the
  right one until every money cell comes out blank.

Refusing is the correct outcome: the ``reporting.jobCosts`` tab is simply not
written that run, the other three financial tabs still land, and nobody reconciles
against a number that came from the wrong report.

**`POST .../data` is a read.** Its parameters are in the body only because a report
run has more of them than a query string comfortably holds. Nothing here mutates
anything, so it is not gated behind ``dry_run`` and must never be treated as a
write by a future guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Sequence

from st_cli.client import ServiceTitanClient
from st_cli.exceptions import RateLimitError, STCLIError
from st_cli.pagination import fetch_all

MODULE = "reporting"

#: The built-in report this feed replicates. Matched exactly (case-folded,
#: whitespace-collapsed) — see the module docstring for why there is no fallback.
JOB_COSTING_SUMMARY_REPORT_NAME = "Job Costing Summary"

_CATEGORIES_RESOURCE = "report-categories"
_PAGE_SIZE = 200

#: Reporting is rate-limited far harder than the rest of the API (ServiceTitan
#: documents roughly one run of the same report per minute per tenant), so a
#: runaway pagination loop is a way to get the whole tenant throttled. A report
#: of per-job costs over the financial window is thousands of rows, not millions.
_MAX_DATA_PAGES = 50


class ReportUnavailableError(STCLIError):
    """Base: the Job Costing Summary report could not be read this run.

    Always non-fatal to the run as a whole — ``run.py`` skips the
    ``reporting.jobCosts`` tab and writes the other three.
    """


class JobCostingReportNotFoundError(ReportUnavailableError):
    """No report named exactly `Job Costing Summary` is visible to this tenant."""


class JobCostingReportAmbiguousError(ReportUnavailableError):
    """More than one distinct report carries that exact name — refuse to choose."""


class ReportRateLimitedError(ReportUnavailableError):
    """ServiceTitan throttled the report run even after the client's own retries."""


class ReportColumnsMismatchError(ReportUnavailableError):
    """The report we found does not declare the columns this tab is built from.

    The name guard alone cannot catch every wrong report. A contractor's own
    report carrying **no** user-defined marker — and the marker spellings are
    unverified, see KNOWN_UNVERIFIED.md — passes ``find_builtin_report`` as a
    single unambiguous match. Its columns then don't line up, every money cell
    comes out blank, and the tab is written with a healthy ``row_count``: wrong
    money, reported as success. So the report's own declared field names are
    checked against the frozen column set, and a mismatch refuses the tab in
    exactly the same loud, per-tab, non-fatal way a missing report does.

    It is equally the tripwire for the other direction: if ServiceTitan renames
    a field on the genuine built-in report, this fires instead of the tab going
    quietly empty forever.
    """


@dataclass(frozen=True)
class ReportRef:
    """Where one report lives for THIS tenant: its category id and its report id."""

    category_id: str
    report_id: str
    name: str


def find_job_costing_summary(client: ServiceTitanClient) -> ReportRef:
    """Locate the built-in Job Costing Summary report for this tenant, by name.

    Raises :class:`JobCostingReportNotFoundError` or :class:`JobCostingReportAmbiguousError`
    rather than returning a best guess. See the module docstring.
    """
    return find_builtin_report(client, JOB_COSTING_SUMMARY_REPORT_NAME)


#: Caps on the census quoted back in a refusal. A tenant can hold thousands of
#: reports, and this message goes to a WARNING line in the run log, so it is
#: bounded three ways: how many near-miss names, how many other names, and how
#: long any one name may be. The numbers are chosen to be readable in a log line
#: while still being enough to spot "Job Cost Summary" sitting next to the
#: report we asked for.
_NEAR_MISS_CAP = 12
_NAME_SAMPLE_CAP = 18
_NAME_CHAR_CAP = 80
#: Belt and braces over the three caps above: even 30 maximum-length names must
#: not turn one warning into a wall of text.
_CENSUS_CHAR_CAP = 1200

_REPORTING_REMEDIATION = (
    "Grant the Reporting permission (Report Categories, and Reports within the "
    "category) and confirm the built-in Job Costing Summary report is available, "
    "then re-run."
)


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tokens_akin(wanted: str, seen: str) -> bool:
    """Same token, or close enough to be worth a human's eye.

    "cost" matches "costing" (prefix) and "costs" matches "costing" (four shared
    leading characters). This is for HIGHLIGHTING only — see :class:`_Census`.
    """
    if wanted == seen:
        return True
    if len(wanted) < 3 or len(seen) < 3:
        return False
    if wanted.startswith(seen) or seen.startswith(wanted):
        return True
    shared = 0
    for a, b in zip(wanted, seen):
        if a != b:
            break
        shared += 1
    return shared >= 4


class _Census:
    """What the lookup actually SAW, so a refusal can say so.

    A failed lookup is otherwise indistinguishable between three very different
    situations — the Reporting scope is not really granted, the report is named
    differently on this tenant, or it genuinely is not there — and the exporter
    only gets one live run per question. So every category and report name that
    goes past is counted here, with a bounded sample kept for the message.

    Near-miss detection is deliberately crude and lives ONLY in this message: it
    shares no code with :func:`_normalize`, which remains the entire matching
    rule. Nothing here can cause a report to be selected.
    """

    def __init__(self, wanted: str) -> None:
        self._wanted_tokens = wanted.split()
        self.categories = 0
        self.reports = 0
        self._near_misses: list[str] = []
        self._sample: list[str] = []

    def saw_category(self) -> None:
        self.categories += 1

    def saw_report(self, name: Any) -> None:
        self.reports += 1
        display = _clip(" ".join(str(name).split()), _NAME_CHAR_CAP) if name is not None else ""
        display = display or "(unnamed)"
        if self._is_near_miss(_normalize(name)):
            if len(self._near_misses) < _NEAR_MISS_CAP:
                self._near_misses.append(display)
        elif len(self._sample) < _NAME_SAMPLE_CAP:
            self._sample.append(display)

    def _is_near_miss(self, normalized: str) -> bool:
        if not self._wanted_tokens or not normalized:
            return False
        seen = normalized.split()
        hits = sum(
            1 for wanted in self._wanted_tokens if any(_tokens_akin(wanted, s) for s in seen)
        )
        return hits >= min(2, len(self._wanted_tokens))

    def diagnosis(self) -> str:
        """The evidence sentence(s) appended to a not-found refusal."""
        if self.categories == 0:
            return (
                "Nothing was visible to enumerate at all: 0 report categories and 0 "
                "reports came back, so no name was ever compared. That points at the "
                f"Reporting scope or at report visibility, not at a naming mismatch. "
                f"{_REPORTING_REMEDIATION}"
            )
        if self.reports == 0:
            return (
                f"{_plural(self.categories, 'report category', 'report categories')} "
                "were visible but contained 0 reports between them, so no name was "
                "ever compared. That points at the Reporting scope or at report "
                "visibility, not at a naming mismatch. "
                f"{_REPORTING_REMEDIATION}"
            )

        parts = [
            "Enumeration itself worked — "
            f"{_plural(self.categories, 'category', 'categories')} and "
            f"{_plural(self.reports, 'report', 'reports')} were read — so the Reporting "
            "permission is in place; this is a naming or availability question, not a "
            "scope one"
        ]
        if self._near_misses:
            parts.append(
                f"Similarly named reports seen ({len(self._near_misses)} shown): "
                + "; ".join(self._near_misses)
            )
        if self._sample:
            parts.append(
                f"Other report names seen ({len(self._sample)} of {self.reports}): "
                + "; ".join(self._sample)
            )
        body = _clip(". ".join(parts), _CENSUS_CHAR_CAP)
        return (
            f"{body}. Confirm with the contractor which of the reports above carries the "
            "job-cost figures, or that the built-in Job Costing Summary report is enabled "
            "for this tenant, then re-run."
        )


def find_builtin_report(client: ServiceTitanClient, report_name: str) -> ReportRef:
    """The one built-in report with exactly ``report_name``, or refuse."""
    wanted = _normalize(report_name)
    matches: dict[tuple[str, str], ReportRef] = {}
    skipped_custom = 0
    census = _Census(wanted)

    for category in _iter_categories(client):
        category_id = category.get("id")
        if category_id is None:
            continue
        census.saw_category()
        for report in _iter_reports(client, str(category_id)):
            census.saw_report(report.get("name"))
            if _normalize(report.get("name")) != wanted:
                continue
            if _looks_custom(report):
                # A contractor can name their own report anything, including this.
                skipped_custom += 1
                continue
            report_id = report.get("id")
            if report_id is None:
                continue
            ref = ReportRef(str(category_id), str(report_id), str(report.get("name")))
            matches[(ref.category_id, ref.report_id)] = ref

    if not matches:
        detail = (
            f"no built-in report named {report_name!r} is visible to this tenant"
            if not skipped_custom
            else (
                f"the only report(s) named {report_name!r} visible to this tenant "
                f"({skipped_custom}) are custom reports, which are never used — "
                "a contractor-authored report would produce wrong cost numbers silently"
            )
        )
        raise JobCostingReportNotFoundError(f"{detail}. {census.diagnosis()}")
    if len(matches) > 1:
        located = ", ".join(
            f"category {c}/report {r} ({matches[(c, r)].name!r})" for c, r in sorted(matches)
        )
        raise JobCostingReportAmbiguousError(
            f"{len(matches)} distinct reports are named {report_name!r} ({located}). "
            "Refusing to choose between them — picking the wrong one would produce "
            "wrong cost numbers with no error."
        )
    return next(iter(matches.values()))


def field_names(metadata: dict[str, Any]) -> list[str]:
    """The field names a report metadata document declares, in order.

    Empty when the document carries no ``fields`` at all — which is treated as
    "could not check here", not as "no columns": ``fetch_report_rows`` checks
    the first data page's own ``fields`` regardless, and RAISES when that page
    declares none either, so the guard always bites somewhere.
    """
    fields = metadata.get("fields")
    if not isinstance(fields, list):
        return []
    return [str(field.get("name")) for field in fields if isinstance(field, dict)]


def require_columns(
    ref: ReportRef,
    declared: Sequence[str],
    required: Sequence[str],
    *,
    source: str,
) -> None:
    """Refuse unless ``declared`` covers every one of ``required``.

    ``source`` names where the column list came from (the metadata document or
    the first data page) so the failure message says which half disagreed.
    Nothing is checked when ``declared`` is empty — see :func:`field_names`.
    """
    if not declared:
        return
    missing = [column for column in required if column not in set(declared)]
    if not missing:
        return
    raise ReportColumnsMismatchError(
        f"the report named {ref.name!r} (category {ref.category_id}/report "
        f"{ref.report_id}) does not have the column(s) {', '.join(missing)} "
        f"that this tab is built from; its {source} declares "
        f"{', '.join(declared) or '(none)'}. Refusing to write the tab: every "
        "missing column would export as a blank money cell, which reads as "
        "'this contractor has no costs' rather than as an error. This is most "
        "likely a contractor-authored report that shares the built-in report's "
        "name and carries no marker saying so."
    )


def fetch_report_rows(
    client: ServiceTitanClient,
    ref: ReportRef,
    *,
    parameters: list[dict[str, Any]],
    page_size: int = _PAGE_SIZE,
    required_columns: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Run the report and return every row as a dict keyed by field name.

    The response is columnar (``fields`` describe the columns, ``data`` is a list
    of positional lists), so it is zipped back into dicts here — the same shape
    ``st_cli.commands.reporting._report_rows_to_dicts`` produces, kept local so
    the exporter does not depend on a CLI presentation module.

    A 429 that survives the client's own backoff aborts the WHOLE pull rather
    than returning the pages fetched so far: a truncated cost report is worse
    than no cost report, because a half-written tab looks complete to whoever
    reads it. ``run.py`` leaves the previous tab and its `_meta` row untouched.
    """
    resource = f"report-category/{ref.category_id}/reports/{ref.report_id}/data"
    body = {"parameters": parameters}

    rows: list[dict[str, Any]] = []
    names: list[str] = []
    page = 1
    while True:
        try:
            # A read, despite the verb — the parameters simply don't fit a query
            # string. Never gate this behind a mutation guard or a dry-run check.
            #
            # `idempotent=True` says exactly that to the client's write-retry
            # gate: running a report a second time cannot create or mutate
            # anything, so a lost answer (a ReadTimeout is routine — a 90-day
            # report against a 30s client timeout) may be re-sent, as it was
            # before that gate existed. This is the ONLY place in the repo that
            # may pass it; a booking, a lead or a price push must not.
            envelope = client.post(
                MODULE,
                resource,
                json_body=body,
                params={"page": page, "pageSize": page_size},
                idempotent=True,
            )
        except RateLimitError as exc:
            raise ReportRateLimitedError(
                f"ServiceTitan rate-limited the {ref.name!r} report on page {page} "
                "(reporting is throttled to roughly one run of the same report per "
                "minute per tenant). Skipping this tab; the rest of the feed is "
                f"unaffected. Detail: {exc}"
            ) from exc

        if not names:
            names = [str(field.get("name")) for field in envelope.get("fields") or []]
            # Checked on the FIRST page, before a single row is kept: the
            # metadata GET and the data POST can disagree, and it is the data
            # response's own columns that decide what lands in the tab.
            #
            # The data page is the BACKSTOP and must never be a second deferral.
            # `field_names` treats metadata with no `fields` as "could not check
            # here"; if the data page ALSO declares none, there is nowhere left
            # to check, every row would zip to `{}`, and the tab would be written
            # with zero rows, a fresh `_meta` row and no recorded failure — the
            # exact silent-blank-money outcome this guard exists to prevent.
            if required_columns and not names:
                raise ReportColumnsMismatchError(
                    f"the report named {ref.name!r} (category {ref.category_id}/report "
                    f"{ref.report_id}) returned a data page that declared no fields, so "
                    "the column(s) this tab is built from "
                    f"({', '.join(required_columns)}) cannot be confirmed. Refusing to "
                    "write the tab: with no field names every row zips to nothing and "
                    "the tab would be written empty, which reads as 'this contractor "
                    "has no costs' rather than as an error."
                )
            require_columns(ref, names, required_columns, source="first data page")
        for data_row in envelope.get("data") or []:
            rows.append(dict(zip(names, data_row)))

        if not envelope.get("hasMore", False):
            break
        page += 1
        if page > _MAX_DATA_PAGES:
            raise ReportRateLimitedError(
                f"the {ref.name!r} report still reported hasMore after "
                f"{_MAX_DATA_PAGES} pages; stopping rather than hammering a "
                "throttled endpoint or writing an unbounded tab."
            )
    return rows


def report_metadata(client: ServiceTitanClient, ref: ReportRef) -> dict[str, Any]:
    """The report's own description of its fields and parameters."""
    result = client.get(MODULE, f"report-category/{ref.category_id}/reports/{ref.report_id}")
    return result if isinstance(result, dict) else {}


def _iter_categories(client: ServiceTitanClient) -> Iterator[dict[str, Any]]:
    yield from fetch_all(client, MODULE, _CATEGORIES_RESOURCE, page_size=_PAGE_SIZE)


def _iter_reports(client: ServiceTitanClient, category_id: str) -> Iterator[dict[str, Any]]:
    yield from fetch_all(
        client, MODULE, f"report-category/{category_id}/reports", page_size=_PAGE_SIZE
    )


def _normalize(name: Any) -> str:
    """Case-fold and collapse whitespace — nothing looser.

    This is the ENTIRE matching rule. It absorbs `"Job  Costing Summary"` and
    `"job costing summary"`, and nothing else: no substring match, no token
    subset, no edit distance. Anything looser lets a report called
    "Job Costing Summary (Dave's copy)" through.
    """
    if name is None:
        return ""
    return " ".join(str(name).split()).casefold()


#: Fields that, when truthy, mark a report as authored by the contractor rather
#: than shipped by ServiceTitan. ServiceTitan's exact spelling is unconfirmed
#: against a real tenant (see KNOWN_UNVERIFIED.md), so several plausible ones are
#: accepted — a false positive here costs a refusal (loud, recoverable), while a
#: false negative costs wrong money numbers (silent).
_CUSTOM_BOOLEAN_FIELDS: tuple[str, ...] = ("isCustom", "custom", "isUserDefined", "userDefined")
_CUSTOM_KIND_FIELDS: tuple[str, ...] = ("type", "reportType", "kind", "source")
_CUSTOM_KIND_VALUES: frozenset[str] = frozenset({"custom", "userdefined", "user-defined", "tenant"})


def _looks_custom(report: dict[str, Any]) -> bool:
    """True when the report record says, in any spelling, that it is user-authored."""
    for field in _CUSTOM_BOOLEAN_FIELDS:
        value = report.get(field)
        if isinstance(value, bool) and value:
            return True
    for field in _CUSTOM_KIND_FIELDS:
        value = report.get(field)
        if isinstance(value, str) and value.strip().casefold() in _CUSTOM_KIND_VALUES:
            return True
    return False
