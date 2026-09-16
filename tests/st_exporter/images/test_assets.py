"""Tests for the pure asset rules — selection, sniffing, idempotency key.

Every expectation here is a mirror of TrueQuote's receiving side; where one of
these fails, the wire format has drifted, not the taste.
"""

from __future__ import annotations

import time_machine

from st_exporter.images.assets import (
    MIN_PLAUSIBLE_IMAGE_BYTES,
    describe_image_size,
    idempotency_key,
    image_dimensions,
    is_displayable,
    is_placeholder_image,
    is_storable,
    is_storage_path,
    looks_blank,
    select_uploadable_asset,
    sniff_content_type,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"rest"
JPEG = b"\xff\xd8\xff" + b"rest"
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"rest"


class TestIdentifierForms:
    def test_https_url_is_storable(self) -> None:
        assert is_storable("https://cdn.example.com/a1.jpg")

    def test_storage_path_is_storable(self) -> None:
        assert is_storage_path("Images/Pricebook/9f2c-uuid.jpg")
        assert is_storable("Images/Pricebook/9f2c-uuid.jpg")

    def test_plain_http_is_not_storable(self) -> None:
        assert not is_storable("http://cdn.example.com/a1.jpg")

    def test_traversal_is_not_a_storage_path(self) -> None:
        assert not is_storage_path("Images/../../etc/passwd")

    def test_bare_filename_is_not_a_storage_path(self) -> None:
        # The grammar needs at least one slash — a lone token is not a path.
        assert not is_storage_path("a1.jpg")


class TestIsDisplayable:
    def test_blank_type_still_counts_as_an_image(self) -> None:
        assert is_displayable({"url": "https://cdn.example.com/a.jpg"})

    def test_image_and_photo_types_pass(self) -> None:
        assert is_displayable({"url": "https://x/a.jpg", "type": "Image"})
        assert is_displayable({"url": "https://x/a.jpg", "type": "ProductPhoto"})

    def test_non_image_type_is_rejected(self) -> None:
        assert not is_displayable({"url": "https://x/a.pdf", "type": "Document"})


class TestSelectUploadableAsset:
    def test_prefers_the_default_asset(self) -> None:
        asset = select_uploadable_asset(
            {
                "id": 100,
                "assets": [
                    {"id": "a1", "url": "https://x/a1.jpg"},
                    {"id": "a2", "url": "https://x/a2.jpg", "isDefault": True},
                ],
            }
        )
        assert asset is not None
        assert asset.asset_id == "a2"
        assert asset.is_default is True

    def test_ties_break_on_the_id_filename_alias_url_tuple(self) -> None:
        asset = select_uploadable_asset(
            {
                "id": 100,
                "assets": [
                    {"id": "b", "url": "https://x/b.jpg"},
                    {"id": "a", "url": "https://x/a.jpg"},
                ],
            }
        )
        assert asset is not None and asset.asset_id == "a"

    def test_carries_the_metadata_the_endpoint_accepts(self) -> None:
        asset = select_uploadable_asset(
            {
                "id": 100,
                "assets": [
                    {
                        "id": "a1",
                        "url": "Images/Pricebook/9f2c.jpg",
                        "fileName": "door.jpg",
                        "alias": "Front",
                        "type": "Image",
                        "isDefault": True,
                    }
                ],
            }
        )
        assert asset is not None
        assert asset.external_item_id == "100"
        assert asset.source_url == "Images/Pricebook/9f2c.jpg"
        assert (asset.filename, asset.alias, asset.asset_type) == ("door.jpg", "Front", "Image")

    def test_item_with_no_usable_asset_returns_none(self) -> None:
        assert select_uploadable_asset({"id": 1, "assets": []}) is None
        assert select_uploadable_asset({"id": 1, "assets": [{"url": "ftp://x/a.jpg"}]}) is None

    def test_non_integer_item_id_is_dropped(self) -> None:
        # TrueQuote parses external_item_id with /^\d{1,18}$/ and 422s the rest.
        assert (
            select_uploadable_asset({"id": "abc", "assets": [{"url": "https://x/a.jpg"}]}) is None
        )

    def test_asset_identity_falls_back_to_the_url(self) -> None:
        asset = select_uploadable_asset(
            {"id": 7, "assets": [{"id": None, "url": "Images/Pricebook/9f2c.jpg"}]}
        )
        assert asset is not None
        assert asset.asset_id is None
        assert asset.identity == "Images/Pricebook/9f2c.jpg"


class TestSniffContentType:
    def test_recognises_the_three_types_truequote_accepts(self) -> None:
        assert sniff_content_type(PNG) == "image/png"
        assert sniff_content_type(JPEG) == "image/jpeg"
        assert sniff_content_type(WEBP) == "image/webp"

    def test_unknown_bytes_return_none(self) -> None:
        assert sniff_content_type(b"GIF89a....") is None
        assert sniff_content_type(b"") is None


class TestIdempotencyKey:
    def _asset(self, **overrides):
        record = {
            "id": 100,
            "assets": [{"id": "a1", "url": "https://x/a1.jpg", **overrides}],
        }
        asset = select_uploadable_asset(record)
        assert asset is not None
        return asset

    def test_same_asset_same_bytes_gives_the_same_key(self) -> None:
        assert idempotency_key(self._asset(), PNG) == idempotency_key(self._asset(), PNG)

    def test_changed_bytes_give_a_new_key_so_the_image_is_re_sent(self) -> None:
        assert idempotency_key(self._asset(), PNG) != idempotency_key(self._asset(), PNG + b"!")

    def test_different_assets_never_collide(self) -> None:
        other = select_uploadable_asset(
            {"id": 101, "assets": [{"id": "a1", "url": "https://x/a1.jpg"}]}
        )
        assert other is not None
        assert idempotency_key(self._asset(), PNG) != idempotency_key(other, PNG)

    def test_key_is_independent_of_the_clock(self) -> None:
        # Nothing time-varying may enter it: a run a year later must recompute
        # the identical value, or the ledger never matches and every image is
        # re-uploaded on every run forever.
        with time_machine.travel("2026-09-14T12:00:00Z"):
            today = idempotency_key(self._asset(), PNG)
        with time_machine.travel("2027-04-01T03:17:00Z"):
            next_year = idempotency_key(self._asset(), PNG)
        assert today == next_year

    def test_the_source_url_is_part_of_the_key(self) -> None:
        # Same asset id, same bytes, moved CDN: TrueQuote stores at a path
        # derived from the source url, so this is a different stored object.
        moved = select_uploadable_asset(
            {"id": 100, "assets": [{"id": "a1", "url": "https://y/a1.jpg"}]}
        )
        assert moved is not None
        assert idempotency_key(self._asset(), PNG) != idempotency_key(moved, PNG)


# ---------------------------------------------------------------------------
# DIMENSIONS AND DENSITY — the measurement the byte floor cannot make.
#
# Measured on 2026-09-16 against ServiceTitan's web-app image proxy: a request
# for a missing asset with `?size=1200&default=Default%2F1.png` answers 200
# `image/webp`, 2,798 bytes — and the bytes are a COMPLETELY BLANK WHITE
# 1200x1200 image. It clears `MIN_PLAUSIBLE_IMAGE_BYTES` (1024) with room to
# spare, and because the placeholder's size scales with `size=`, no fixed byte
# threshold can be both above every placeholder and below every photograph.
#
# Nothing here REJECTS anything. It exists to measure, because no real
# pricebook asset from any live tenant has ever been sized.
# ---------------------------------------------------------------------------


def _png(width: int, height: int, body: bytes = b"") -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + body
    )


