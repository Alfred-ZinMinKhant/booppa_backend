"""Magic-byte validation for screenshot provider responses.

Regression: previously, providers that returned an HTML error/marketing page
when they couldn't reach the target URL had their bytes silently base64-
encoded and stored as `site_screenshot`. The report viewer would then render
the HTML inside an <img> slot, producing the confusing "unstyled Booppa
marketing page in the screenshot box" bug seen on 2026-06-04.
"""
from app.services.screenshot_service import looks_like_image as _looks_like_image


PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP_HEADER = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8
GIF89_HEADER = b"GIF89a" + b"\x00" * 16
GIF87_HEADER = b"GIF87a" + b"\x00" * 16


class TestRealImages:
    def test_png_accepted(self):
        assert _looks_like_image(PNG_HEADER) is True

    def test_jpeg_accepted(self):
        assert _looks_like_image(JPEG_HEADER) is True

    def test_webp_accepted(self):
        assert _looks_like_image(WEBP_HEADER) is True

    def test_gif89_accepted(self):
        assert _looks_like_image(GIF89_HEADER) is True

    def test_gif87_accepted(self):
        assert _looks_like_image(GIF87_HEADER) is True


class TestHtmlMasquerade:
    """The original bug class — HTML being treated as a screenshot."""

    def test_doctype_rejected(self):
        body = b"<!DOCTYPE html><html><head><title>Booppa</title>" + b"x" * 1000
        assert _looks_like_image(body) is False

    def test_lowercase_html_rejected(self):
        body = b"<html><body>Get a verified compliance report</body></html>" + b"x" * 1000
        assert _looks_like_image(body) is False

    def test_json_error_rejected(self):
        body = b'{"error": "could not capture", "status": 500}' + b"x" * 200
        assert _looks_like_image(body) is False

    def test_xml_rejected(self):
        body = b'<?xml version="1.0"?><error>nope</error>' + b"x" * 200
        assert _looks_like_image(body) is False


class TestEdgeCases:
    def test_empty_rejected(self):
        assert _looks_like_image(b"") is False

    def test_too_short_rejected(self):
        # Need at least 12 bytes to inspect the WEBP RIFF/WEBP slice
        assert _looks_like_image(b"\x89PNG") is False

    def test_none_safe(self):
        assert _looks_like_image(None) is False  # type: ignore[arg-type]

    def test_random_bytes_rejected(self):
        assert _looks_like_image(b"\x00" * 32) is False
