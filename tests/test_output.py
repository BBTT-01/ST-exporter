"""Tests for output.py."""

from __future__ import annotations

import json

from st_cli.output import _resolve, render, render_single


class TestResolve:
    def test_flat_key(self):
        assert _resolve({"name": "Acme"}, "name") == "Acme"

    def test_nested_key(self):
        assert _resolve({"address": {"city": "Austin"}}, "address.city") == "Austin"

    def test_missing_key_returns_none(self):
        assert _resolve({"name": "Acme"}, "email") is None

    def test_missing_nested_returns_none(self):
        assert _resolve({"address": {}}, "address.city") is None

    def test_deeply_nested(self):
        data = {"a": {"b": {"c": "deep"}}}
        assert _resolve(data, "a.b.c") == "deep"


class TestResolveAlternation:
    """`a|b` widens a spelling. The FIRST NON-EMPTY alternative wins.

    "First not-None" was wrong on the shape ServiceTitan actually returns:
    `phoneSettings: [{"phone": ""}]` next to a populated flat `phone` hid a
    number that was right there — the blank-column failure this DSL exists to
    prevent, caused by the DSL itself.
    """

    def test_the_first_alternative_wins_when_it_is_populated(self):
        assert _resolve({"jobNumber": "J-1", "number": "9"}, "jobNumber|number") == "J-1"

    def test_a_missing_first_alternative_falls_through(self):
        assert _resolve({"number": "9"}, "jobNumber|number") == "9"

    def test_a_blank_first_alternative_falls_through(self):
        rec = {"phoneSettings": [{"phone": ""}], "phone": "555-0199"}
        assert _resolve(rec, "phoneSettings.0.phone|phone") == "555-0199"

    def test_a_whitespace_only_alternative_falls_through(self):
        assert _resolve({"a": "   ", "b": "real"}, "a|b") == "real"

    def test_blank_everywhere_still_reports_present_but_blank(self):
        # "" and None are different facts; neither alternative had anything, so
        # the value that WAS there (blank) is returned rather than invented.
        assert _resolve({"a": "", "b": ""}, "a|b") == ""
        assert _resolve({}, "a|b") is None

    def test_zero_is_a_value_not_a_blank(self):
        assert _resolve({"a": 0, "b": 5}, "a|b") == 0


class TestRender:
    def test_json_output(self, capsys):
        data = [{"id": 1, "name": "Acme"}]
        columns = [("ID", "id"), ("Name", "name")]
        render(data, columns, as_json=True)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed == data

    def test_table_output_contains_data(self, capsys):
        data = [{"id": 1, "name": "Acme"}]
        columns = [("ID", "id"), ("Name", "name")]
        render(data, columns, as_json=False, title="Test")
        captured = capsys.readouterr()
        assert "Acme" in captured.out
        assert "Test" in captured.out

    def test_total_count_shown(self, capsys):
        data = [{"id": 1}]
        columns = [("ID", "id")]
        render(data, columns, as_json=False, total_count=42)
        captured = capsys.readouterr()
        assert "42" in captured.out


class TestRenderSingle:
    def test_json_output(self, capsys):
        record = {"id": 1, "name": "Acme"}
        columns = [("ID", "id"), ("Name", "name")]
        render_single(record, columns, as_json=True)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed == record

    def test_table_output_contains_data(self, capsys):
        record = {"id": 1, "name": "Acme"}
        columns = [("ID", "id"), ("Name", "name")]
        render_single(record, columns, as_json=False)
        captured = capsys.readouterr()
        assert "Acme" in captured.out
        assert "ID" in captured.out
