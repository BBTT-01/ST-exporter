"""The customer-contacts feed: where a phone number and an email really come from.

``customer_phone`` and ``customer_email`` were blank on every `jobs` row ever
exported on both live tenants, because the exporter read them off the customer
record and ServiceTitan keeps them on ``customers/{id}/contacts``. These tests pin
the fetch half: the route, the dedupe, the cap, and the selection rule shared with
TradeRated's Direct-path function.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from st_cli.client import ServiceTitanClient
from st_cli.exceptions import APIError
from st_exporter.feeds.contacts import (
    DEFAULT_MAX_CONTACT_CUSTOMERS,
    EMAIL_TYPES_IN_PREFERENCE_ORDER,
    PHONE_TYPES_IN_PREFERENCE_ORDER,
    fetch_contacts_export_delta,
    fetch_contacts_per_customer,
    group_contacts_by_customer,
    select_contact_value,
)
from tests.st_exporter.conftest import mock_auth_token

TENANT_ID = 12345


def _client(st_settings) -> ServiceTitanClient:
    return ServiceTitanClient(st_settings)


def _contacts_route(api_base: str, customer_id: int) -> str:
    return f"{api_base}/crm/v2/tenant/{TENANT_ID}/customers/{customer_id}/contacts"


class TestSelectionRule:
    """The rule has to match the Direct path's, or one contractor sees two numbers."""

    def test_mobile_wins_over_landline_however_the_array_is_ordered(self) -> None:
        contacts = [
            {"type": "Phone", "value": "555-LAND"},
            {"type": "MobilePhone", "value": "555-MOBILE"},
        ]
        assert select_contact_value(contacts, PHONE_TYPES_IN_PREFERENCE_ORDER) == "555-MOBILE"
        assert (
            select_contact_value(list(reversed(contacts)), PHONE_TYPES_IN_PREFERENCE_ORDER)
            == "555-MOBILE"
        )

    def test_a_landline_is_used_when_there_is_no_mobile(self) -> None:
        assert (
            select_contact_value(
                [{"type": "Phone", "value": "555-LAND"}], PHONE_TYPES_IN_PREFERENCE_ORDER
            )
            == "555-LAND"
        )

    def test_a_fax_is_never_a_phone_number(self) -> None:
        """A wrong number on a technician's screen is worse than a blank one."""
        contacts = [{"type": "Fax", "value": "555-FAX"}]
        assert select_contact_value(contacts, PHONE_TYPES_IN_PREFERENCE_ORDER) is None

    def test_an_email_is_never_a_phone_number_and_vice_versa(self) -> None:
        contacts = [{"type": "Email", "value": "a@b.test"}]
        assert select_contact_value(contacts, PHONE_TYPES_IN_PREFERENCE_ORDER) is None
        assert select_contact_value(contacts, EMAIL_TYPES_IN_PREFERENCE_ORDER) == "a@b.test"

    def test_types_are_matched_case_folded(self) -> None:
        contacts = [{"type": "mobilephone", "value": "555-MOBILE"}]
        assert select_contact_value(contacts, PHONE_TYPES_IN_PREFERENCE_ORDER) == "555-MOBILE"

    def test_a_blank_value_is_skipped_for_the_next_entry_of_the_same_type(self) -> None:
        contacts = [
            {"type": "Phone", "value": "   "},
            {"type": "Phone", "value": "555-LAND"},
        ]
        assert select_contact_value(contacts, PHONE_TYPES_IN_PREFERENCE_ORDER) == "555-LAND"

    def test_an_untyped_or_malformed_entry_is_skipped_not_guessed(self) -> None:
        contacts = [{"value": "555-????"}, "not a dict", {"type": None, "value": "555-????"}]
        assert select_contact_value(contacts, PHONE_TYPES_IN_PREFERENCE_ORDER) is None

    def test_no_contacts_at_all_is_none_not_an_error(self) -> None:
        assert select_contact_value([], PHONE_TYPES_IN_PREFERENCE_ORDER) is None
        assert select_contact_value(None, PHONE_TYPES_IN_PREFERENCE_ORDER) is None


