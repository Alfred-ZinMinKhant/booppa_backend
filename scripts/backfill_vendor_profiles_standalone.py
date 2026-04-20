"""
backfill_vendor_profiles_standalone.py
=======================================
For every existing VENDOR user who has no claimed MarketplaceVendor entry,
either:
  1. Claim an existing unclaimed marketplace_vendors row (matched by UEN, then
     company name), OR
  2. Create a new marketplace_vendors row linked to that user.

Standalone — zero app imports. Runs directly with psycopg2.

Usage:
    DATABASE_URL="postgresql://user:pass@host:5432/db" python3 scripts/backfill_vendor_profiles_standalone.py
    DATABASE_URL="postgresql://user:pass@host:5432/db" python3 scripts/backfill_vendor_profiles_standalone.py --dry-run
"""

import os
import re
import sys
import uuid
import argparse
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras


# ── slug helper ───────────────────────────────────────────────────────────────

def generate_slug(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'[\s-]+', '-', slug)
    slug = slug.strip('-')
    return slug[:200]


def unique_slug(cur, base: str) -> str:
    slug = base
    counter = 1
    while True:
        cur.execute("SELECT 1 FROM marketplace_vendors WHERE slug = %s", (slug,))
        if not cur.fetchone():
            return slug
        slug = f"{base}-{counter}"
        counter += 1


# ── main ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False):
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL environment variable not set", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    now = datetime.now(timezone.utc)

    # 1. Find all VENDOR users who don't yet have a claimed marketplace entry
    cur.execute("""
        SELECT u.id, u.email, u.company, u.uen, u.industry
        FROM users u
        WHERE u.role = 'VENDOR'
          AND u.company IS NOT NULL
          AND u.company != ''
          AND NOT EXISTS (
              SELECT 1 FROM marketplace_vendors mv
              WHERE mv.claimed_by_user_id = u.id
          )
        ORDER BY u.created_at
    """)
    users = cur.fetchall()

    print(f"[backfill] Found {len(users)} VENDOR users without a marketplace profile")

    claimed = 0
    created = 0
    skipped = 0

    for user in users:
        uid    = user['id']
        email  = user['email']
        company = user['company']
        uen    = user['uen']
        industry = user['industry']

        mv_id = None

        # --- Try to match an existing unclaimed entry ---

        # 1a. By UEN
        if uen:
            cur.execute("""
                SELECT id FROM marketplace_vendors
                WHERE uen = %s AND claimed_by_user_id IS NULL
                LIMIT 1
            """, (uen,))
            row = cur.fetchone()
            if row:
                mv_id = row['id']

        # 1b. By exact company name (case-insensitive)
        if mv_id is None:
            cur.execute("""
                SELECT id FROM marketplace_vendors
                WHERE lower(company_name) = lower(%s) AND claimed_by_user_id IS NULL
                LIMIT 1
            """, (company,))
            row = cur.fetchone()
            if row:
                mv_id = row['id']

        if mv_id:
            # Claim the existing entry
            print(f"  [claim]  user={email}  mv_id={mv_id}")
            if not dry_run:
                cur.execute("""
                    UPDATE marketplace_vendors
                    SET claimed_by_user_id = %s,
                        claimed_at = %s,
                        industry = COALESCE(NULLIF(industry, ''), %s)
                    WHERE id = %s
                """, (uid, now, industry, mv_id))
            claimed += 1
        else:
            # Create a fresh entry
            slug = unique_slug(cur, generate_slug(company))
            new_id = str(uuid.uuid4())
            print(f"  [create] user={email}  slug={slug}")
            if not dry_run:
                cur.execute("""
                    INSERT INTO marketplace_vendors
                        (id, company_name, slug, uen, industry, country, source,
                         scan_status, claimed_by_user_id, claimed_at, created_at, updated_at)
                    VALUES
                        (%s, %s, %s, %s, %s, 'Singapore', 'manual',
                         'NONE', %s, %s, %s, %s)
                """, (new_id, company, slug, uen or None, industry or None,
                      uid, now, now, now))
            created += 1

    if not dry_run:
        conn.commit()
        print(f"\n[backfill] Done — claimed={claimed}  created={created}  skipped={skipped}")
    else:
        conn.rollback()
        print(f"\n[backfill] DRY RUN — would claim={claimed}  create={created}  skip={skipped}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill marketplace_vendors for existing vendor users")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
