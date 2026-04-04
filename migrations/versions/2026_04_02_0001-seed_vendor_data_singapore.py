"""Seed vendor data for Singapore

Revision ID: 2026_04_02_0001
Revises: 2026_04_01_0001
Create Date: 2026-04-02
"""
import os
import csv
import re
import uuid
import hashlib
from datetime import datetime
from alembic import op
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision = "2026_04_02_0001"
down_revision = "2026_04_02_0000"
branch_labels = None
depends_on = None

# ── Helpers ───────────────────────────────────────────────────────────────────

def generate_slug(name: str, uen: str | None = None) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    if uen:
        uen_suffix = re.sub(r"[^a-z0-9]", "", uen.lower())[:12]
        return f"{base}-{uen_suffix}"
    return base

def make_claim_token(company_name: str, domain: str | None, line_no: int) -> str:
    key = f"{company_name}:{domain or line_no}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return f"seed-{digest}"

# ── Migration ───────────────────────────────────────────────────────────────

def upgrade() -> None:
    # Use a session for data operations
    bind = op.get_bind()
    session = Session(bind=bind)

    csv_path = os.path.join(os.path.dirname(__file__), "../../data/vendor_seed_sg_1775105894.csv")
    
    # Check if file exists (might not during build but should during run)
    # Actually, in Docker it will be at /app/data/vendor_seed_sg_1775105894.csv
    # and migrations folder is at /app/migrations/versions/
    # so we can use a path relative to the file.
    
    if not os.path.exists(csv_path):
        print(f"[seed_data] WARNING: CSV not found at {csv_path}. Skipping data seed.")
        return

    print(f"[seed_data] Importing from {csv_path}...")

    try:
        with open(csv_path, mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            
            inserted = 0
            updated = 0
            skipped = 0
            
            for line_no, row in enumerate(reader, 1):
                company_name = row.get("company_name", "").strip()
                uen = row.get("uen", "").strip() or None
                industry = row.get("industry", "").strip() or None
                country = row.get("country", "Singapore").strip()
                city = row.get("city", "Singapore").strip()
                website = row.get("website", "").strip() or None
                domain = website # Simple mapping for now
                short_desc = row.get("short_description", "").strip() or None
                source_val = (row.get("source", "") or "csv").lower().strip()

                if not company_name:
                    skipped += 1
                    continue

                if uen:
                    # Upsert MarketplaceVendor
                    existing = bind.execute(
                        sa.text("SELECT id FROM marketplace_vendors WHERE uen = :uen"),
                        {"uen": uen}
                    ).fetchone()

                    if existing:
                        bind.execute(
                            sa.text("""
                                UPDATE marketplace_vendors 
                                SET industry = :industry, website = :website, domain = :domain, source = :source, updated_at = :now
                                WHERE id = :id
                            """),
                            {
                                "industry": industry,
                                "website": website,
                                "domain": domain,
                                "source": source_val,
                                "now": datetime.utcnow(),
                                "id": existing.id
                            }
                        )
                        updated += 1
                    else:
                        slug = generate_slug(company_name, uen)
                        bind.execute(
                            sa.text("""
                                INSERT INTO marketplace_vendors (
                                    id, company_name, slug, domain, website, uen, industry, country, city, short_description, scan_status, source, created_at, updated_at
                                ) VALUES (
                                    :id, :name, :slug, :domain, :website, :uen, :industry, :country, :city, :desc, 'NONE', :source, :now, :now
                                )
                            """),
                            {
                                "id": str(uuid.uuid4()),
                                "name": company_name,
                                "slug": slug,
                                "domain": domain,
                                "website": website,
                                "uen": uen,
                                "industry": industry,
                                "country": country,
                                "city": city,
                                "desc": short_desc,
                                "source": source_val,
                                "now": datetime.utcnow()
                            }
                        )
                        inserted += 1
                else:
                    # Upsert DiscoveredVendor
                    existing = bind.execute(
                        sa.text("SELECT id FROM discovered_vendors WHERE company_name = :name"),
                        {"name": company_name}
                    ).fetchone()

                    if existing:
                        bind.execute(
                            sa.text("""
                                UPDATE discovered_vendors 
                                SET industry = :industry, domain = :domain, source = :source, updated_at = :now
                                WHERE id = :id
                            """),
                            {
                                "industry": industry,
                                "domain": domain,
                                "source": source_val,
                                "now": datetime.utcnow(),
                                "id": existing.id
                            }
                        )
                        updated += 1
                    else:
                        bind.execute( sa.text("""
                                INSERT INTO discovered_vendors (
                                    id, company_name, domain, industry, country, city, source, created_at, updated_at
                                ) VALUES (
                                    :id, :name, :domain, :industry, :country, :city, :source, :now, :now
                                )
                            """),
                            {
                                "id": str(uuid.uuid4()),
                                "name": company_name,
                                "domain": domain,
                                "industry": industry,
                                "country": country,
                                "city": city,
                                "source": source_val,
                                "now": datetime.utcnow()
                            }
                        )
                        inserted += 1

                if (inserted + updated) % 100 == 0:
                    session.flush()

        session.commit()
        print(f"[seed_data] Done: {inserted} inserted, {updated} updated, {skipped} skipped.")

    except Exception as e:
        session.rollback()
        print(f"[seed_data] ERROR during migration: {e}")
        raise e
    finally:
        session.close()


def downgrade() -> None:
    # No automatic downgrade for data seed, as it might delete data that was updated/already there.
    pass