def _webp_vp8x(width: int, height: int, body: bytes = b"") -> bytes:
    return (
        b"RIFF"
        + b"\x00\x00\x00\x00"
        + b"WEBP"
        + b"VP8X"
        + (10).to_bytes(4, "little")
        + b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
        + body
    )


def _jpeg(width: int, height: int, body: bytes = b"") -> bytes:
    sof = (
        b"\xff\xc0"
        + (11).to_bytes(2, "big")
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x00\x00\x00"
    )
    return b"\xff\xd8\xff" + b"\xe0" + (4).to_bytes(2, "big") + b"JF" + sof + body


class TestDimensionsComeFromTheHeaderAlone:
    """No decode, no image library, no pixel touched — just the file header."""

    def test_png(self) -> None:
        assert image_dimensions(_png(1200, 800)) == (1200, 800)

    def test_webp_vp8x(self) -> None:
        assert image_dimensions(_webp_vp8x(1200, 1200)) == (1200, 1200)

    def test_jpeg(self) -> None:
        assert image_dimensions(_jpeg(640, 480)) == (640, 480)

    def test_an_unreadable_header_answers_none_rather_than_a_wrong_number(self) -> None:
        """The harmless direction: no dimensions means no density is reported,
        never a density computed from a guess."""
        assert image_dimensions(b"") is None
        assert image_dimensions(b"\x89PNG\r\n\x1a\n" + b"short") is None
        assert image_dimensions(b"GIF89a" + b"\x00" * 40) is None
        assert image_dimensions(b"RIFF" + b"\x00" * 40) is None

    def test_a_hostile_jpeg_cannot_make_it_scan_forever(self) -> None:
        """A malformed segment chain costs a bounded walk, not 8 MiB."""
        assert image_dimensions(b"\xff\xd8\xff" + b"\xe0\x00\x04ab" * 500) is None


