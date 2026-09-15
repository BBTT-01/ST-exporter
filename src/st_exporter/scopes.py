"""What a ServiceTitan **403** means for one output TAB: never bought, or taken away.

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

* **No `_meta` row for this tab** (or one with a blank `last_run_at`), and no such
  tab in the Sheet -> it has never once been written -> the entity was never
  granted -> **skip quietly**. No tab is written; an absent tab means "not
  bought", matching the contract's rule that an absent thing is absent rather
  than empty. The run does not fail.
* **A `_meta` row with a past `last_run_at`** (or, failing that, an existing tab)
  -> it worked before, so the tenant HAD this permission -> a 403 now means it
  was **revoked** -> **be loud**: a red `::error` annotation naming the tab and
  the permission, the tab left at its previous contents, and a non-zero exit so
  the run turns red.

Either way that tab's previous `_meta` row is carried forward untouched, exactly
as ``_TabGuard`` does for a failed tab. That evidence is the whole mechanism:
delete it and every later 403 reads as "never bought".

**The unit is the TAB, not the feed.** ServiceTitan grants permissions per
*entity* — `Pricebook -> Materials` is its own tick-box, separate from
`Pricebook -> Services`, `-> Equipment` and `-> Categories`, and `Reporting` is
its own section entirely. The team's own runbook proves it: the TrueQuote block
grants Services, Equipment, Categories and Images and deliberately omits
Materials, which only arrives when the contractor buys Profit Wizard. So "one tab
of a feed is refused while its siblings are read perfectly well" is not an edge
case — it is the **ordinary** state of a TrueQuote-only tenant, and of every
Profit Wizard tenant that missed the Reporting permission. Classifying a 403 per
feed would cost that tenant the tabs it was granted, skip its image pass, and red
every run forever. So a 403 rules out exactly the tab that earned it, the feed's
remaining tabs are still attempted, and each is judged on the evidence for
itself.

**A first-ever run therefore declares nothing revoked.** No tab has a `_meta` row
yet and no tab exists, so a 403 on run one is "not granted" — which is correct:
there is genuinely no evidence it ever worked.

And a real outage is never mistaken for "not bought": **only HTTP 403 takes this
path.** A 400 (see `KNOWN_UNVERIFIED.md` on `active=Any`), a 401, a 404, a 429 or
a transport error keeps the behaviour it has always had — the per-tab guard fails
that tab loudly, or the exception ends the run. Widening this to any error class
would turn an outage into a silent skip, which is the exact bug this branch exists
to eliminate.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from st_cli.exceptions import APIError
from st_exporter.logging_setup import announce_to_actions, logger
from st_exporter.meta import MetaRow, MetaRowSet

#: Verdicts. Deliberately two words rather than a bool: "False" would read as
#: "fine", and half the point is that one of these is a failure.
NOT_GRANTED = "not_granted"
REVOKED = "revoked"

#: Output tab -> the ServiceTitan permission a contractor has to tick for it.
#: Keyed by TAB because that is how ServiceTitan grants: `Pricebook -> Materials`
#: is a different box from `Pricebook -> Services`, and `Reporting` is a section
#: of its own. Named in the annotation, because "the pricebook feed got a 403" is
#: not actionable and "tick Pricebook -> Materials" is.
#:
#: Every string here is what the code actually calls, not what the feed is named
#: after: `jobs` denormalises five exports plus two reference lookups, so a 403
#: anywhere in it can be Settings -> Business Units, and `technicians` reads
#: `settings/technicians` and nothing else. `run.EXPORT_TABS` and a test keep
#: this dict and the tabs the exporter writes in step.
TAB_PERMISSIONS: dict[str, str] = {
    "jobs": (
        "CRM (Customers, Locations) + JPM (Jobs, Appointments, Job Types) + "
        "Dispatch (Appointment Assignments) + Settings (Business Units)"
    ),
    "technicians": "Settings -> Technicians",
    "pricebook.services": "Pricebook -> Services",
    "pricebook.equipment": "Pricebook -> Equipment",
    "pricebook.materials": "Pricebook -> Materials",
    "pricebook.categories": "Pricebook -> Categories",
    "accounting.invoices": "Accounting -> Invoices",
    "payroll.timesheets": "Payroll -> Timesheets (plus JPM -> Jobs, to list the completed jobs)",
    "settings.businessUnits": "Settings -> Business Units",
    "reporting.jobCosts": "Reporting -> Reports (report categories, reports, report data)",
}


def is_permission_denied(exc: BaseException) -> bool:
    """True only for an HTTP **403** from ServiceTitan.

    Not 401 (bad credentials — the client already retried that once with a fresh
    token), not 404, not 429, not a transport error. Authorization only.
    """
    return isinstance(exc, APIError) and exc.status_code == 403


def feed_ever_ran(meta_rows: Mapping[str, MetaRow], tab_names: Sequence[str]) -> bool:
    """Has any of these tabs ever been written by a successful run?

    A row with a blank ``last_run_at`` is not evidence: it never named a run.

    Callers pass a ONE-tab sequence — the evidence that decides a 403 is the
    evidence for the tab that earned it. The signature stays a sequence because
    "any of these" is the honest predicate and a caller with a genuinely
    single-tab feed (`jobs`, `technicians`) reads identically.
    """
    return any(tab in meta_rows and meta_rows[tab].last_run_at.strip() for tab in tab_names)


class ScopeLedger:
    """Classifies each TAB's 403 once, and carries that tab's `_meta` row forward.

    One per run. ``deny`` is called from wherever a tab's fetch can fail — the
    bare try/except around the single-tab jobs and technicians feeds, and
    ``_TabGuard`` for the two multi-tab ones — and answers the only question that
    matters: is this a permission the tenant never had, or one they have lost?
    """

    def __init__(
        self,
        *,
        meta_rows: Mapping[str, MetaRow],
        new_meta_rows: MetaRowSet,
        tab_exists: Callable[[str], bool] | None = None,
    ) -> None:
        self._meta_rows = meta_rows
        self._new_meta_rows = new_meta_rows
        # Second-line evidence, consulted only when `_meta` has nothing to say.
        # `SheetsClient.read_grid` answers `[]` for a tab that is not there, so a
        # `_meta` tab somebody deleted or renamed would otherwise turn every
        # later 403 into "never bought" — the dangerous direction, because it is
        # the silent one. An export tab that EXISTS is evidence a run once wrote
        # it, whatever state `_meta` is in. Called lazily, and only on a 403 with
        # no `_meta` row, so it costs nothing on an ordinary run.
        self._tab_exists = tab_exists
        #: Tab -> the permission it needs. Quiet: logged and named in the run's
        #: summary line, no annotation, exit code unchanged.
        self.not_granted: dict[str, str] = {}
        #: Tab -> ServiceTitan's own 403 detail. Loud: `::error` annotation, and
        #: the run exits non-zero.
        self.revoked: dict[str, str] = {}

    def denied(self, tab_name: str) -> bool:
        """Has this tab already been ruled out this run, either way?"""
        return tab_name in self.not_granted or tab_name in self.revoked

    def deny(self, tab_name: str, exc: BaseException) -> str | None:
        """Classify ``exc`` for one tab. Returns the verdict, or ``None`` if it is
        not a 403.

        ``None`` means "not mine" and the caller must handle the exception the way
        it always has — that is what keeps a 400/429/transport error from being
        swallowed as "not bought".

        Carries this tab's existing `_meta` row forward before returning, so a
        skipped tab never loses the evidence that it once worked. It carries
        nothing over a tab that already succeeded this run: ``MetaRowSet.carry``
        never displaces a fresh row, which is what keeps `_meta` at exactly one
        row per tab.
        """
        if not is_permission_denied(exc):
            return None
        verdict = REVOKED if self._ever_ran(tab_name) else NOT_GRANTED
        if not self.denied(tab_name):
            self._announce(tab_name, verdict, exc)
        self.carry_forward(tab_name)
        return verdict

    def carry_forward(self, tab_name: str) -> None:
        """Append this tab's previous `_meta` row unchanged, at most once, and
        never over a row this run has already written fresh."""
        previous = self._meta_rows.get(tab_name)
        if previous is not None:
            self._new_meta_rows.carry(previous)

    def _ever_ran(self, tab_name: str) -> bool:
        if feed_ever_ran(self._meta_rows, (tab_name,)):
            return True
        return self._tab_exists is not None and self._tab_exists(tab_name)

    def _announce(self, tab_name: str, verdict: str, exc: BaseException) -> None:
        permission = TAB_PERMISSIONS.get(tab_name, tab_name)
        if verdict == NOT_GRANTED:
            self.not_granted[tab_name] = permission
            # INFO, no annotation, no failure. This is the ordinary state of a
            # tab belonging to an entity the contractor's app was never granted —
            # a TrueQuote-only tenant has no `Pricebook -> Materials` and never
            # will. It is still named in the run's summary line, so "no materials
            # tab" is always a stated fact rather than an absence somebody has to
            # notice.
            logger.info(
                "%s: ServiceTitan answered 403 and this tab has never been written on "
                "this Sheet, so the tenant's app was never granted %s. Skipping "
                "quietly: no tab is written, and an absent tab means the product was "
                "not bought. Not a failure, and the feed's other tabs still run.",
                tab_name,
                permission,
            )
            return
        self.revoked[tab_name] = str(exc)
        logger.error(
            "SCOPE REVOKED — the `%s` tab has been written successfully on this Sheet "
            "before, so the tenant HAD %s, and ServiceTitan is now answering 403 (%s). "
            "This is a permission that was taken away, not a product that was never "
            "bought. The tab was NOT refreshed and keeps last run's contents; its "
            "_meta row is unchanged so `last_run_at` still says when it was last "
            "genuinely current.",
            tab_name,
            permission,
            exc,
        )
        announce_to_actions(
            "ServiceTitan permission revoked",
            (
                f"The `{tab_name}` tab was written before and is now refused (403). The "
                f"tenant's ServiceTitan app has lost: {permission}. That tab still holds "
                f"last run's data and is going stale every cycle until the permission is "
                f"restored."
            ),
            level="error",
        )
