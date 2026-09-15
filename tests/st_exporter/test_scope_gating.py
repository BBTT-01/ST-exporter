"""A 403 per feed: never bought (quiet) or revoked (loud), decided from `_meta`.

The caller workflow no longer carries a repository variable per feed. Every feed
job runs on its schedule and ServiceTitan's scopes decide what exports, so the
exporter has to answer the one question the variables were defending against:

    does this 403 mean "they never bought it" or "the permission was taken away"?

`_meta` answers it, because it already records the last successful run of every
tab. These tests hold that line from both sides — the quiet skip must stay quiet
and tab-free, the revocation must stay loud and must never lose the evidence that
made it decidable, and NOTHING but a 403 may take either path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest
import respx

from st_cli.exceptions import APIError, RateLimitError, STCLIError, TransportError
from st_exporter.meta import MetaRow, build_meta_grid, parse_meta_grid
from st_exporter.run import (
    EXPORT_FEEDS,
    FINANCIAL_FEED_NAMES,
    OUTBOX_FEED,
    PRICEBOOK_FEED_NAMES,
    run_export,
)
from st_exporter.scopes import (
    NOT_GRANTED,
    REVOKED,
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


#: The first endpoint each feed calls. Registered AFTER the happy-path fixtures:
#: respx keys routes by pattern, so re-registering one replaces it.
FIRST_CALL = {
    "jobs": f"crm/v2/tenant/{TENANT_ID}/export/customers",
    "technicians": f"settings/v2/tenant/{TENANT_ID}/technicians",
    "pricebook": f"pricebook/v2/tenant/{TENANT_ID}/services",
    "financial": f"accounting/v2/tenant/{TENANT_ID}/invoices",
}

#: Every tab a feed owns — the unit a ServiceTitan permission is granted over.
FEED_TABS = {
    "jobs": ("jobs",),
    "technicians": ("technicians",),
    "pricebook": PRICEBOOK_FEED_NAMES,
    "financial": FINANCIAL_FEED_NAMES,
}


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


def _run(st_settings, exporter_settings, export_store, feed: str):
    with _frozen_now():
        return run_export(
            st_settings,
            exporter_settings,
            feeds=frozenset({feed}),
            export_store=export_store,
            raw_cache_store=InMemorySheetsStore(),
        )


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

    def test_any_tab_of_the_feed_counts(self) -> None:
        """ServiceTitan grants `Pricebook`, not `pricebook.equipment`. So one tab
        having worked is proof the whole feed's permission was once held."""
        rows = {"pricebook.categories": MetaRow(feed="pricebook.categories", last_run_at=EARLIER)}
        assert feed_ever_ran(rows, PRICEBOOK_FEED_NAMES)


class TestTheLedger:
    def _ledger(self, meta_rows=None):
        new: list[MetaRow] = []
        return ScopeLedger(meta_rows=meta_rows or {}, new_meta_rows=new), new

    def test_a_first_ever_403_is_not_granted(self) -> None:
        ledger, _ = self._ledger()
        assert ledger.deny("pricebook", PRICEBOOK_FEED_NAMES, APIError(403, "x")) == NOT_GRANTED
        assert "pricebook" in ledger.not_granted
        assert not ledger.revoked

    def test_a_403_after_a_good_run_is_revoked(self) -> None:
        rows = {"jobs": MetaRow(feed="jobs", last_run_at=EARLIER)}
        ledger, _ = self._ledger(rows)
        assert ledger.deny("jobs", ("jobs",), APIError(403, "x")) == REVOKED
        assert "jobs" in ledger.revoked
        assert not ledger.not_granted

    def test_anything_but_a_403_is_handed_straight_back(self) -> None:
        ledger, new = self._ledger({"jobs": MetaRow(feed="jobs", last_run_at=EARLIER)})
        assert ledger.deny("jobs", ("jobs",), RateLimitError()) is None
        assert not ledger.denied("jobs")
        # And it did NOT carry anything forward: the caller still owns that row.
        assert new == []

    def test_the_previous_meta_row_is_carried_forward_when_revoked(self) -> None:
        row = MetaRow(feed="jobs", last_run_at=EARLIER, row_count=9)
        ledger, new = self._ledger({"jobs": row})
        ledger.deny("jobs", ("jobs",), APIError(403, "x"))
        assert new == [row]

    def test_carrying_forward_is_idempotent(self) -> None:
        """Four pricebook tabs each earn their own 403; the grid must not grow
        four copies of one row."""
        row = MetaRow(feed="pricebook.services", last_run_at=EARLIER)
        ledger, new = self._ledger({"pricebook.services": row})
        for _ in range(4):
            ledger.deny("pricebook", PRICEBOOK_FEED_NAMES, APIError(403, "x"))
        assert new == [row]

    def test_a_feed_with_no_previous_row_carries_nothing(self) -> None:
        ledger, new = self._ledger()
        ledger.deny("pricebook", PRICEBOOK_FEED_NAMES, APIError(403, "x"))
        assert new == []


