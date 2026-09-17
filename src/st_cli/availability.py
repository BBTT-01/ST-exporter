"""Pure functions for technician availability / who-busy logic.

Shared by the Typer CLI (dispatch.py) and the MCP server.
No CLI or Rich dependencies here — just data in, data out.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any


def _parse_dt(val: Any) -> datetime | None:
    """Best-effort parse an ISO datetime string."""
    if not val or not isinstance(val, str):
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours_between(start: datetime | None, end: datetime | None) -> float:
    if start and end and end > start:
        return (end - start).total_seconds() / 3600
    return 0.0


def _build_busy_summary(
    shifts: list[dict[str, Any]],
    appointments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cross-reference shifts and appointments to build per-tech availability."""
    appts_by_tech: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for appt in appointments:
        tech_id = appt.get("technicianId")
        if tech_id:
            appts_by_tech[tech_id].append(appt)

    shifts_by_tech: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for shift in shifts:
        tech_id = shift.get("technicianId")
        if tech_id:
            shifts_by_tech[tech_id].append(shift)

    rows = []
    for tech_id in sorted(shifts_by_tech.keys()):
        tech_shifts = shifts_by_tech[tech_id]
        tech_appts = appts_by_tech.get(tech_id, [])

        shift_hours = 0.0
        for s in tech_shifts:
            shift_hours += _hours_between(_parse_dt(s.get("start")), _parse_dt(s.get("end")))

        booked_hours = 0.0
        for a in tech_appts:
            booked_hours += _hours_between(_parse_dt(a.get("start")), _parse_dt(a.get("end")))

        available_hours = max(0.0, shift_hours - booked_hours)
        tech_name = tech_shifts[0].get("technicianName") or str(tech_id)
        pct_booked = (booked_hours / shift_hours * 100) if shift_hours > 0 else 0.0

        rows.append(
            {
                "technicianId": tech_id,
                "technicianName": tech_name,
                "shifts": len(tech_shifts),
                "shiftHours": round(shift_hours, 1),
                "appointments": len(tech_appts),
                "bookedHours": round(booked_hours, 1),
                "availableHours": round(available_hours, 1),
                "percentBooked": round(pct_booked, 1),
            }
        )

    return rows
