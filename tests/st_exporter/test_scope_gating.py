"""A 403 per TAB: never granted (quiet) or revoked (loud), decided from `_meta`.

The caller workflow no longer carries a repository variable per feed. Every feed
job runs on its schedule and ServiceTitan's scopes decide what exports, so the
exporter has to answer the one question the variables were defending against:

    does this 403 mean "they never bought it" or "the permission was taken away"?

`_meta` answers it, because it already records the last successful run of every
tab. These tests hold that line from both sides — the quiet skip must stay quiet
and tab-free, the revocation must stay loud and must never lose the evidence that
made it decidable, and NOTHING but a 403 may take either path.

**And the unit is the TAB.** ServiceTitan grants per entity: `Pricebook ->
Materials` is its own tick-box, and the team's runbook deliberately omits it from
the TrueQuote block because Materials only arrives with Profit Wizard. So "one tab
403s while its siblings answer 200" is not an edge case to be tolerated — it is
the ordinary, every-cycle state of a TrueQuote-only tenant, and of every Profit
Wizard tenant that missed the Reporting permission. A per-FEED verdict cost those
tenants tabs they had paid for, skipped their images entirely, duplicated their
`_meta` rows and reddened every run forever. Half the tests below 403 the LAST tab
of a multi-tab feed, precisely because only 403ing the first one is what let that
ship.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest
import respx

from st_cli.exceptions import APIError, RateLimitError, STCLIError, TransportError
from st_exporter.meta import MetaRow, MetaRowSet, build_meta_grid, parse_meta_grid
from st_exporter.run import (
    EXPORT_FEEDS,
    EXPORT_TABS,
    FINANCIAL_FEED_NAMES,
    OUTBOX_FEED,
    PRICEBOOK_FEED_NAMES,
    run_export,
)
from st_exporter.scopes import (
    NOT_GRANTED,
    REVOKED,
    TAB_PERMISSIONS,
    ScopeLedger,
    feed_ever_ran,
    is_permission_denied,
)
from st_exporter.sheets import InMemorySheetsStore
from tests.st_exporter.conftest import mock_auth_token
from tests.st_exporter.fixtures import tenant_financial, tenant_pricebook, tenant_run1

FIXED_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
EARLIER = "2026-09-13T12:00:00+00:00"
TENANT_ID = 12345


def _frozen_now():
    return patch("st_exporter.run.datetime", **{"now.return_value": FIXED_NOW})


def _forbidden(api_base: str, path: str) -> None:
    """Make one endpoint answer 403 — ServiceTitan's "scope validation failed"."""
    respx.get(f"{api_base.rstrip('/')}/{path}").mock(
        return_value=httpx.Response(403, text="Scope validation failed")
    )


def _status(api_base: str, path: str, status: int) -> None:
    respx.get(f"{api_base.rstrip('/')}/{path}").mock(return_value=httpx.Response(status, text="no"))


#: The request that earns each TAB its 403 — the first call that tab makes which
#: no sibling tab of the same feed also makes. Registered AFTER the happy-path
#: fixtures: respx keys routes by pattern, so re-registering one replaces it.
#:
#: `payroll.timesheets` is keyed on one job's timesheet URL rather than the job
#: list it is built from, because the job list is JPM and the permission being
#: modelled is Payroll. `reporting.jobCosts` is keyed on `report-categories`,
#: which is the first thing the Reporting section gates.
TAB_FIRST_CALL = {
    "jobs": f"crm/v2/tenant/{TENANT_ID}/export/customers",
    "technicians": f"settings/v2/tenant/{TENANT_ID}/technicians",
    "pricebook.services": f"pricebook/v2/tenant/{TENANT_ID}/services",
    "pricebook.equipment": f"pricebook/v2/tenant/{TENANT_ID}/equipment",
    "pricebook.materials": f"pricebook/v2/tenant/{TENANT_ID}/materials",
    "pricebook.categories": f"pricebook/v2/tenant/{TENANT_ID}/categories",
    "accounting.invoices": f"accounting/v2/tenant/{TENANT_ID}/invoices",
    "payroll.timesheets": f"payroll/v2/tenant/{TENANT_ID}/jobs/7/timesheets",
    "settings.businessUnits": f"settings/v2/tenant/{TENANT_ID}/business-units",
    "reporting.jobCosts": f"reporting/v2/tenant/{TENANT_ID}/report-categories",
    "sales.estimates": f"sales/v2/tenant/{TENANT_ID}/estimates",
}

#: Which feed has to be selected for a tab to be attempted at all.
TAB_FEED = {
    "jobs": "jobs",
    "technicians": "technicians",
    **{tab: "pricebook" for tab in PRICEBOOK_FEED_NAMES},
    **{tab: "financial" for tab in FINANCIAL_FEED_NAMES},
}

