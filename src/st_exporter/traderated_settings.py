"""TradeRated Outbox settings, loaded from environment only.

Mirrors ``st_exporter.config.ExporterSettings``'s no-``.env``-fallback rule — a
GitHub Actions runner has none, so there must be no code path that reads one.

Optional by design: ticket 06 must be buildable and mergeable before ticket 07
issues the machine token and outbox URL (the handoff brief: "Neither blocks
ticket 05... build... and swap the target when 07 lands"). ``cli.py`` skips the
outbox drain entirely, with a log line, when ``configured`` is ``False``.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradeRatedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRADERATED_")

    machine_token: str | None = Field(default=None, repr=False)
    outbox_base_url: str | None = None

    @property
    def configured(self) -> bool:
        return self.machine_token is not None and self.outbox_base_url is not None
