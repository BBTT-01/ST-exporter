"""Cell-text formatting for the Export Store.

Every cell in the Export Store is written as text — Sheets is asked to store the
value literally (``value_input_option=RAW``, see ``sheets.py``) rather than parse
it, so a `"40.7128"` cell is never silently reinterpreted as a number and a
`"2026-09-03T10:00:00-05:00"` cell is never truncated to a bare date. ``to_cell_text``
is the single funnel every cell passes through so that rule can't be violated ad hoc
elsewhere in the codebase.
"""

from __future__ import annotations

from typing import Any

from st_exporter.logging_setup import logger

#: The `jobs` tab contract. **v2, not v1** — 0.2.7 changed this tab's GRAIN from
#: one row per appointment to one row per assigned technician, which broke
#: ``st_appointment_id``'s uniqueness and broke a consumer, silently, in
#: production. That change is exactly what a contract version exists to announce,
#: so the pre-0.2.7 shape (still sitting in every Sheet written by <= 0.2.6, where
#: `_meta.contract_version` is blank) is `jobs.v1` and today's shape is `jobs.v2`.
#: Calling today's shape v1 would hand the name "v1" to two different tab shapes.
JOBS_CONTRACT_VERSION = "jobs.v2"

#: The `technicians` tab contract. Never changed shape since first release, so v1.
TECHNICIANS_CONTRACT_VERSION = "technicians.v1"

# Column order is frozen by spec.md — TradeRated's reader depends on it exactly.
JOB_COLUMNS: tuple[str, ...] = (
    "st_job_id",
    "st_appointment_id",
    "job_number",
    "st_technician_id",
    "customer_name",
    "customer_phone",
    "customer_email",
    "service_address",
    "latitude",
    "longitude",
    "appointment_start",
    "appointment_end",
    "job_status",
    "job_type",
    "summary",
    "business_unit",
    "modified_on",
    # APPENDED, deliberately last. Appending is the one column change that is
    # additive under `docs/export-contract.md` — consumers look columns up by
    # name and ignore the rest — so `jobs.v2` does not become `jobs.v3`.
    "completed_on",
    "total_revenue",
    # APPENDED after those, for Profit Wizard hosted parity — same rule. Read
    # straight off the job record; every one is blank when ServiceTitan's export
    # change-feed does not carry the field, never a guessed "0"/"false".
    "recall_for_id",
    "warranty_id",
    "no_charge",
    "total",
    "business_unit_id",
    "sold_by_id",
    # APPENDED last for TrueQuote hosted calibration (booking -> jobs -> invoices).
    "booking_id",
)

TECHNICIAN_COLUMNS: tuple[str, ...] = (
    "st_technician_id",
    "name",
    "email",
    "active",
    # APPENDED, deliberately last, for Profit Wizard hosted parity — see
    # `jobs.completed_on` above for why appending does not bump `technicians.v1`.
    "phone",
    "business_unit_id",
    "business_unit_name",
    "role_ids",
    "home_address",
    "home_latitude",
    "home_longitude",
)

_ADDRESS_PARTS = ("street", "unit", "city", "state", "zip")


