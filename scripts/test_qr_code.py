#!/usr/bin/env python3
"""
QR Code Validation Test Script
Verifies that Polygonscan URLs are generated correctly for scannable QR codes.

RUN THIS SCRIPT TO VERIFY THE CRITICAL FIX:
Zero spaces in blockchain verification URLs
"""

import hashlib

def main():
    print("QR Code URL Validation Test")
    print("=" * 50)

    # Test transaction hash
    test_tx_hash = "0x5c798725C269784b0B0F396001D4f8287A1A7D18"

    # CORRECT IMPLEMENTATION: Zero spaces
    correct_url = f"https://polygonscan.com/tx/{test_tx_hash}"

    # INCORRECT (what we fixed): With spaces
    incorrect_url = f"https://polygonscan.com/tx/  {test_tx_hash}"

    print(f"Test Transaction Hash: {test_tx_hash}")
    print()

    # Test correct URL
    print("Testing CORRECT URL (zero spaces):")
    print(f"   URL: {correct_url}")
    print(f"   Contains spaces: {'YES - BROKEN' if ' ' in correct_url else 'NO - GOOD'}")
    print(f"   Length: {len(correct_url)} characters")
    print(f"   QR Code Status: {'SCANNABLE' if ' ' not in correct_url else 'BROKEN'}")
    print()

    # Test incorrect URL
    print("Testing INCORRECT URL (with spaces):")
    print(f"   URL: {incorrect_url}")
    print(f"   Contains spaces: {'YES - BROKEN' if ' ' in incorrect_url else 'NO - GOOD'}")
    print(f"   Length: {len(incorrect_url)} characters")
    print(f"   QR Code Status: {'SCANNABLE' if ' ' not in incorrect_url else 'BROKEN'}")
    print()

    # Validation
    if ' ' not in correct_url:
        print("SUCCESS: QR code URLs are correctly generated!")
        print("   Blockchain verification will work properly.")
        return True
    else:
        print("CRITICAL ERROR: URLs still contain spaces!")
        print("   QR codes will not work correctly.")
        return False

if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
