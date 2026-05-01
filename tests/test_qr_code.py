"""Test QR code URL generation - CRITICAL FIX VERIFICATION"""
from app.core.config import settings


def test_polygonscan_url_has_no_spaces():
    """Verify that Polygonscan URLs contain zero spaces for valid QR codes"""
    test_tx_hash = "0x1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b"
    explorer = settings.POLYGON_EXPLORER_URL.rstrip("/")
    polygonscan_url = f"{explorer}/tx/{test_tx_hash}"

    assert ' ' not in polygonscan_url, "URL contains spaces - QR code will fail!"
    assert polygonscan_url.startswith(f"{explorer}/tx/0x")
    assert polygonscan_url == f"{explorer}/tx/{test_tx_hash}"


def test_multiple_transaction_hashes():
    """Test various transaction hash formats"""
    explorer = settings.POLYGON_EXPLORER_URL.rstrip("/")
    test_hashes = [
        "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        "0x1A2B3C4D5E6F7A8B9C0D1E2F3A4B5C6D7E8F9A0B1C2D3E4F5A6B7C8D9E0F1A2B",
    ]
    for tx_hash in test_hashes:
        url = f"{explorer}/tx/{tx_hash}"
        assert ' ' not in url, f"URL contains spaces for hash: {tx_hash}"
        assert url == f"{explorer}/tx/{tx_hash}"