#: Every tab a feed writes.
FEED_TABS = {
    "jobs": ("jobs",),
    "technicians": ("technicians",),
    "pricebook": PRICEBOOK_FEED_NAMES,
    "financial": FINANCIAL_FEED_NAMES,
}

#: The tab each multi-tab feed attempts LAST. 403ing the first tab of a feed is
#: the scenario that hid the per-feed bug for a whole review round: a first-tab
#: 403 short-circuited every sibling, so "the siblings were lost" and "the
#: siblings were never tried" looked identical. These are the ones that tell them
#: apart.
LAST_TAB = {"pricebook": PRICEBOOK_FEED_NAMES[-1], "financial": FINANCIAL_FEED_NAMES[-1]}

MULTI_TAB_FEEDS = sorted(LAST_TAB)


def _register_tenant(api_base: str) -> None:
    """Every route every feed needs, all answering happily."""
    tenant_run1.register(
        api_base, today_iso=FIXED_NOW.isoformat(), far_past_iso="2020-01-01T00:00:00Z"
    )
    tenant_pricebook.register(api_base)
    tenant_financial.register(api_base)


def _seeded_store(*feeds: str) -> InMemorySheetsStore:
    """An Export Store whose `_meta` says these feeds ran successfully yesterday."""
    store = InMemorySheetsStore()
    rows = [
        MetaRow(
            feed=tab,
            last_run_at=EARLIER,
            row_count=7,
            exporter_version="0.2.8",
            contract_version="x.v1",
        )
        for feed in feeds
        for tab in FEED_TABS[feed]
    ]
    store.replace_grid("_meta", build_meta_grid(rows))
    for feed in feeds:
        for tab in FEED_TABS[feed]:
            store.replace_grid(tab, [["header"], ["yesterday"]])
    return store


def _run(st_settings, exporter_settings, export_store, *feeds: str):
    with _frozen_now():
        return run_export(
            st_settings,
            exporter_settings,
            feeds=frozenset(feeds),
            export_store=export_store,
            raw_cache_store=InMemorySheetsStore(),
        )


def _meta_feeds(store: InMemorySheetsStore) -> list[str]:
    """Every `feed` cell in the written `_meta` grid, duplicates included."""
    header, *data = store.tabs["_meta"]
    return [row[0] for row in data if row and row[0]]


# ---------------------------------------------------------------------------
# The classifier itself.
# ---------------------------------------------------------------------------


class TestWhatCountsAsNotBought:
    """Only an authorization failure may ever mean "they did not buy this".

    Widening this is the one change that would turn a real outage — a 400 on a
    filter ServiceTitan changed, a 429 storm, a DNS failure — into a feed that
    silently stops exporting on green runs. That is the exact bug class this
    branch exists to eliminate, arriving through the door built to prevent it.
    """

    def test_a_403_is_a_permission_answer(self) -> None:
        assert is_permission_denied(APIError(403, "Scope validation failed"))

    @pytest.mark.parametrize("status", [400, 401, 404, 409, 429, 500, 503])
    def test_no_other_status_is(self, status: int) -> None:
        assert not is_permission_denied(APIError(status, "nope"))

    def test_a_429_is_not_even_though_it_subclasses_api_error(self) -> None:
        assert not is_permission_denied(RateLimitError())

    def test_a_transport_error_is_not(self) -> None:
        """No HTTP status at all. A refused connection is not a purchase decision."""
        assert not is_permission_denied(TransportError("connection refused"))

    def test_a_plain_stcli_error_is_not(self) -> None:
        assert not is_permission_denied(STCLIError("something else"))


class TestEvidenceOfAPastRun:
    def test_no_row_at_all_is_no_evidence(self) -> None:
        assert not feed_ever_ran({}, ("jobs",))

    def test_a_row_with_a_past_run_is_evidence(self) -> None:
        rows = {"jobs": MetaRow(feed="jobs", last_run_at=EARLIER)}
        assert feed_ever_ran(rows, ("jobs",))

    def test_a_blank_last_run_at_is_not_evidence(self) -> None:
        """A row that never named a run cannot prove the feed ever worked — so a
        403 beside it is "never granted", not a revocation nobody can support."""
        rows = {"jobs": MetaRow(feed="jobs", last_run_at="   ")}
        assert not feed_ever_ran(rows, ("jobs",))

    def test_a_sibling_tabs_row_is_not_evidence_for_this_one(self) -> None:
        """The correction. ServiceTitan grants `Pricebook -> Materials`, NOT
        `Pricebook`: the runbook's TrueQuote block ticks Services, Equipment and
        Categories and deliberately leaves Materials out. So a written
        `pricebook.categories` proves nothing whatever about Materials, and
        reading it as proof is what turned an ordinary TrueQuote-only tenant into
        a red run every hour."""
        rows = {"pricebook.categories": MetaRow(feed="pricebook.categories", last_run_at=EARLIER)}
        assert not feed_ever_ran(rows, ("pricebook.materials",))
        assert feed_ever_ran(rows, ("pricebook.categories",))