def to_cell_text(value: Any) -> str:
    """Format one value as the literal text that goes into a Sheet cell.

    ``None`` -> ``""`` (blank, never the string "None"). Booleans serialise as
    lowercase ``"true"``/``"false"`` to match ServiceTitan's own JSON convention,
    not Python's ``str(bool)`` (which would give ``"True"``). Everything else goes
    through ``str()`` — floats use Python's shortest round-trip ``repr`` via
    ``str()``, which is deterministic across runs given the same input.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_service_address(address: dict[str, Any] | None) -> str:
    """Join a ServiceTitan address object into the single-line ``service_address`` cell.

    Skips parts that are missing or blank rather than emitting empty segments
    (e.g. no dangling ``", "`` when ``unit`` is absent).
    """
    if not address:
        return ""
    parts = [str(address[key]).strip() for key in _ADDRESS_PARTS if address.get(key)]
    return ", ".join(parts)


def format_job_row(row: dict[str, Any]) -> list[str]:
    """Render one denormalised job dict into the exact `jobs` tab column order."""
    return [to_cell_text(row.get(col)) for col in JOB_COLUMNS]


def format_technician_row(row: dict[str, Any]) -> list[str]:
    """Render one technician dict into the exact `technicians` tab column order."""
    return [to_cell_text(row.get(col)) for col in TECHNICIAN_COLUMNS]


def build_job_grid(rows: list[dict[str, Any]]) -> list[list[str]]:
    """Header row + one row per denormalised `jobs` row, in frozen column order.

    Pure and client-free for the same reason ``pricebook.build_item_grid`` is: the
    exact grid a run would write has to be derivable from records alone, so the
    contract fixtures (``contracts/fixtures/``) can pin it.
    """
    return [list(JOB_COLUMNS)] + [format_job_row(row) for row in rows]


def build_technician_grid(
    records: list[dict[str, Any]],
    business_units: dict[str, dict[str, Any]] | None = None,
) -> list[list[str]]:
    """Header row + one deduped row per ServiceTitan technician record.

    The whole `technicians` tab, derived from raw records alone — mapping, dedupe
    and formatting in one pure call, so both ``run.py`` and the contract fixtures
    go through the same code path rather than two that can drift.

    ``business_units`` is the SAME reference lookup the `jobs` tab's
    ``business_unit`` column resolves against (id -> record, see
    ``feeds.reference.fetch_business_units``) — one reference table, read the same
    way by both tabs, so a business unit's name can never disagree between them.
    ``None``/``{}`` degrades to a blank ``business_unit_name`` rather than raising.
    """
    rows = dedupe_technician_rows([technician_row(record, business_units) for record in records])
    return [list(TECHNICIAN_COLUMNS)] + [format_technician_row(row) for row in rows]


def technician_row(
    record: dict[str, Any],
    business_units: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Map one ServiceTitan technician record onto the technician-tab columns.

    ``phone``/``business_unit_id``/``role_ids``/``home_*`` are unverified against
    a real tenant (see ``KNOWN_UNVERIFIED.md``, "Technician phone/business
    unit/roles/home address fields") — each reads the documented-looking spelling
    first and widens to an alternate one rather than guessing a single name, the
    same defensive shape ``denormalize._contact_detail`` already uses for
    customer contacts.
    """
    business_units = business_units or {}
    business_unit_id = record.get("businessUnitId")
    home = _obj(record.get("homeAddress")) or _obj(record.get("home"))
    home_latitude, home_longitude = _home_coordinate(home)
    return {
        "st_technician_id": record.get("id"),
        "name": record.get("name"),
        "email": record.get("email"),
        "active": record.get("active"),
        "phone": record.get("phoneNumber") or record.get("phone"),
        "business_unit_id": business_unit_id,
        "business_unit_name": _reference_name(business_unit_id, business_units),
        "role_ids": _joined_ids(record.get("roleIds")),
        "home_address": build_service_address(home) if home else "",
        "home_latitude": home_latitude,
        "home_longitude": home_longitude,
    }


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _reference_name(record_id: Any, reference: dict[str, dict[str, Any]]) -> str | None:
    """The ``name`` of a reference-table entry (business units), or ``None``.

    Same lookup shape as ``denormalize._business_unit_name`` — kept as a separate
    copy here (rather than imported) because ``denormalize`` imports FROM this
    module, and importing back would create a cycle.
    """
    if record_id is None:
        return None
    ref = reference.get(str(record_id))
    return str(ref["name"]) if ref and ref.get("name") else None


def _joined_ids(values: Any) -> str | None:
    """``[1, 2, 3]`` -> ``"1,2,3"``; blank (``None``) for anything else, never ``"0"``."""
    if not isinstance(values, list) or not values:
        return None
    return ",".join(str(v) for v in values if v is not None)


def _home_coordinate(home: dict[str, Any]) -> tuple[Any, Any]:
    """A technician's home latitude/longitude, or (None, None) if absent.

    Membership-tested, not truthiness-tested, so a real ``0.0`` at the equator or
    prime meridian is never mistaken for "missing" — same rule
    ``denormalize._coordinate`` applies to a job's location.
    """
    if not home:
        return None, None
    lat = home.get("latitude") if "latitude" in home else None
    lng = home.get("longitude") if "longitude" in home else None
    return lat, lng


def dedupe_technician_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeats of the same technician, keeping the first occurrence.

    The dedupe key is ``st_technician_id`` — the tab's own identity, and the key
    the `jobs` tab's ``st_technician_id`` column joins against. A second row for
    an id already emitted carries no information the first doesn't, so dropping
    it is always safe; it also makes the tab robust against the list endpoint
    returning a record twice across page boundaries.

    It is deliberately NOT ``email``. Two DISTINCT technician ids sharing one
    address is a real ServiceTitan state (a re-created technician record, or a
    shop's shared inbox on several techs), and both of those technicians can be
    assigned to jobs — so both must appear here or a `jobs` row would reference a
    technician missing from the tab. A downstream unique-email constraint is the
    downstream's to resolve; we log the collision rather than silently deleting a
    real technician to satisfy it. Rows with no id are all kept, for the same
    "never drop a real technician" reason.
    """
    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    duplicate_id_count = 0
    for row in rows:
        identifier = row.get("st_technician_id")
        if identifier is not None:
            key = str(identifier)
            if key in seen_ids:
                duplicate_id_count += 1
                continue
            seen_ids.add(key)
        deduped.append(row)

    if duplicate_id_count:
        logger.warning(
            "technicians: dropped %d duplicate row(s) for an already-exported st_technician_id",
            duplicate_id_count,
        )

    ids_by_email: dict[str, list[str]] = {}
    for row in deduped:
        email = str(row.get("email") or "").strip().lower()
        if not email:
            continue
        ids_by_email.setdefault(email, []).append(str(row.get("st_technician_id")))
    for email, ids in ids_by_email.items():
        if len(ids) > 1:
            logger.warning(
                "technicians: %s is shared by %d distinct technician ids (%s); all "
                "are exported — a consumer requiring unique emails must resolve it",
                email,
                len(ids),
                ", ".join(ids),
            )

    return deduped
