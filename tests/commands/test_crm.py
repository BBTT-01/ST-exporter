"""Tests for CRM commands."""

from __future__ import annotations

from tests.conftest import make_envelope


class TestCustomersList:
    def test_basic_list(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope(
            [
                {
                    "id": 1,
                    "name": "Acme",
                    "email": "a@b.com",
                    "phone": "555",
                    "active": True,
                    "createdOn": "2025-01-01",
                }
            ]
        )
        result = invoke(["crm", "customers-list"])
        assert result.exit_code == 0
        assert "Acme" in result.output
        mock_client.get.assert_called_once()

    def test_with_filters(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope([])
        invoke(["crm", "customers-list", "--name", "Acme", "--email", "a@b.com"])
        call_params = mock_client.get.call_args[1]["params"]
        assert call_params["name"] == "Acme"
        assert call_params["email"] == "a@b.com"

    def test_json_output(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope(
            [
                {
                    "id": 1,
                    "name": "Acme",
                    "email": None,
                    "phone": None,
                    "active": True,
                    "createdOn": "2025-01-01",
                }
            ]
        )
        result = invoke(["crm", "customers-list"], json_output=True)
        assert result.exit_code == 0


class TestCustomersGet:
    def test_get_by_id(self, invoke, mock_client):
        mock_client.get.return_value = {
            "id": 42,
            "name": "Acme",
            "email": "a@b.com",
            "phone": "555",
            "active": True,
            "createdOn": "2025-01-01",
        }
        result = invoke(["crm", "customers-get", "42"])
        assert result.exit_code == 0
        assert "Acme" in result.output
        mock_client.get.assert_called_once_with("crm", "customers/42")


class TestLocations:
    def test_locations_list(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope(
            [
                {
                    "id": 1,
                    "name": "HQ",
                    "address": {
                        "street": "123 Main",
                        "city": "Austin",
                        "state": "TX",
                        "zip": "78701",
                    },
                }
            ]
        )
        result = invoke(["crm", "locations-list"])
        assert result.exit_code == 0
        assert "HQ" in result.output

    def test_locations_get(self, invoke, mock_client):
        mock_client.get.return_value = {
            "id": 1,
            "name": "HQ",
            "address": {"street": "123 Main", "city": "Austin", "state": "TX", "zip": "78701"},
        }
        result = invoke(["crm", "locations-get", "1"])
        assert result.exit_code == 0


class TestBookings:
    def test_bookings_list(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope(
            [
                {
                    "id": 1,
                    "customerId": 10,
                    "status": "Pending",
                    "start": "2025-01-01",
                    "createdOn": "2025-01-01",
                }
            ]
        )
        result = invoke(["crm", "bookings-list"])
        assert result.exit_code == 0

    def test_bookings_get(self, invoke, mock_client):
        mock_client.get.return_value = {
            "id": 1,
            "customerId": 10,
            "status": "Pending",
            "start": "2025-01-01",
            "createdOn": "2025-01-01",
        }
        result = invoke(["crm", "bookings-get", "1"])
        assert result.exit_code == 0


class TestContacts:
    def test_contacts_list(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope(
            [{"id": 1, "type": "Email", "value": "a@b.com", "customerId": 10}]
        )
        result = invoke(["crm", "contacts-list"])
        assert result.exit_code == 0

    def test_contacts_list_all_pages(self, invoke, mock_client):
        contact1 = {"id": 1, "type": "Email", "value": "a@b.com", "customerId": 10}
        contact2 = {"id": 2, "type": "Phone", "value": "555", "customerId": 10}
        mock_client.get.side_effect = [
            make_envelope([contact1], has_more=True),
            make_envelope([contact2], has_more=False),
        ]
        result = invoke(["crm", "contacts-list", "--all"])
        assert result.exit_code == 0

    def test_contacts_get(self, invoke, mock_client):
        mock_client.get.return_value = {
            "id": 1,
            "type": "Email",
            "value": "a@b.com",
            "customerId": 10,
        }
        result = invoke(["crm", "contacts-get", "1"])
        assert result.exit_code == 0


class TestCustomerContactColumns:
    """ServiceTitan carries contact details as settings ARRAYS, not scalars.

    Profit Wizard's production client reads `c.phoneSettings[].phone` /
    `c.emailSettings[].email` and treats the flat scalars only as a fallback
    (`lib/crm/servicetitan.ts:903-906`). Reading only the scalar is the same
    mistake that left `job_number` blank on every row ever exported.
    """

    def test_the_settings_arrays_are_read(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope(
            [
                {
                    "id": 1,
                    "name": "Acme",
                    "emailSettings": [{"email": "primary@acme.test"}],
                    "phoneSettings": [{"phone": "555-0100"}],
                }
            ]
        )
        result = invoke(["crm", "customers-list"])
        assert "primary@acme.test" in result.output
        assert "555-0100" in result.output

    def test_the_scalars_still_work_as_a_fallback(self, invoke, mock_client):
        # Widen-only: nothing that showed a phone number before stops doing so.
        mock_client.get.return_value = make_envelope(
            [{"id": 1, "name": "Acme", "email": "flat@acme.test", "phone": "555-0199"}]
        )
        result = invoke(["crm", "customers-list"])
        assert "flat@acme.test" in result.output
        assert "555-0199" in result.output

    def test_an_empty_settings_array_falls_back_rather_than_blanking(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope(
            [{"id": 1, "name": "Acme", "phoneSettings": [], "phone": "555-0199"}]
        )
        assert "555-0199" in invoke(["crm", "customers-list"]).output
