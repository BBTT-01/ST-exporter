"""ST-exporter: wraps st_cli to produce the ServiceTitan Hosted Export Store.

Reads ServiceTitan change feeds via the (upstream) ``st_cli`` package, denormalises
jobs/appointments/customers/locations into one row per appointment, applies the
90-day-or-future window, and writes the result to a Google Sheet that TradeRated
reads read-only. The tab contract (``jobs``/``technicians``/``_meta``) is frozen by
``spec.md`` on the TradeRated side; see ``KNOWN_UNVERIFIED.md`` for field-mapping
assumptions that need confirming once real ServiceTitan credentials exist.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

try:
    # Single source of truth: `pyproject.toml`'s `version`, read back off the
    # installed distribution. The workflow does a fresh `pip install -e .` of the
    # code it just checked out, so this always describes THAT code — which is the
    # whole point, since the version guard compares it against the pinned tag.
    #
    # Caveat for local work: an editable install snapshots the version at install
    # time, so after bumping `pyproject.toml` you must reinstall before this
    # reflects it. Harmless in CI (always a fresh install); confusing on a laptop.
    EXPORTER_VERSION = _distribution_version("st-cli")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    EXPORTER_VERSION = "0.0.0+unknown"
