# Contract fixtures

One recorded set of tab rows per contract version, committed so that the exporter
that WRITES the Export Store and the three apps that READ it can each assert
against the same bytes. A column renamed on either side then fails in CI, on the
side that renamed it, instead of returning zero rows in production.

- `manifest.json` — the current contract version per feed, plus a `sha256` for
  every fixture file. Read versions from here, never from a hard-coded table.
- `<contract_version>/<tab>.json` — one tab: its `columns` (the header row) and
  `rows` (every cell a string), exactly as written to the Sheet, plus the `grain`
  and `row_key` that say what one row *is*.
- `published.json` — the sha256 of every file **as released**, per contract
  version. This is the only file here that is not derived from the current code,
  and that is the point: a released version's bytes are frozen, so a contract
  change means a NEW version directory. The generator refuses to rewrite a listed
  version and names the one to bump instead.

**Generated, never hand-edited** (`python scripts/gen_contract_fixtures.py`); a
hand edit is caught by the manifest checksums, and a change to an already-released
file is caught by `published.json` whether it was hand-made or regenerated.

**Synthetic, never real.** This repo is public. No real customer name, address,
phone, email, id or price may ever appear here.

Everything a consumer needs — how to fetch these at a pinned tag, what to do on an
unsupported version, what forces a version bump, and the scrubbing rule for any
future recording — is in **`docs/export-contract.md`** in this repo.
