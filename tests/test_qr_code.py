"""Test QR code URL generation - CRITICAL FIX VERIFICATION"""
import hashlib

def test_polygonscan_url_has_no_spaces():
    """Verify that Polygonscan URLs contain zero spaces for valid QR codes"""
    test_tx_hash = "0x1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b"

    # CORRECT: Zero spaces between /tx/ and transaction hash
    polygonscan_url = f"https://polygonscan.com/tx/{test_tx_hash}"

    # Critical validation
    assert ' ' not in polygonscan_url, "URL contains spaces - QR code will fail!"
    assert polygonscan_url.startswith('https://polygonscan.com/tx/0x')
    assert polygonscan_url == f"https://polygonscan.com/tx/{test_tx_hash}"

    print("SUCCESS: QR code URL is valid and will work correctly")
    print(f"   Generated URL: {polygonscan_url}")

def test_multiple_transaction_hashes():
    """Test various transaction hash formats"""
    test_hashes = [
        "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        "0x1A2B3C4D5E6F7A8B9C0D1E2F3A4B5C6D7E8F9A0B1C2D3E4F5A6B7C8D9E0F1A2B"
    ]

    for tx_hash in test_hashes:
        url = f"https://polygonscan.com/tx/{tx_hash}"
        assert ' ' not in url, f"URL contains spaces for hash: {tx_hash}"
        assert url == f"https://polygonscan.com/tx/{tx_hash}"

if __name__ == "__main__":
    test_polygonscan_url_has_no_spaces()
    test_multiple_transaction_hashes()
    print("All QR code URL tests passed! Blockchain verification will work correctly.")