# ---------------------------------------------------------------------------
# Through the whole run.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("feed", sorted(EXPORT_FEEDS))
class TestAFeedThatWasNeverGranted:
    """The ordinary state of a feed belonging to a product this contractor did
    not buy, on a connector that runs all four feed jobs for everybody."""

    @respx.mock
    def test_it_writes_no_tab_and_does_not_fail(self, feed, st_settings, exporter_settings) -> None:
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, FIRST_CALL[feed])
        store = InMemorySheetsStore()

        summary = _run(st_settings, exporter_settings, store, feed)

        assert feed in summary.scope_not_granted
        assert not summary.scope_revoked
        for tab in FEED_TABS[feed]:
            assert tab not in store.tabs, "an absent tab is how the contract says 'not bought'"

    @respx.mock
    def test_it_names_the_permission_in_the_log_and_stays_at_info(
        self, feed, st_settings, exporter_settings, caplog
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, FIRST_CALL[feed])
        with caplog.at_level(logging.INFO, logger="st_exporter"):
            _run(st_settings, exporter_settings, InMemorySheetsStore(), feed)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("Skipping quietly" in r.getMessage() for r in caplog.records)

    @respx.mock
    def test_it_annotates_nothing(
        self, feed, st_settings, exporter_settings, monkeypatch, capsys
    ) -> None:
        """Quiet means quiet. A `::warning` per feed per cycle, forever, on every
        connector whose contractor bought one product, is noise that trains
        everybody to ignore the channel the revocation needs."""
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, FIRST_CALL[feed])
        _run(st_settings, exporter_settings, InMemorySheetsStore(), feed)
        assert not [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::")]


@pytest.mark.parametrize("feed", sorted(EXPORT_FEEDS))
class TestAFeedWhosePermissionWasRevoked:
    """It worked before. `_meta` proves it, which is the whole reason that row is
    carried forward rather than dropped when a feed does not run."""

    @respx.mock
    def test_it_is_loud_and_leaves_the_previous_tab_alone(
        self, feed, st_settings, exporter_settings, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, FIRST_CALL[feed])
        store = _seeded_store(feed)

        summary = _run(st_settings, exporter_settings, store, feed)

        assert feed in summary.scope_revoked
        assert not summary.scope_not_granted
        annotations = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::")]
        assert annotations, "a green run is a run nobody opens"
        assert annotations[0].startswith("::error title=ServiceTitan permission revoked::")
        assert feed in annotations[0]
        for tab in FEED_TABS[feed]:
            assert store.tabs[tab] == [["header"], ["yesterday"]]

    @respx.mock
    def test_the_meta_row_is_carried_forward_unchanged(
        self, feed, st_settings, exporter_settings
    ) -> None:
        """Never delete the evidence. It is what makes the NEXT run's 403
        decidable — drop it once and a revocation reads as "never bought" from
        then on, forever."""
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, FIRST_CALL[feed])
        store = _seeded_store(feed)

        _run(st_settings, exporter_settings, store, feed)

        meta = parse_meta_grid(store.tabs["_meta"])
        for tab in FEED_TABS[feed]:
            assert meta[tab].last_run_at == EARLIER, "last_run_at must still name the last GOOD run"
            assert meta[tab].row_count == 7

    @respx.mock
    def test_it_names_the_feed_and_the_permission_at_error(
        self, feed, st_settings, exporter_settings, caplog
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, FIRST_CALL[feed])
        with caplog.at_level(logging.ERROR, logger="st_exporter"):
            _run(st_settings, exporter_settings, _seeded_store(feed), feed)
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("SCOPE REVOKED" in message and feed in message for message in errors)


