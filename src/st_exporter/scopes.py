"""What a ServiceTitan **403** means for a feed: never bought, or taken away.

Every feed job in the caller workflow runs on its schedule for every connector.
Nothing in the YAML says which feeds a contractor bought, because the contractor
already said so: the boxes they ticked when they created the ServiceTitan app ARE
the statement. A repository variable repeating it was a second, hand-maintained
copy of the same fact in a different place, and the two drift — variable on plus
scope missing is a red run every hour forever; variable off plus scope granted is
a feed they paid for silently not running. The scope controls access. The variable
controlled nothing.

The one thing a variable did answer is the thing this module answers instead:

    A 403 on its own cannot tell "never bought" from "permission revoked".

`_meta` can, because the exporter already writes one row per output tab with the
`last_run_at` of the run that last refreshed it:

* **No `_meta` row for any of the feed's tabs** (or one with a blank
  `last_run_at`) -> the feed has never once worked -> the scope was never granted
  -> **skip quietly**. No tab is written; an absent tab means "not bought",
  matching the contract's rule that an absent thing is absent rather than empty.
  The run does not fail.
* **A `_meta` row with a past `last_run_at`** -> it worked before, so the tenant
  HAD this permission -> a 403 now means it was **revoked** -> **be loud**: a red
  `::error` annotation naming the feed and the permission, the feed's tabs left at
  their previous contents, and a non-zero exit so the run turns red.

Either way the previous `_meta` row is carried forward untouched, exactly as
``_TabGuard`` does for a failed tab. That evidence is the whole mechanism: delete
it and every later 403 reads as "never bought".

**A first-ever run therefore declares nothing revoked.** No feed has a `_meta` row
yet, so a 403 on run one is "not granted" — which is correct: there is genuinely
no evidence the feed ever worked. The evidence is per FEED and never global, so a
brand-new contractor who bought one product gets that product's feed running and
the other three quietly skipped, one feed at a time, with no run ever claiming a
revocation it cannot support.

And a real outage is never mistaken for "not bought": **only HTTP 403 takes this
path.** A 400 (see `KNOWN_UNVERIFIED.md` on `active=Any`), a 401, a 404, a 429 or
a transport error keeps the behaviour it has always had — the per-tab guard fails
that tab loudly, or the exception ends the run. Widening this to any error class
would turn an outage into a silent skip, which is the exact bug this branch exists
to eliminate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from st_cli.exceptions import APIError
from st_exporter.logging_setup import announce_to_actions, logger
from st_exporter.meta import MetaRow

#: Verdicts. Deliberately two words rather than a bool: "False" would read as
#: "fine", and half the point is that one of these is a failure.
NOT_GRANTED = "not_granted"
REVOKED = "revoked"

#: Feed -> the ServiceTitan permission a contractor has to tick for it. Named in
#: the annotation, because "the pricebook feed got a 403" is not actionable and
#: "tick Pricebook -> Read" is.
FEED_PERMISSIONS: dict[str, str] = {
    "jobs": "CRM (Customers, Locations) + Jobs (Jobs, Appointments, Job Types)",
    "technicians": "Settings -> Technicians, Business Units",
    "pricebook": "Pricebook -> Services, Equipment, Materials, Categories",
    "financial": "Accounting -> Invoices, Payroll -> Timesheets, Reporting -> Reports",
}


def is_permission_denied(exc: BaseException) -> bool:
    """True only for an HTTP **403** from ServiceTitan.

    Not 401 (bad credentials — the client already retried that once with a fresh
    token), not 404, not 429, not a transport error. Authorization only.
    """
    return isinstance(exc, APIError) and exc.status_code == 403


def feed_ever_ran(meta_rows: Mapping[str, MetaRow], tab_names: Sequence[str]) -> bool:
    """Has any tab of this feed ever been written by a successful run?

    A row with a blank ``last_run_at`` is not evidence: it never named a run.
    """
    return any(tab in meta_rows and meta_rows[tab].last_run_at.strip() for tab in tab_names)


class ScopeLedger:
    """Classifies each feed's 403 once, and carries its `_meta` rows forward.

    One per run. ``deny`` is called from wherever a feed's fetch can fail — the
    bare try/except around the jobs and technicians feeds, and ``_TabGuard`` for
    the two multi-tab feeds — and answers the only question that matters: is this
    a permission the tenant never had, or one they have lost?
    """

    def __init__(
        self,
        *,
        meta_rows: Mapping[str, MetaRow],
        new_meta_rows: list[MetaRow],
    ) -> None:
        self._meta_rows = meta_rows
        self._new_meta_rows = new_meta_rows
        self._carried: set[str] = set()
        #: feed -> the permission it needs. Quiet: logged and named in the run's
        #: summary line, no annotation, exit code unchanged.
        self.not_granted: dict[str, str] = {}
        #: feed -> ServiceTitan's own 403 detail. Loud: `::error` annotation, and
        #: the run exits non-zero.
        self.revoked: dict[str, str] = {}

    def denied(self, feed: str) -> bool:
        """Has this feed already been ruled out this run, either way?"""
        return feed in self.not_granted or feed in self.revoked

    def deny(self, feed: str, tab_names: Sequence[str], exc: BaseException) -> str | None:
        """Classify ``exc``. Returns the verdict, or ``None`` if it is not a 403.

        ``None`` means "not mine" and the caller must handle the exception the way
        it always has — that is what keeps a 400/429/transport error from being
        swallowed as "not bought".

        Carries every existing `_meta` row of the feed forward before returning,
        so a skipped feed never loses the evidence that it once worked.
        """
        if not is_permission_denied(exc):
            return None
        verdict = REVOKED if feed_ever_ran(self._meta_rows, tab_names) else NOT_GRANTED
        if not self.denied(feed):
            self._announce(feed, verdict, exc)
        for tab in tab_names:
            self.carry_forward(tab)
        return verdict

    def carry_forward(self, tab_name: str) -> None:
        """Append this tab's previous `_meta` row unchanged, at most once."""
        if tab_name in self._carried:
            return
        self._carried.add(tab_name)
        if tab_name in self._meta_rows:
            self._new_meta_rows.append(self._meta_rows[tab_name])

    def _announce(self, feed: str, verdict: str, exc: BaseException) -> None:
        permission = FEED_PERMISSIONS.get(feed, feed)
        if verdict == NOT_GRANTED:
            self.not_granted[feed] = permission
            # INFO, no annotation, no failure. This is the ordinary state of a
            # feed belonging to a product the contractor did not buy, on a
            # connector that runs every feed job on its schedule. It is still
            # named in the run's summary line, so "no pricebook tab" is always a
            # stated fact rather than an absence somebody has to notice.
            logger.info(
                "%s: ServiceTitan answered 403 and this feed has never run on this "
                "Sheet, so the tenant's app was never granted %s. Skipping quietly: "
                "no tab is written, and an absent tab means the product was not "
                "bought. Not a failure.",
                feed,
                permission,
            )
            return
        self.revoked[feed] = str(exc)
        logger.error(
            "SCOPE REVOKED — the `%s` feed has run successfully on this Sheet before, "
            "so the tenant HAD %s, and ServiceTitan is now answering 403 (%s). This is "
            "a permission that was taken away, not a product that was never bought. "
            "The feed's tabs were NOT refreshed and keep last run's contents; their "
            "_meta rows are unchanged so `last_run_at` still says when they were last "
            "genuinely current.",
            feed,
            permission,
            exc,
        )
        announce_to_actions(
            "ServiceTitan permission revoked",
            (
                f"The `{feed}` feed worked before and is now refused (403). The tenant's "
                f"ServiceTitan app has lost: {permission}. Its tabs still hold last run's "
                f"data and are going stale every cycle until the permission is restored."
            ),
            level="error",
        )