class TestTheLedger:
    def _ledger(self, meta_rows=None, tab_exists=None):
        new = MetaRowSet()
        return (
            ScopeLedger(meta_rows=meta_rows or {}, new_meta_rows=new, tab_exists=tab_exists),
            new,
        )

    def test_a_first_ever_403_is_not_granted(self) -> None:
        ledger, _ = self._ledger()
        assert ledger.deny("pricebook.materials", APIError(403, "x")) == NOT_GRANTED
        assert "pricebook.materials" in ledger.not_granted
        assert not ledger.revoked

    def test_a_403_after_a_good_run_is_revoked(self) -> None:
        rows = {"jobs": MetaRow(feed="jobs", last_run_at=EARLIER)}
        ledger, _ = self._ledger(rows)
        assert ledger.deny("jobs", APIError(403, "x")) == REVOKED
        assert "jobs" in ledger.revoked
        assert not ledger.not_granted

    def test_one_tabs_verdict_does_not_decide_its_siblings(self) -> None:
        """`pricebook.categories` has run before; `pricebook.materials` never has.
        Both are refused in the same run, and they get different verdicts — which
        is the whole point of classifying per tab."""
        rows = {"pricebook.categories": MetaRow(feed="pricebook.categories", last_run_at=EARLIER)}
        ledger, _ = self._ledger(rows)
        assert ledger.deny("pricebook.materials", APIError(403, "x")) == NOT_GRANTED
        assert ledger.deny("pricebook.categories", APIError(403, "x")) == REVOKED
        assert set(ledger.not_granted) == {"pricebook.materials"}
        assert set(ledger.revoked) == {"pricebook.categories"}

    def test_anything_but_a_403_is_handed_straight_back(self) -> None:
        ledger, new = self._ledger({"jobs": MetaRow(feed="jobs", last_run_at=EARLIER)})
        assert ledger.deny("jobs", RateLimitError()) is None
        assert not ledger.denied("jobs")
        # And it did NOT carry anything forward: the caller still owns that row.
        assert list(new) == []

    def test_the_previous_meta_row_is_carried_forward_when_revoked(self) -> None:
        row = MetaRow(feed="jobs", last_run_at=EARLIER, row_count=9)
        ledger, new = self._ledger({"jobs": row})
        ledger.deny("jobs", APIError(403, "x"))
        assert list(new) == [row]

    def test_carrying_forward_is_idempotent(self) -> None:
        """A retry loop must not grow four copies of one row."""
        row = MetaRow(feed="pricebook.services", last_run_at=EARLIER)
        ledger, new = self._ledger({"pricebook.services": row})
        for _ in range(4):
            ledger.deny("pricebook.services", APIError(403, "x"))
        assert list(new) == [row]

    def test_it_never_carries_over_a_row_this_run_already_wrote(self) -> None:
        """The `_meta` corruption, at its source. A tab that succeeded holds a
        FRESH row; nothing a sibling's 403 does may put yesterday's row back."""
        old = MetaRow(feed="pricebook.categories", last_run_at=EARLIER, row_count=7)
        fresh = MetaRow(feed="pricebook.categories", last_run_at="2026-09-14", row_count=3)
        ledger, new = self._ledger({"pricebook.categories": old})
        new.add(fresh)
        ledger.carry_forward("pricebook.categories")
        assert list(new) == [fresh]

    def test_a_tab_with_no_previous_row_carries_nothing(self) -> None:
        ledger, new = self._ledger()
        ledger.deny("pricebook.materials", APIError(403, "x"))
        assert list(new) == []

    def test_an_existing_tab_is_evidence_when_meta_says_nothing(self) -> None:
        """Finding 3. `read_grid` answers `[]` for a tab that is not there, so a
        `_meta` tab somebody deleted or renamed would silently turn every later
        403 into "never bought" — the quiet, wrong direction. An export tab that
        EXISTS is evidence a run once wrote it, whatever state `_meta` is in."""
        ledger, _ = self._ledger(tab_exists=lambda tab: tab == "pricebook.services")
        assert ledger.deny("pricebook.services", APIError(403, "x")) == REVOKED
        assert ledger.deny("pricebook.materials", APIError(403, "x")) == NOT_GRANTED


