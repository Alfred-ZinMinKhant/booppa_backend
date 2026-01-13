#!/usr/bin/env python3
"""
Generate a secure secret key for JWT tokens
"""
import secrets

def generate_secret_key():
    """Generate a cryptographically secure secret key"""
    return secrets.token_urlsafe(32)

if __name__ == "__main__":
    key = generate_secret_key()
    print(f"Generated SECRET_KEY: {key}")
    print("\nAdd this to your .env file:")
    print(f"SECRET_KEY={key}")
