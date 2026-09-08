"""ST-exporter: wraps st_cli to produce the ServiceTitan Hosted Export Store.

Reads ServiceTitan change feeds via the (upstream) ``st_cli`` package, denormalises
jobs/appointments/customers/locations into one row per appointment, applies the
90-day-or-future window, and writes the result to a Google Sheet that TradeRated
reads read-only. The tab contract (``jobs``/``technicians``/``_meta``) is frozen by
``spec.md`` on the TradeRated side; see ``KNOWN_UNVERIFIED.md`` for field-mapping
assumptions that need confirming once real ServiceTitan credentials exist.
"""

from __future__ import annotations

EXPORTER_VERSION = "0.2.5"