class TestThePermissionStrings:
    """An annotation that names the wrong box sends a contractor to tick things
    they already have, and leaves the one they are missing unticked."""

    def test_every_tab_the_exporter_writes_declares_one(self) -> None:
        assert set(TAB_PERMISSIONS) == set(EXPORT_TABS)

    def test_the_jobs_permission_names_settings_business_units(self) -> None:
        """Finding 2. A jobs-feed 403 is just as likely to come from the two
        REFERENCE lookups (`settings/business-units`, `jpm/job-types`) as from the
        five exports, and the old string named neither."""
        assert "Business Units" in TAB_PERMISSIONS["jobs"]
        assert "Job Types" in TAB_PERMISSIONS["jobs"]

    def test_the_technicians_permission_names_only_what_it_calls(self) -> None:
        """`fetch_technicians` calls `settings/technicians` and nothing else. The
        old string also named Business Units, which this feed never reads."""
        assert TAB_PERMISSIONS["technicians"] == "Settings -> Technicians"

    def test_each_pricebook_tab_names_its_own_entity(self) -> None:
        assert TAB_PERMISSIONS["pricebook.materials"] == "Pricebook -> Materials"
        assert TAB_PERMISSIONS["pricebook.services"] == "Pricebook -> Services"
        assert "Materials" not in TAB_PERMISSIONS["pricebook.services"]

    def test_the_job_costs_tab_names_reporting_and_nothing_else(self) -> None:
        assert TAB_PERMISSIONS["reporting.jobCosts"].startswith("Reporting ->")


# ---------------------------------------------------------------------------
# Through the whole run.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tab", sorted(EXPORT_TABS))
class TestATabThatWasNeverGranted:
    """The ordinary state of a tab belonging to an entity this contractor's app
    does not cover, on a connector that runs all four feed jobs for everybody."""

    @respx.mock
    def test_it_writes_no_tab_and_does_not_fail(self, tab, st_settings, exporter_settings) -> None:
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, TAB_FIRST_CALL[tab])
        store = InMemorySheetsStore()

        summary = _run(st_settings, exporter_settings, store, TAB_FEED[tab])

        assert tab in summary.scope_not_granted
        assert not summary.scope_revoked
        assert tab not in store.tabs, "an absent tab is how the contract says 'not bought'"

    @respx.mock
    def test_it_costs_its_siblings_nothing(self, tab, st_settings, exporter_settings) -> None:
        """The regression, stated once. A denied Materials must not cost
        Categories: they are different tick-boxes, and the tenant paid for one."""
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, TAB_FIRST_CALL[tab])
        store = InMemorySheetsStore()

        _run(st_settings, exporter_settings, store, TAB_FEED[tab])

        siblings = [sibling for sibling in FEED_TABS[TAB_FEED[tab]] if sibling != tab]
        for sibling in siblings:
            assert sibling in store.tabs, f"{sibling} was granted and must still be written"
            assert parse_meta_grid(store.tabs["_meta"])[sibling].last_run_at == (
                FIXED_NOW.isoformat()
            )

    @respx.mock
    def test_it_names_the_permission_in_the_log_and_stays_at_info(
        self, tab, st_settings, exporter_settings, caplog
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, TAB_FIRST_CALL[tab])
        with caplog.at_level(logging.INFO, logger="st_exporter"):
            _run(st_settings, exporter_settings, InMemorySheetsStore(), TAB_FEED[tab])
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        skipped = [r.getMessage() for r in caplog.records if "Skipping quietly" in r.getMessage()]
        assert skipped
        assert TAB_PERMISSIONS[tab] in skipped[0]

    @respx.mock
    def test_it_annotates_nothing(
        self, tab, st_settings, exporter_settings, monkeypatch, capsys
    ) -> None:
        """Quiet means quiet. A `::warning` per tab per cycle, forever, on every
        connector whose contractor bought one product, is noise that trains
        everybody to ignore the channel the revocation needs."""
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, TAB_FIRST_CALL[tab])
        _run(st_settings, exporter_settings, InMemorySheetsStore(), TAB_FEED[tab])
        assert not [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::")]


@pytest.mark.parametrize("tab", sorted(EXPORT_TABS))
class TestATabWhosePermissionWasRevoked:
    """It worked before. `_meta` proves it, which is the whole reason that row is
    carried forward rather than dropped when a tab does not run."""

    @respx.mock
    def test_it_is_loud_and_leaves_the_previous_tab_alone(
        self, tab, st_settings, exporter_settings, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, TAB_FIRST_CALL[tab])
        feed = TAB_FEED[tab]
        store = _seeded_store(feed)

        summary = _run(st_settings, exporter_settings, store, feed)

        assert tab in summary.scope_revoked
        assert not summary.scope_not_granted
        annotations = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::")]
        assert annotations, "a green run is a run nobody opens"
        assert annotations[0].startswith("::error title=ServiceTitan permission revoked::")
        assert tab in annotations[0]
        assert store.tabs[tab] == [["header"], ["yesterday"]]
        # ...and only that tab. Its siblings were granted and are refreshed.
        for sibling in FEED_TABS[feed]:
            if sibling != tab:
                assert store.tabs[sibling] != [["header"], ["yesterday"]]

    @respx.mock
    def test_the_meta_row_is_carried_forward_unchanged(
        self, tab, st_settings, exporter_settings
    ) -> None:
        """Never delete the evidence. It is what makes the NEXT run's 403
        decidable — drop it once and a revocation reads as "never bought" from
        then on, forever."""
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, TAB_FIRST_CALL[tab])
        feed = TAB_FEED[tab]
        store = _seeded_store(feed)

        _run(st_settings, exporter_settings, store, feed)

        meta = parse_meta_grid(store.tabs["_meta"])
        assert meta[tab].last_run_at == EARLIER, "last_run_at must still name the last GOOD run"
        assert meta[tab].row_count == 7
        for sibling in FEED_TABS[feed]:
            if sibling != tab:
                assert meta[sibling].last_run_at == FIXED_NOW.isoformat()

    @respx.mock
    def test_it_names_the_tab_and_the_permission_at_error(
        self, tab, st_settings, exporter_settings, caplog
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, TAB_FIRST_CALL[tab])
        with caplog.at_level(logging.ERROR, logger="st_exporter"):
            _run(st_settings, exporter_settings, _seeded_store(TAB_FEED[tab]), TAB_FEED[tab])
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any(
            "SCOPE REVOKED" in message and tab in message and TAB_PERMISSIONS[tab] in message
            for message in errors
        )


