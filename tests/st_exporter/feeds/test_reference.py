from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from st_exporter.feeds.reference import fetch_business_units, fetch_job_types, fetch_technicians


@pytest.fixture()
def mock_client():
    return MagicMock()


def test_fetch_technicians_hits_settings_technicians(mock_client) -> None:
    mock_client.get.return_value = {
        "data": [{"id": 1, "name": "Jane", "email": "jane@example.com", "active": True}],
        "hasMore": False,
    }
    technicians = fetch_technicians(mock_client)
    assert technicians == [{"id": 1, "name": "Jane", "email": "jane@example.com", "active": True}]
    mock_client.get.assert_called_once_with(
        "settings", "technicians", params={"active": "Any", "page": 1, "pageSize": 200}
    )


def test_fetch_job_types_returns_dict_keyed_by_string_id(mock_client) -> None:
    mock_client.get.return_value = {
        "data": [{"id": 5, "name": "Repair"}, {"id": 6, "name": "Install"}],
        "hasMore": False,
    }
    job_types = fetch_job_types(mock_client)
    assert job_types == {"5": {"id": 5, "name": "Repair"}, "6": {"id": 6, "name": "Install"}}
    mock_client.get.assert_called_once_with("jpm", "job-types", params={"page": 1, "pageSize": 200})


def test_fetch_business_units_returns_dict_keyed_by_string_id(mock_client) -> None:
    mock_client.get.return_value = {"data": [{"id": 7, "name": "Garage Doors"}], "hasMore": False}
    business_units = fetch_business_units(mock_client)
    assert business_units == {"7": {"id": 7, "name": "Garage Doors"}}
    mock_client.get.assert_called_once_with(
        "settings", "business-units", params={"page": 1, "pageSize": 200}
    )


def test_fetch_job_types_skips_records_without_id(mock_client) -> None:
    mock_client.get.return_value = {"data": [{"name": "No id"}], "hasMore": False}
    assert fetch_job_types(mock_client) == {}