class TestOnlyThisFeed:
    @respx.mock
    def test_another_feeds_meta_row_survives_a_quiet_skip(
        self, st_settings, exporter_settings
    ) -> None:
        """A run that skips `pricebook` must not drop `jobs`' row on the way past
        — the carry-forward for unselected feeds and the one for denied feeds are
        different code paths and both write the same grid."""
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, FIRST_CALL["pricebook"])
        store = _seeded_store("jobs")

        _run(st_settings, exporter_settings, store, "pricebook")

        meta = parse_meta_grid(store.tabs["_meta"])
        assert meta["jobs"].last_run_at == EARLIER
        assert store.tabs["jobs"] == [["header"], ["yesterday"]]

    @respx.mock
    def test_one_denied_feed_does_not_deny_another(self, st_settings, exporter_settings) -> None:
        """`financial` refused, `pricebook` granted: the pricebook tabs are
        written normally in the same Sheet."""
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, FIRST_CALL["financial"])
        store = InMemorySheetsStore()

        summary = _run(st_settings, exporter_settings, store, "pricebook")

        assert not summary.scope_not_granted and not summary.scope_revoked
        assert "pricebook.services" in store.tabs


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
        _status(st_settings.api_base, FIRST_CALL["pricebook"], status)
        store = InMemorySheetsStore()

        summary = _run(st_settings, exporter_settings, store, "pricebook")

        assert not summary.scope_not_granted and not summary.scope_revoked
        assert "pricebook.services" in (summary.pricebook_failures or {})
        # The other three tabs are untouched by it — per-tab isolation stands.
        assert "pricebook.categories" in (summary.pricebook_row_counts or {})

    @respx.mock
    def test_a_400_on_the_jobs_feed_still_ends_the_run(
        self, st_settings, exporter_settings
    ) -> None:
        """`KNOWN_UNVERIFIED.md` records a 400 from `active=Any` on some tenants.
        It is a sibling branch's problem, and it must still be loud here."""
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _status(st_settings.api_base, FIRST_CALL["jobs"], 400)
        with pytest.raises(APIError) as exc:
            _run(st_settings, exporter_settings, InMemorySheetsStore(), "jobs")
        assert exc.value.status_code == 400

    @respx.mock
    def test_a_401_is_not_a_purchase_decision_either(self, st_settings, exporter_settings) -> None:
        """Bad credentials look nothing like an unbought product, and the client
        has already retried once with a fresh token by the time we see one."""
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _status(st_settings.api_base, FIRST_CALL["jobs"], 401)
        with pytest.raises(APIError) as exc:
            _run(st_settings, exporter_settings, InMemorySheetsStore(), "jobs")
        assert exc.value.status_code == 401


class TestAFirstEverRun:
    """`_meta` is empty for EVERY feed on run one, so "no row" cannot mean
    "revoked" — and it does not. Nothing is declared revoked on a brand-new
    connector, and nothing is declared bought that was not."""

    @respx.mock
    def test_nothing_is_called_revoked_when_the_sheet_is_brand_new(
        self, st_settings, exporter_settings
    ) -> None:
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, FIRST_CALL["pricebook"])
        store = InMemorySheetsStore()
        assert store.read_grid("_meta") == []

        summary = _run(st_settings, exporter_settings, store, "pricebook")

        assert summary.scope_revoked == {}
        assert "pricebook" in summary.scope_not_granted

    @respx.mock
    def test_the_feed_they_did_buy_still_runs_and_writes_its_meta_row(
        self, st_settings, exporter_settings
    ) -> None:
        """The other half of run one for a single-product contractor: the granted
        feed works, and the row it writes is what makes a LATER 403 on that feed
        decidable as a revocation."""
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
            _forbidden(st_settings.api_base, FIRST_CALL["pricebook"])
            second = _run(st_settings, exporter_settings, store, "pricebook")

        assert "pricebook" in second.scope_revoked
        assert not second.scope_not_granted


class TestTheImagePass:
    @respx.mock
    def test_a_denied_pricebook_feed_never_reaches_the_image_ledger(
        self, st_settings, exporter_settings
    ) -> None:
        """A refused catalogue fetched no items. Handing that empty list to the
        image pass would let `ImageLedger.keep` prune every asset the tenant has
        ever had uploaded — "not looked at" read as "gone"."""
        mock_auth_token(st_settings.auth_url)
        _register_tenant(st_settings.api_base)
        _forbidden(st_settings.api_base, FIRST_CALL["pricebook"])

        with _frozen_now(), patch("st_exporter.run._upload_pricebook_images") as upload:
            summary = run_export(
                st_settings,
                exporter_settings,
                feeds=frozenset({"pricebook"}),
                export_store=InMemorySheetsStore(),
                raw_cache_store=InMemorySheetsStore(),
                image_client=object(),  # type: ignore[arg-type]
            )

        upload.assert_not_called()
        assert summary.images is None
        assert "pricebook" in summary.scope_not_granted


class TestTheDrainIsNotAFeed:
    """Three review rounds hardened the drain's gate: it runs iff `outbox` is in
    `--feeds`. It is not scope-gated, it fetches nothing from the read API, and
    nothing here may touch it."""

    def test_outbox_is_not_an_export_feed(self) -> None:
        assert OUTBOX_FEED not in EXPORT_FEEDS

    def test_no_scope_permission_is_declared_for_the_drain(self) -> None:
        from st_exporter.scopes import FEED_PERMISSIONS

        assert OUTBOX_FEED not in FEED_PERMISSIONS
        assert set(FEED_PERMISSIONS) == set(EXPORT_FEEDS)