@pytest.mark.parametrize("feed", MULTI_TAB_FEEDS)
class TestTheLastTabOfAFeed:
    """Every through-the-run test used to 403 the FIRST call of a feed, which is
    precisely why a per-feed verdict looked correct: a first-tab 403 skipped the
    siblings, so nobody could see them being lost. These 403 the last tab, with
    every sibling answering 200 before it."""

    @respx.mock
    def test_the_siblings_that_ran_first_are_all_written(
        self, feed, st_settings, exporter_settings
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        last = LAST_TAB[feed]
        _forbidden(st_settings.api_base, TAB_FIRST_CALL[last])
        store = InMemorySheetsStore()

        summary = _run(st_settings, exporter_settings, store, feed)

        assert set(summary.scope_not_granted) == {last}
        for sibling in FEED_TABS[feed]:
            assert (sibling in store.tabs) is (sibling != last)

    @respx.mock
    def test_meta_holds_exactly_one_row_per_tab(self, feed, st_settings, exporter_settings) -> None:
        """The `_meta` corruption, end to end. A success appended a fresh row and
        the later 403 carried the OLD row forward for every tab of the feed — so
        a tab holding run-3 data answered with run-1's `last_run_at` forever, and
        `docs/export-contract.md` tells consumers to read exactly that cell."""
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        last = LAST_TAB[feed]
        _forbidden(st_settings.api_base, TAB_FIRST_CALL[last])
        store = _seeded_store(feed)

        _run(st_settings, exporter_settings, store, feed)

        written = _meta_feeds(store)
        assert len(written) == len(set(written)), f"duplicate _meta rows: {written}"
        meta = parse_meta_grid(store.tabs["_meta"])
        for tab in FEED_TABS[feed]:
            expected = EARLIER if tab == last else FIXED_NOW.isoformat()
            assert meta[tab].last_run_at == expected

    @respx.mock
    def test_a_never_granted_last_tab_keeps_the_run_green_every_cycle(
        self, feed, st_settings, exporter_settings
    ) -> None:
        """Run 2, run 3, run N. The tab was never granted on run 1 and is still
        not granted now; nothing about its siblings having run may promote it to
        a revocation, because a revocation reds the run forever."""
        last = LAST_TAB[feed]
        store = InMemorySheetsStore()
        for _ in range(3):
            with respx.mock:
                mock_auth_token(st_settings.auth_url)
                _register_tenant(st_settings.api_base)
                _forbidden(st_settings.api_base, TAB_FIRST_CALL[last])
                summary = _run(st_settings, exporter_settings, store, feed)
            assert summary.scope_revoked == {}
            assert set(summary.scope_not_granted) == {last}


class TestTheTwoTenantsThisBroke:
    """The reviewer's two executed repros, as tests."""

    @respx.mock
    def test_a_truequote_only_tenant_gets_services_equipment_and_categories(
        self, st_settings, exporter_settings, monkeypatch, capsys
    ) -> None:
        """The runbook's TrueQuote block grants Pricebook Services, Equipment,
        Categories and Images, and deliberately NOT Materials — Materials arrives
        only when the contractor also buys Profit Wizard. So `materials` 403s on
        every run of every TrueQuote-only tenant, forever, and that is normal."""
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, TAB_FIRST_CALL["pricebook.materials"])
        store = InMemorySheetsStore()

        summary = _run(st_settings, exporter_settings, store, "pricebook")

        assert set(store.tabs) == {
            "_meta",
            "pricebook.services",
            "pricebook.equipment",
            "pricebook.categories",
        }
        assert set(summary.pricebook_row_counts or {}) == {
            "pricebook.services",
            "pricebook.equipment",
            "pricebook.categories",
        }
        assert set(summary.scope_not_granted) == {"pricebook.materials"}
        assert not summary.scope_revoked and not summary.pricebook_failures
        assert not [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::")]
        written = _meta_feeds(store)
        assert sorted(written) == [
            "pricebook.categories",
            "pricebook.equipment",
            "pricebook.services",
        ]
        assert len(written) == len(set(written))

    @respx.mock
    def test_a_truequote_only_tenant_still_gets_its_images(
        self, st_settings, exporter_settings
    ) -> None:
        """The worst of it. Gating the image pass on a feed-level denial meant
        such a tenant never received a single image, ever — while paying for the
        product whose whole point is pictures of doors."""
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, TAB_FIRST_CALL["pricebook.materials"])

        with _frozen_now(), patch("st_exporter.run._upload_pricebook_images") as upload:
            run_export(
                st_settings,
                exporter_settings,
                feeds=frozenset({"pricebook"}),
                export_store=InMemorySheetsStore(),
                raw_cache_store=InMemorySheetsStore(),
                image_client=object(),  # type: ignore[arg-type]
            )

        upload.assert_called_once()
        assert upload.call_args.kwargs["catalogue_complete"] is False
        assert upload.call_args.args[2], "the services and equipment items it DID read"

    @respx.mock
    def test_a_profit_wizard_tenant_without_reporting_writes_its_other_three_tabs(
        self, st_settings, exporter_settings, monkeypatch, capsys
    ) -> None:
        """Reporting is its own section in the ServiceTitan portal and the
        runbook's own author could not name the box, so a Profit Wizard tenant
        missing it is entirely ordinary. Invoices, timesheets and business units
        must still export, and the run must stay green."""
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, TAB_FIRST_CALL["reporting.jobCosts"])
        store = InMemorySheetsStore()

        summary = _run(st_settings, exporter_settings, store, "financial")

        assert set(summary.financial_row_counts or {}) == {
            "accounting.invoices",
            "payroll.timesheets",
            "settings.businessUnits",
            "sales.estimates",
        }
        assert set(summary.scope_not_granted) == {"reporting.jobCosts"}
        assert not summary.scope_revoked and not summary.financial_failures
        assert "reporting.jobCosts" not in store.tabs
        assert not [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::")]


class TestTheSummaryLine:
    """One line is all a support engineer reads. It has to name the TAB — the
    old line said `not_granted=pricebook` while two pricebook tabs had just been
    written in the same run, which is not merely vague, it is false."""

    def test_it_names_the_refused_tab_not_the_feed(self) -> None:
        from st_exporter.cli import _summary_line
        from st_exporter.run import ExportSummary

        line = _summary_line(
            ExportSummary(
                jobs_row_count=0,
                technicians_row_count=0,
                skipped_no_job=0,
                dry_run=False,
                pricebook_row_counts={"pricebook.services": 2, "pricebook.equipment": 1},
                scope_not_granted={"pricebook.materials": "Pricebook -> Materials"},
                scope_revoked={"reporting.jobCosts": "HTTP 403"},
            ),
            [],
        )

        assert "not_granted=pricebook_materials" in line
        assert "scope_revoked=reporting_jobCosts" in line
        assert "pricebook_services=2" in line


class TestOnlyThisFeed:
    @respx.mock
    def test_another_feeds_meta_row_survives_a_quiet_skip(
        self, st_settings, exporter_settings
    ) -> None:
        """A run that skips `pricebook` must not drop `jobs`' row on the way past
        — the carry-forward for unselected feeds and the one for denied tabs are
        different code paths and both write the same grid."""
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        for tab in PRICEBOOK_FEED_NAMES:
            _forbidden(st_settings.api_base, TAB_FIRST_CALL[tab])
        store = _seeded_store("jobs")

        _run(st_settings, exporter_settings, store, "pricebook")

        meta = parse_meta_grid(store.tabs["_meta"])
        assert meta["jobs"].last_run_at == EARLIER
        assert store.tabs["jobs"] == [["header"], ["yesterday"]]

    @respx.mock
    def test_one_denied_feed_does_not_deny_another(self, st_settings, exporter_settings) -> None:
        """`financial` refused outright, `pricebook` granted — **in one run that
        selects both**. Running only `pricebook` never fetched a financial route
        at all, so the old version of this test asserted nothing."""
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        for tab in FINANCIAL_FEED_NAMES:
            _forbidden(st_settings.api_base, TAB_FIRST_CALL[tab])
        store = InMemorySheetsStore()

        summary = _run(st_settings, exporter_settings, store, "pricebook", "financial")

        assert set(summary.scope_not_granted) == set(FINANCIAL_FEED_NAMES)
        assert not summary.scope_revoked
        for tab in PRICEBOOK_FEED_NAMES:
            assert tab in store.tabs
        for tab in FINANCIAL_FEED_NAMES:
            assert tab not in store.tabs


class TestNotEveryFailureIsAPurchase:
    """The dangerous direction. A feed that is DOWN must not be filed as a feed
    that was never bought — that turns an outage into silence."""

    @respx.mock
    @pytest.mark.parametrize("status", [400, 429, 500])
    def test_a_non_403_still_fails_the_pricebook_tab(
        self, status, st_settings, exporter_settings
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _status(st_settings.api_base, TAB_FIRST_CALL["pricebook.services"], status)
        store = InMemorySheetsStore()

        summary = _run(st_settings, exporter_settings, store, "pricebook")

        assert not summary.scope_not_granted and not summary.scope_revoked
        assert "pricebook.services" in (summary.pricebook_failures or {})
        # The other three tabs are untouched by it — per-tab isolation stands.
        assert "pricebook.categories" in (summary.pricebook_row_counts or {})

    @respx.mock
    @pytest.mark.parametrize(
        ("status", "why"),
        [
            # `KNOWN_UNVERIFIED.md` records a 400 from `active=Any` on some tenants.
            (400, "a filter ServiceTitan rejects"),
            # Bad credentials look nothing like an unbought product, and the client
            # has already retried once with a fresh token by the time we see one.
            (401, "credentials the client already retried"),
        ],
    )
    def test_a_non_403_fails_the_jobs_feed_loudly_instead(
        self, status, why, st_settings, exporter_settings
    ) -> None:
        """It must still be LOUD, and it must still not be filed as a purchase.

        Ticket 21 changed the channel, not the volume. `jobs` and `technicians`
        now run behind ``_guarded_feed`` for a reason that has nothing to do with
        scopes: `_meta` is written ONCE, at the end, for every feed, so an
        exception escaping a feed threw past that write and discarded the cursor
        of every feed that had ALREADY committed its tab — silently, forever
        re-draining. So the exception no longer propagates; the feed is named in
        `feed_failures`, announced on the run itself by ``_announce_feed_failure``
        (a red `::error` annotation + step summary + log), echoed in the summary
        line as `feed_failed=jobs`, and `_meta` is reached and written.

        The volume includes the EXIT CODE. `cli.py` reds any run with
        `feed_failures`, so a non-403 here is a failed Actions run exactly as it
        was before the guard existed — see
        `test_cli.TestAFailedFeedRedsTheRun::test_a_failed_jobs_feed_exits_one`
        and the end-to-end
        `test_writeback_integration.test_a_failed_jobs_feed_is_not_advised_to_change_the_feeds_it_already_has`.
        What the guard changed is that the `_meta` write below still happens.

        What this class defends is untouched: a feed that is DOWN is NOT filed as
        a feed that was never bought. Both ledgers stay empty, whatever the
        status, because only a 403 ever reaches ``ScopeLedger.deny``.
        """
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _status(st_settings.api_base, TAB_FIRST_CALL["jobs"], status)
        store = InMemorySheetsStore()

        summary = _run(st_settings, exporter_settings, store, "jobs")

        assert not summary.scope_not_granted and not summary.scope_revoked, why
        assert str(status) in (summary.feed_failures or {})["jobs"]
        # And the write the old behaviour threw past actually happened.
        assert "_meta" in store.tabs


class TestAFirstEverRun:
    """`_meta` is empty for EVERY tab on run one, so "no row" cannot mean
    "revoked" — and it does not. Nothing is declared revoked on a brand-new
    connector, and nothing is declared bought that was not."""

    @respx.mock
    def test_nothing_is_called_revoked_when_the_sheet_is_brand_new(
        self, st_settings, exporter_settings
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        for tab in PRICEBOOK_FEED_NAMES:
            _forbidden(st_settings.api_base, TAB_FIRST_CALL[tab])
        store = InMemorySheetsStore()
        assert store.read_grid("_meta") == []

        summary = _run(st_settings, exporter_settings, store, "pricebook")

        assert summary.scope_revoked == {}
        assert set(summary.scope_not_granted) == set(PRICEBOOK_FEED_NAMES)

    @respx.mock
    def test_the_feed_they_did_buy_still_runs_and_writes_its_meta_row(
        self, st_settings, exporter_settings
    ) -> None:
        """The other half of run one for a single-product contractor: the granted
        tabs work, and the rows they write are what make a LATER 403 on those
        tabs decidable as a revocation."""
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        store = InMemorySheetsStore()

        _run(st_settings, exporter_settings, store, "pricebook")

        meta = parse_meta_grid(store.tabs["_meta"])
        assert meta["pricebook.services"].last_run_at == FIXED_NOW.isoformat()

    @respx.mock
    def test_a_403_on_the_run_after_a_good_one_flips_to_revoked(
        self, st_settings, exporter_settings
    ) -> None:
        """The whole mechanism, end to end and in order: run 1 succeeds and lays
        down the evidence; run 2 is refused and is therefore loud."""
        store = InMemorySheetsStore()
        with respx.mock:
            mock_auth_token(st_settings.auth_url)
            _register_tenant(st_settings.api_base)
            first = _run(st_settings, exporter_settings, store, "pricebook")
        assert not first.scope_not_granted and not first.scope_revoked

        with respx.mock:
            mock_auth_token(st_settings.auth_url)
            _register_tenant(st_settings.api_base)
            _forbidden(st_settings.api_base, TAB_FIRST_CALL["pricebook.categories"])
            second = _run(st_settings, exporter_settings, store, "pricebook")

        assert set(second.scope_revoked) == {"pricebook.categories"}
        assert not second.scope_not_granted


class TestTheImagePass:
    @respx.mock
    def test_a_wholly_refused_catalogue_never_prunes_the_ledger(
        self, st_settings, exporter_settings
    ) -> None:
        """A refused catalogue fetched no items. Pruning against that empty list
        would let `ImageLedger.keep` drop every asset the tenant has ever had
        uploaded — "not looked at" read as "gone". `catalogue_complete` is what
        vetoes it, and it is False here because no item tab produced a grid."""
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        for tab in PRICEBOOK_FEED_NAMES:
            _forbidden(st_settings.api_base, TAB_FIRST_CALL[tab])

        with _frozen_now(), patch("st_exporter.run._upload_pricebook_images") as upload:
            summary = run_export(
                st_settings,
                exporter_settings,
                feeds=frozenset({"pricebook"}),
                export_store=InMemorySheetsStore(),
                raw_cache_store=InMemorySheetsStore(),
                image_client=object(),  # type: ignore[arg-type]
            )

        assert upload.call_args.kwargs["catalogue_complete"] is False
        assert upload.call_args.args[2] == []
        assert set(summary.scope_not_granted) == set(PRICEBOOK_FEED_NAMES)

    @respx.mock
    def test_a_fully_granted_catalogue_is_complete(self, st_settings, exporter_settings) -> None:
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)

        with _frozen_now(), patch("st_exporter.run._upload_pricebook_images") as upload:
            run_export(
                st_settings,
                exporter_settings,
                feeds=frozenset({"pricebook"}),
                export_store=InMemorySheetsStore(),
                raw_cache_store=InMemorySheetsStore(),
                image_client=object(),  # type: ignore[arg-type]
            )

        assert upload.call_args.kwargs["catalogue_complete"] is True

    @respx.mock
    def test_a_refused_categories_tab_does_not_make_the_catalogue_incomplete(
        self, st_settings, exporter_settings
    ) -> None:
        """Categories carry no images, so losing that tab cannot orphan a key."""
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, TAB_FIRST_CALL["pricebook.categories"])

        with _frozen_now(), patch("st_exporter.run._upload_pricebook_images") as upload:
            run_export(
                st_settings,
                exporter_settings,
                feeds=frozenset({"pricebook"}),
                export_store=InMemorySheetsStore(),
                raw_cache_store=InMemorySheetsStore(),
                image_client=object(),  # type: ignore[arg-type]
            )

        assert upload.call_args.kwargs["catalogue_complete"] is True


class TestTheDrainIsNotAFeed:
    """Three review rounds hardened the drain's gate: it runs iff `outbox` is in
    `--feeds`. It is not scope-gated, it fetches nothing from the read API, and
    nothing here may touch it."""

    def test_outbox_is_not_an_export_feed(self) -> None:
        assert OUTBOX_FEED not in EXPORT_FEEDS

    def test_no_scope_permission_is_declared_for_the_drain(self) -> None:
        assert OUTBOX_FEED not in TAB_PERMISSIONS
        assert set(TAB_PERMISSIONS) == set(EXPORT_TABS)
