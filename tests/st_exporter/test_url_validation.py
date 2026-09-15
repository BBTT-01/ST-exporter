"""Tests for the outbox-URL guard.

Every case here is about one thing: an environment variable set in a
contractor's own repository decides where a bearer Machine Token is sent, so a
value we would not trust must be refused before any client is built.
"""

from __future__ import annotations

import pytest

from st_cli.exceptions import ConfigError, STCLIError
from st_exporter.url_validation import InvalidOutboxUrlError, validate_outbox_url

ENV_VAR = "TRUEQUOTE_OUTBOX_URL"


class TestAccepted:
    @pytest.mark.parametrize(
        "url",
        [
            "https://tq.example.com",
            "https://tq.example.com/api/outbox",
            "https://tq.example.com/api/outbox/",
            "https://ref.supabase.co/functions/v1",
            "https://tq.example.com:8443/api/outbox",
            "https://tq",  # a bare host is legal; there is no allowlist
        ],
    )
    def test_https_urls_pass_through_unchanged(self, url: str) -> None:
        assert validate_outbox_url(url, ENV_VAR) == url

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_unset_or_empty_stays_valid(self, value: str | None) -> None:
        """An unbought product's secrets are absent, and GitHub Actions gives an
        unset secret as the empty string. Neither is a misconfiguration."""
        assert validate_outbox_url(value, ENV_VAR) == value


class TestRefused:
    def test_http_is_refused(self) -> None:
        with pytest.raises(InvalidOutboxUrlError) as excinfo:
            validate_outbox_url("http://tq.example.com/api/outbox", ENV_VAR)
        assert "http://" in str(excinfo.value)
        assert "unencrypted" in str(excinfo.value)

    def test_a_non_http_scheme_is_refused(self) -> None:
        with pytest.raises(InvalidOutboxUrlError):
            validate_outbox_url("ftp://tq.example.com", ENV_VAR)

    @pytest.mark.parametrize(
        "url",
        ["tq.example.com/api/outbox", "//tq.example.com/api/outbox", "not a url at all"],
    )
    def test_a_missing_scheme_is_refused(self, url: str) -> None:
        with pytest.raises(InvalidOutboxUrlError) as excinfo:
            validate_outbox_url(url, ENV_VAR)
        assert "scheme" in str(excinfo.value)

    @pytest.mark.parametrize("url", ["https://", "https:///api/outbox"])
    def test_an_empty_host_is_refused(self, url: str) -> None:
        with pytest.raises(InvalidOutboxUrlError) as excinfo:
            validate_outbox_url(url, ENV_VAR)
        assert "no host" in str(excinfo.value)

    @pytest.mark.parametrize(
        "url",
        [
            "https://user:pass@tq.example.com/api/outbox",
            "https://tq.example.com@evil.example/api/outbox",
        ],
    )
    def test_embedded_credentials_are_refused(self, url: str) -> None:
        """The second form is the dangerous one: the real host is `evil.example`
        and everything before the `@` is a username that merely reads right."""
        with pytest.raises(InvalidOutboxUrlError) as excinfo:
            validate_outbox_url(url, ENV_VAR)
        assert "credentials" in str(excinfo.value)

    def test_the_password_is_not_echoed_back(self) -> None:
        with pytest.raises(InvalidOutboxUrlError) as excinfo:
            validate_outbox_url("https://user:hunter2@tq.example.com", ENV_VAR)
        assert "hunter2" not in str(excinfo.value)

    def test_a_query_string_is_refused(self) -> None:
        with pytest.raises(InvalidOutboxUrlError) as excinfo:
            validate_outbox_url("https://tq.example.com/api/outbox?token=leak", ENV_VAR)
        assert "query string" in str(excinfo.value)

    def test_a_fragment_is_refused(self) -> None:
        with pytest.raises(InvalidOutboxUrlError) as excinfo:
            validate_outbox_url("https://tq.example.com/api/outbox#frag", ENV_VAR)
        assert "fragment" in str(excinfo.value)

    def test_an_unparseable_url_is_refused(self) -> None:
        with pytest.raises(InvalidOutboxUrlError):
            validate_outbox_url("https://tq.example.com:notaport/api", ENV_VAR)

    def test_localhost_gets_no_http_exception(self) -> None:
        """Nothing in this repository runs an outbox locally, so there is no
        development carve-out to aim at."""
        with pytest.raises(InvalidOutboxUrlError):
            validate_outbox_url("http://localhost:3000", ENV_VAR)
        with pytest.raises(InvalidOutboxUrlError):
            validate_outbox_url("http://127.0.0.1:3000", ENV_VAR)


class TestTheMessageAnOperatorReads:
    def test_it_names_the_offending_environment_variable(self) -> None:
        with pytest.raises(InvalidOutboxUrlError) as excinfo:
            validate_outbox_url("http://pw.example.com", "PROFITWIZARD_OUTBOX_URL")
        message = str(excinfo.value)
        assert message.count("PROFITWIZARD_OUTBOX_URL") == 2  # what is wrong, and what to set
        assert excinfo.value.env_var == "PROFITWIZARD_OUTBOX_URL"
        # The whole point is that a GitHub Actions log is all the operator has:
        # it must say what to fix without reading our source.
        assert "https://" in message
        assert "http://pw.example.com" in message

    def test_it_takes_the_cli_s_clean_error_path(self) -> None:
        """``cli.main`` catches ``STCLIError``, so this prints ``Error: ...`` and
        exits 1 instead of dumping a traceback into the Actions log."""
        assert issubclass(InvalidOutboxUrlError, ConfigError)
        assert issubclass(InvalidOutboxUrlError, STCLIError)

    def test_it_is_not_a_valueerror(self) -> None:
        """Deliberate: pydantic wraps ``ValueError`` from a field validator in a
        ``ValidationError`` whose text buries the sentence above. Anything else
        it re-raises untouched."""
        assert not issubclass(InvalidOutboxUrlError, ValueError)
