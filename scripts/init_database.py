#!/usr/bin/env python3
"""
Initialize database tables (development only)
In production, use Alembic migrations.
"""
import sys
import os

# Add app directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.db import create_tables, engine
from app.core.config import settings

def main():
    print("Initializing database tables...")
    try:
        create_tables()
        print("Database tables created successfully")
    except Exception as e:
        print(f"Error creating tables: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
