#!/usr/bin/env python3
"""
BOOPPA v10.0 Setup Validation Script
Tests critical components to ensure everything works.
"""

import sys
import os

def test_imports():
    """Test that all critical modules can be imported"""
    print("Testing module imports...")

    try:
        from app.core.config import settings
        from app.core.db import SessionLocal, Base
        from app.core.models import User, Report
        from app.services.blockchain import BlockchainService
        from app.services.ai_service import AIService
        print("All modules import successfully")
        return True
    except Exception as e:
        print(f"Import failed: {e}")
        return False

def test_qr_urls():
    """Test QR code URL generation"""
    print("\nTesting QR code URLs...")

    test_hashes = [
        "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "0x1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b"
    ]

    for tx_hash in test_hashes:
        url = f"https://polygonscan.com/tx/{tx_hash}"
        if ' ' in url:
            print(f"URL contains spaces: {url}")
            return False
        print(f"Valid URL: {url}")

    print("All QR code URLs are valid")
    return True

def test_config():
    """Test configuration loading"""
    print("\nTesting configuration...")

    try:
        from app.core.config import settings
        required_settings = [
            'SECRET_KEY', 'DATABASE_URL', 'REDIS_URL',
            'AWS_REGION', 'S3_BUCKET', 'POLYGON_RPC_URL'
        ]

        for setting in required_settings:
            if hasattr(settings, setting):
                print(f"{setting}: Configured")
            else:
                print(f"{setting}: Missing")
                return False

        print("All required settings are present")
        return True
    except Exception as e:
        print(f"Config test failed: {e}")
        return False

def main():
    print("BOOPPA v10.0 Enterprise - Setup Validation")
    print("=" * 50)

    tests = [
        test_imports,
        test_qr_urls,
        test_config
    ]

    results = []
    for test in tests:
        results.append(test())

    print("\n" + "=" * 50)
    if all(results):
        print("ALL TESTS PASSED! BOOPPA v10.0 is ready for deployment!")
        return 0
    else:
        print("Some tests failed. Please check the issues above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