class TestDensityIsWhatDistinguishesABlank:
    def test_the_measured_blank_placeholder_is_recognised(self) -> None:
        """2,798 bytes over 1200x1200 = 0.0019 bytes/pixel. The real one."""
        blank = _webp_vp8x(1200, 1200, body=b"\x00" * 2768)
        assert len(blank) == 2798
        assert looks_blank(blank) is True
        assert "SUSPECTED-BLANK" in describe_image_size(blank)
        assert "1200x1200" in describe_image_size(blank)

    def test_the_byte_floor_does_not_catch_it_which_is_the_whole_point(self) -> None:
        blank = _webp_vp8x(1200, 1200, body=b"\x00" * 2768)
        assert len(blank) > MIN_PLAUSIBLE_IMAGE_BYTES
        assert is_placeholder_image(blank) is False, (
            "the fixed byte floor passes a 2798-byte blank; density is why this exists"
        )

    def test_a_real_photograph_is_orders_of_magnitude_denser(self) -> None:
        """A 640x480 JPEG at even low quality carries tens of KB."""
        photo = _jpeg(640, 480, body=b"\x7f" * 40_000)
        assert looks_blank(photo) is False
        assert "SUSPECTED-BLANK" not in describe_image_size(photo)

    def test_a_small_thumbnail_is_not_mistaken_for_a_blank(self) -> None:
        """64x64 is 4,096 pixels; 3 KB over them is 0.75 bytes/pixel."""
        thumb = _png(64, 64, body=b"\x11" * 3000)
        assert looks_blank(thumb) is False

    def test_unknown_dimensions_are_never_called_blank(self) -> None:
        """No evidence is not evidence. The payload is uploaded either way, but
        a count that guessed would be worse than no count."""
        assert looks_blank(b"GIF89a" + b"\x00" * 5000) is False
        assert describe_image_size(b"GIF89a") == "bytes=6 dimensions=unknown density=unknown"

    def test_nothing_here_changes_what_is_accepted(self) -> None:
        """Deliberately measurement-only: no real pricebook asset has ever been
        sized, so there is no evidence from which to set a rejection rule, and a
        rule guessed today would silently drop real images."""
        blank = _webp_vp8x(1200, 1200, body=b"\x00" * 2768)
        assert sniff_content_type(blank) == "image/webp"
        assert is_placeholder_image(blank) is False