class TestPerCustomerFetch:
    @respx.mock
    def test_it_reads_the_contacts_sub_resource_for_each_customer(self, st_settings) -> None:
        mock_auth_token(st_settings.auth_url)
        respx.get(_contacts_route(st_settings.api_base, 10)).mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"type": "Email", "value": "a@b.test"}], "hasMore": False},
            )
        )
        contacts = fetch_contacts_per_customer(_client(st_settings), [10])
        assert contacts == {"10": [{"type": "Email", "value": "a@b.test"}]}

    @respx.mock
    def test_repeated_customers_cost_one_request_not_one_per_row(self, st_settings) -> None:
        """Many jobs share a customer — the whole reason N+1 is affordable here."""
        mock_auth_token(st_settings.auth_url)
        route = respx.get(_contacts_route(st_settings.api_base, 10)).mock(
            return_value=httpx.Response(200, json={"data": [], "hasMore": False})
        )
        fetch_contacts_per_customer(_client(st_settings), [10, 10, "10", 10])
        assert route.call_count == 1

    @respx.mock
    def test_a_404_on_one_customer_does_not_lose_the_others(self, st_settings) -> None:
        """A customer merged or deleted between the export feed and this call."""
        mock_auth_token(st_settings.auth_url)
        respx.get(_contacts_route(st_settings.api_base, 10)).mock(
            return_value=httpx.Response(404, text="gone")
        )
        respx.get(_contacts_route(st_settings.api_base, 11)).mock(
            return_value=httpx.Response(
                200, json={"data": [{"type": "Phone", "value": "555-1"}], "hasMore": False}
            )
        )
        contacts = fetch_contacts_per_customer(_client(st_settings), [10, 11])
        assert "10" not in contacts
        assert contacts["11"] == [{"type": "Phone", "value": "555-1"}]

    @respx.mock
    def test_a_403_is_raised_for_the_callers_degradation_guard(self, st_settings) -> None:
        """Not swallowed here: silently returning {} is the blank-column bug again."""
        mock_auth_token(st_settings.auth_url)
        respx.get(_contacts_route(st_settings.api_base, 10)).mock(
            return_value=httpx.Response(403, text="Forbidden")
        )
        client = _client(st_settings)
        with pytest.raises(APIError) as excinfo:
            fetch_contacts_per_customer(client, [10])
        assert excinfo.value.status_code == 403

    @respx.mock
    def test_the_cap_bounds_the_request_count_and_says_so(
        self, st_settings, caplog, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        mock_auth_token(st_settings.auth_url)
        routes = [
            respx.get(_contacts_route(st_settings.api_base, cid)).mock(
                return_value=httpx.Response(200, json={"data": [], "hasMore": False})
            )
            for cid in (10, 11, 12)
        ]
        with caplog.at_level("WARNING", logger="st_exporter"):
            fetch_contacts_per_customer(_client(st_settings), [10, 11, 12], max_customers=2)

        assert [route.call_count for route in routes] == [1, 1, 0]
        assert "cap is 2" in caplog.text
        # And loudly: a capped run leaves real rows blank, which must not be a
        # line in the log of a green Actions run that nobody opens.
        assert "::warning title=Customer contacts capped::" in capsys.readouterr().out

    @respx.mock
    def test_the_default_cap_is_generous_enough_for_both_live_tenants(
        self, st_settings, caplog
    ) -> None:
        """2,441 and 1,068 job ROWS, so fewer distinct customers than that."""
        assert DEFAULT_MAX_CONTACT_CUSTOMERS > 2441
        mock_auth_token(st_settings.auth_url)
        respx.get(_contacts_route(st_settings.api_base, 10)).mock(
            return_value=httpx.Response(200, json={"data": [], "hasMore": False})
        )
        with caplog.at_level("WARNING", logger="st_exporter"):
            fetch_contacts_per_customer(_client(st_settings), [10])
        assert "cap" not in caplog.text


class TestBulkRoute:
    """The opt-in bulk change-feed — unverified, which is why it is opt-in."""

    @respx.mock
    def test_it_drains_the_export_feed_from_the_stored_cursor(self, st_settings) -> None:
        mock_auth_token(st_settings.auth_url)
        seen: list[str | None] = []

        def _answer(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.params.get("from"))
            return httpx.Response(
                200,
                json={
                    "data": [{"id": 1, "customerId": 10, "type": "Phone", "value": "555-1"}],
                    "hasMore": False,
                    "continueFrom": "contacts-c2",
                },
            )

        respx.get(
            f"{st_settings.api_base}/crm/v2/tenant/{TENANT_ID}/export/customers/contacts"
        ).mock(side_effect=_answer)

        records, cursor = fetch_contacts_export_delta(_client(st_settings), "contacts-c1")

        assert seen == ["contacts-c1"]
        assert cursor == "contacts-c2"
        assert group_contacts_by_customer(records) == {
            "10": [{"id": 1, "customerId": 10, "type": "Phone", "value": "555-1"}]
        }

    def test_grouping_drops_records_with_no_customer_to_join_on(self) -> None:
        assert group_contacts_by_customer([{"type": "Phone", "value": "555-1"}]) == {}
