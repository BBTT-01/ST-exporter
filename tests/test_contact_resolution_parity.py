"""The drift guard: two implementations of one "first non-empty" rule.

``st_cli.output._resolve`` (the ``a|b`` alternation DSL, for CLI table columns)
and ``st_exporter.denormalize._contact_detail`` (the exporter's customer
phone/email) apply the same rule to the same ServiceTitan shapes. They are kept
separate on purpose — one is a generic path resolver over arbitrary column keys,
the other knows what a customer contact is — but they have already drifted apart
once, and that drift *was* the bug round two fixed: the DSL returned the blank
``phoneSettings[0].phone`` while the exporter fell through to the populated flat
``phone``.

So they are pinned here, against ONE shared matrix, rather than by two sets of
tests that can be updated independently. Every case below is run through both.

**The differences asserted here are intended, not drift.** They are also
documented at ``_resolve``'s own docstring:

- *Array position.* A column key names an index (``phoneSettings.0.phone``) and
  the DSL resolves exactly what it says; ``_contact_detail`` scans every entry
  for the first non-empty one. The CLI table is a debugging view where "index 0
  is blank" is itself the fact worth seeing; the exporter's tab is a frozen
  contract column read by another system, where a blank cell is
  indistinguishable from a contractor with no phone number.
- *Spelling order.* Each caller owns its own order. Both are widen-only, so the
  order only ever decides between two populated values.
- *``contacts[]``.* Only ``_contact_detail`` selects ``contacts[]`` by ``type``;
  no path DSL can express "the entry whose type is Phone".

If you change either implementation and this file goes red, the question is
which of the two is now wrong — not which assertion to relax.
"""

from __future__ import annotations

import pytest

from st_cli.output import _resolve
from st_exporter.denormalize import _contact_detail

#: One customer shape per row, plus what each side is expected to return.
#: ``dsl_key`` is spelled the way ``crm.CUSTOMER_COLUMNS`` spells it.
_DSL_KEY = "phoneSettings.0.phoneNumber|phoneSettings.0.phone|phone"
_FIELDS = ("phone", "phoneNumber", "number")

# (id, customer, expected) — cases where the two MUST agree.
AGREEING_MATRIX: list[tuple[str, dict, object]] = [
    (
        "missing key entirely",
        {"id": 1, "name": "Jane"},
        None,
    ),
    (
        "empty settings array",
        {"phoneSettings": [], "phone": "555-1111"},
        "555-1111",
    ),
    (
        "blank entry beside a populated flat scalar — THE round-two bug",
        {"phoneSettings": [{"phone": ""}], "phone": "555-1111"},
        "555-1111",
    ),
    (
        "populated entry wins over the flat scalar",
        {"phoneSettings": [{"phone": "555-2222"}], "phone": "555-1111"},
        "555-2222",
    ),
    (
        "the documented phoneNumber spelling is read",
        {"phoneSettings": [{"phoneNumber": "555-4444", "doNotText": False}]},
        "555-4444",
    ),
    (
        "flat scalar only",
        {"phone": "555-1111"},
        "555-1111",
    ),
    (
        "no settings array and no scalar",
        {"emailSettings": [{"email": "a@b.test"}]},
        None,
    ),
    (
        "a real 0 is a value, not an absence",
        {"phone": 0},
        0,
    ),
    (
        "blank everywhere is still 'present but blank', not absent",
        {"phoneSettings": [{"phone": ""}], "phone": ""},
        "",
    ),
]


@pytest.mark.parametrize(
    "case,customer,expected", AGREEING_MATRIX, ids=[c[0] for c in AGREEING_MATRIX]
)
def test_the_cli_dsl_resolves_the_shared_matrix(
    case: str, customer: dict, expected: object
) -> None:
    assert _resolve(customer, _DSL_KEY) == expected


@pytest.mark.parametrize(
    "case,customer,expected", AGREEING_MATRIX, ids=[c[0] for c in AGREEING_MATRIX]
)
def test_the_exporter_resolves_the_shared_matrix_identically(
    case: str, customer: dict, expected: object
) -> None:
    assert _contact_detail(customer, settings_key="phoneSettings", fields=_FIELDS) == expected


@pytest.mark.parametrize(
    "case,customer,_expected", AGREEING_MATRIX, ids=[c[0] for c in AGREEING_MATRIX]
)
def test_both_implementations_agree(case: str, customer: dict, _expected: object) -> None:
    """The assertion that actually catches drift: same input, same answer."""
    assert _resolve(customer, _DSL_KEY) == _contact_detail(
        customer, settings_key="phoneSettings", fields=_FIELDS
    )


class TestTheDeliberateDifferences:
    """Pinned so a future reader can tell "intended" from "drifted"."""

    def test_the_dsl_reads_index_0_only_while_the_exporter_scans_the_array(self) -> None:
        customer = {"phoneSettings": [{"phone": ""}, {"phone": "555-3333"}]}
        # The column key says `.0.`, so that is what it resolves — "index 0 is
        # blank" is the useful fact in a debugging table.
        assert _resolve(customer, _DSL_KEY) == ""
        # The tab has one cell and a blank one reads as "no phone number", so
        # the exporter keeps looking.
        assert _contact_detail(customer, settings_key="phoneSettings", fields=_FIELDS) == "555-3333"
        # A CLI column may always ask for a later index explicitly.
        assert _resolve(customer, "phoneSettings.1.phone") == "555-3333"

    def test_only_the_exporter_knows_about_contacts(self) -> None:
        customer = {"contacts": [{"type": "Phone", "value": "555-7777"}]}
        # No path DSL can express "the entry whose type is Phone".
        assert _resolve(customer, _DSL_KEY) is None
        assert (
            _contact_detail(
                customer,
                settings_key="phoneSettings",
                fields=_FIELDS,
                contact_types=("phone", "mobilephone"),
            )
            == "555-7777"
        )

    def test_spelling_order_only_matters_when_both_are_populated(self) -> None:
        customer = {"phoneSettings": [{"phone": "555-2222", "phoneNumber": "555-4444"}]}
        # crm.CUSTOMER_COLUMNS puts the documented spelling first...
        assert _resolve(customer, _DSL_KEY) == "555-4444"
        # ...the exporter's `fields` tuple puts `phone` first. Both are
        # widen-only; neither can blank a column that used to resolve.
        assert _contact_detail(customer, settings_key="phoneSettings", fields=_FIELDS) == "555-2222"
