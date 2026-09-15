"""Tests for Jobs commands."""

from __future__ import annotations

from tests.conftest import make_envelope

# ServiceTitan's JPM job object names this field `jobNumber`, NOT `number` —
# the fixture said `number` and so the blank Number column looked correct here
# for as long as it has been wrong against every real tenant. (`number` IS
# legitimate on invoices and projects; PROJECT below keeps it deliberately.)
JOB = {
    "id": 1,
    "jobNumber": "J-001",
    "customerId": 10,
    "jobStatus": "Completed",
    "jobTypeName": "Repair",
    "total": 150.0,
    "createdOn": "2025-01-01",
}
APPOINTMENT = {
    "id": 1,
    "jobId": 1,
    "status": "Scheduled",
    "start": "2025-01-01T09:00",
    "end": "2025-01-01T10:00",
    "arrivalWindowStart": "09:00",
    "arrivalWindowEnd": "10:00",
}
PROJECT = {"id": 1, "number": "P-001", "name": "Big Project", "status": "Active", "customerId": 10}


class TestJobsList:
    def test_basic_list(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope([JOB])
        result = invoke(["jobs", "list"])
        assert result.exit_code == 0
        assert "J-001" in result.output

    def test_with_status_filter(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope([])
        invoke(["jobs", "list", "--status", "Completed"])
        params = mock_client.get.call_args[1]["params"]
        assert params["jobStatus"] == "Completed"

    def test_with_range(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope([])
        invoke(["jobs", "list", "--range", "last-7-days"])
        params = mock_client.get.call_args[1]["params"]
        assert "createdOnOrAfter" in params
        assert "createdBefore" in params

    def test_explicit_dates_override_range(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope([])
        invoke(["jobs", "list", "--range", "last-week", "--from-date", "2025-01-01"])
        params = mock_client.get.call_args[1]["params"]
        assert params["createdOnOrAfter"] == "2025-01-01"


class TestJobsGet:
    def test_get(self, invoke, mock_client):
        mock_client.get.return_value = JOB
        result = invoke(["jobs", "get", "1"])
        assert result.exit_code == 0
        assert "J-001" in result.output


class TestJobsCancel:
    def test_cancel(self, invoke, mock_client):
        mock_client.post.return_value = None
        result = invoke(["jobs", "cancel", "1"])
        assert result.exit_code == 0
        assert "cancelled" in result.output
        mock_client.post.assert_called_once_with("jpm", "jobs/1/cancel")


class TestAppointments:
    def test_list(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope([APPOINTMENT])
        result = invoke(["jobs", "appointments-list"])
        assert result.exit_code == 0

    def test_get(self, invoke, mock_client):
        mock_client.get.return_value = APPOINTMENT
        result = invoke(["jobs", "appointments-get", "1"])
        assert result.exit_code == 0


class TestProjects:
    def test_list(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope([PROJECT])
        result = invoke(["jobs", "projects-list"])
        assert result.exit_code == 0

    def test_get(self, invoke, mock_client):
        mock_client.get.return_value = PROJECT
        result = invoke(["jobs", "projects-get", "1"])
        assert result.exit_code == 0


class TestTheJobNumberColumn:
    """`number` is blank on every ServiceTitan job; `jobNumber` is the field.

    The exporter shipped the identical bug and measured it: 0 non-empty cells
    across 2431 live rows. This column has been permanently blank in
    `st jobs list` for the same reason.
    """

    def test_the_number_column_reads_jobnumber(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope([{"id": 1, "jobNumber": "J-777"}])
        result = invoke(["jobs", "list"])
        assert "J-777" in result.output

    def test_number_is_still_accepted_as_a_fallback(self, invoke, mock_client):
        # Widen-only: a tenant (or a recorded fixture) that answers with the old
        # spelling must not start showing a blank column.
        mock_client.get.return_value = make_envelope([{"id": 1, "number": "J-888"}])
        result = invoke(["jobs", "list"])
        assert "J-888" in result.output

    def test_jobnumber_wins_when_both_are_present(self, invoke, mock_client):
        mock_client.get.return_value = make_envelope(
            [{"id": 1, "jobNumber": "J-999", "number": "wrong"}]
        )
        result = invoke(["jobs", "list"])
        assert "J-999" in result.output

    def test_a_project_still_reads_its_own_number_field(self, invoke, mock_client):
        # `number` is legitimate on projects — only the JOB object renamed it.
        mock_client.get.return_value = make_envelope([PROJECT])
        result = invoke(["jobs", "projects-list"])
        assert "P-001" in result.output
