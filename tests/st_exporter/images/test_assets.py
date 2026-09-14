"""Tests for the pure asset rules — selection, sniffing, idempotency key.

Every expectation here is a mirror of TrueQuote's receiving side; where one of
these fails, the wire format has drifted, not the taste.
"""

from __future__ import annotations

from st_exporter.images.assets import (
    idempotency_key,
    is_displayable,
    is_storable,
    is_storage_path,
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
        # Nothing time-varying may enter it: the whole point is that a later run
        # recomputes the identical value.
        assert idempotency_key(self._asset(), PNG) == (
            idempotency_key(
                select_uploadable_asset(
                    {"id": 100, "assets": [{"id": "a1", "url": "https://x/a1.jpg"}]}
                ),
                PNG,
            )  # type: ignore[arg-type]
        )
