"""Refuse an outbox base URL before a Machine Token is ever sent to it.

Every outbox lane authenticates with ``Authorization: Bearer <machine token>``
against whatever host an environment variable names, and those variables are
repository secrets in a *contractor's own* GitHub repository. A typo, a
copy-paste from the wrong environment, or a hostile value therefore hands a
credential that authenticates against that contractor's queue to a host of
someone else's choosing. The only processing these values used to get was
``.rstrip("/")``.

So the check lives at the boundary where a value enters the process, not at the
call site that sends the request: by the time an ``httpx.Client`` exists the
token is already in a header.

What is checked, and why only this:

* **https only.** A token over plaintext is the failure this exists to prevent.
  There is no ``http://localhost`` exception — nothing in this repository runs
  an outbox against a local host (no test, script, workflow or doc mentions
  one), and an exception nobody needs is an exception an attacker can aim at.
* **Absolute, with a host.** ``urlparse`` must make sense of it and it must name
  somewhere to connect to.
* **No embedded credentials, query string or fragment.** A base URL that paths
  are appended to has no business carrying any of the three, and
  ``https://real.example.com@evil.example`` is a host confusion that reads as
  legitimate.

Deliberately **no host allowlist**: each app is deployed at its own host and
those change per contractor and per deployment, so an allowlist would break real
installations and would need editing for every new one.

The error is an :class:`~st_cli.exceptions.ConfigError` subclass, which gets
``st-export``'s clean ``Error: ...`` + exit-1 treatment in ``cli.main`` instead
of a raw traceback, and — because it is not a ``ValueError`` — pydantic
propagates it out of a field validator *unwrapped*, so the operator reading a
GitHub Actions log sees the sentence below and nothing else.
"""

from __future__ import annotations

from urllib.parse import urlparse

from st_cli.exceptions import ConfigError

_EXAMPLE = "https://app.example.com/api/outbox"


class InvalidOutboxUrlError(ConfigError):
    """A configured outbox URL is one we refuse to send a Machine Token to."""

    def __init__(self, env_var: str, value: str, reason: str) -> None:
        self.env_var = env_var
        self.reason = reason
        super().__init__(
            f"{env_var} is not a usable outbox URL: {reason}. "
            f"Set {env_var} to an absolute https:// URL with a host and no "
            f"credentials, query string or fragment — for example {_EXAMPLE}. "
            f"Got: {_redact(value)!r}"
        )


def _redact(value: str) -> str:
    """The value as it may be printed. Strips any ``user:pass@`` it carries."""
    scheme, _, rest = value.partition("://")
    if not rest or "@" not in rest.split("/")[0]:
        return value
    authority, _, tail = rest.partition("/")
    _, _, host = authority.rpartition("@")
    return f"{scheme}://<redacted>@{host}" + (f"/{tail}" if tail else "")


def validate_outbox_url(value: str | None, env_var: str) -> str | None:
    """Return ``value`` unchanged, or raise :class:`InvalidOutboxUrlError`.

    ``None`` and empty/whitespace-only values pass: an unbought product's
    secrets are simply absent, and GitHub Actions maps an ``env:`` entry for an
    unset secret to the empty string. Those are the normal case, not an error —
    callers already treat them as "this lane is not configured".
    """
    if value is None or not value.strip():
        return value

    candidate = value.strip()
    try:
        parsed = urlparse(candidate)
        host = parsed.hostname
        username, password = parsed.username, parsed.password
        _ = parsed.port  # raises ValueError on a non-numeric/out-of-range port
    except ValueError as exc:
        raise InvalidOutboxUrlError(env_var, candidate, f"it could not be parsed ({exc})") from exc

    if not parsed.scheme:
        raise InvalidOutboxUrlError(
            env_var, candidate, "it has no scheme, so it is not an absolute URL"
        )
    if parsed.scheme == "http":
        raise InvalidOutboxUrlError(
            env_var,
            candidate,
            "it uses http://, which would send the Machine Token over an unencrypted connection",
        )
    if parsed.scheme != "https":
        raise InvalidOutboxUrlError(
            env_var, candidate, f"its scheme is {parsed.scheme!r}, not https"
        )
    if not host:
        raise InvalidOutboxUrlError(env_var, candidate, "it names no host to connect to")
    if username or password:
        raise InvalidOutboxUrlError(
            env_var,
            candidate,
            "it embeds credentials (the user:pass@host form), which hides the "
            "real host and is never how an outbox is addressed",
        )
    if parsed.query:
        raise InvalidOutboxUrlError(
            env_var, candidate, "it carries a query string, which a base URL must not"
        )
    if parsed.fragment:
        raise InvalidOutboxUrlError(
            env_var, candidate, "it carries a URL fragment, which a base URL must not"
        )

    return value
