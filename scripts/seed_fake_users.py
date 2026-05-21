#!/usr/bin/env python3
"""
scripts/seed_fake_users.py — Seed verified users and fake vendors
===================================================================
Generates around 500 verified users (claimed vendors) and 200-300 unclaimed fake vendors.
Fully populates related tables: users, marketplace_vendors, verify_records,
vendor_scores, vendor_status_snapshots, vendor_sectors, and discovered_vendors.

Usage:
    python scripts/seed_fake_users.py          # default seeding
    python scripts/seed_fake_users.py --clean  # clean up previous mock seed before seeding
    python scripts/seed_fake_users.py --dry-run # count only, no database writes
"""

import argparse
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to path so we can import app modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal
from app.core.models import User
from app.core.models_v10 import MarketplaceVendor, DiscoveredVendor
from app.core.models_v6 import VerifyRecord, LifecycleStatus, VerificationLevel, VendorScore, VendorSector
from app.core.models_v8 import VendorStatusSnapshot
from app.core.auth import get_password_hash


# ── Data Lists for Generation ───────────────────────────────────────────────

FIRST_NAMES = [
    "Aiden", "Olivia", "Ethan", "Sophia", "Liam", "Ava", "Noah", "Isabella", 
    "Jackson", "Mia", "Lucas", "Charlotte", "Oliver", "Amelia", "Elijah", "Harper", 
    "Benjamin", "Evelyn", "Leo", "Emily", "Muhammad", "Wei", "Mei", "Chen", 
    "Arjun", "Priya", "Sanjay", "Deepika", "Zhi", "Ying", "Ryan", "Sarah", 
    "Alex", "Jessica", "David", "Michelle", "Tan", "Lim", "Goh", "Lee"
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", 
    "Rodriguez", "Martinez", "Tan", "Lim", "Lee", "Ong", "Goh", "Chan", 
    "Wong", "Teo", "Koh", "Kumar", "Patel", "Sharma", "Singh", "Rao", "Nair"
]

COMPANY_PREFIXES = [
    "Apex", "Vertex", "BlueOcean", "Cyber", "Novus", "Quantum", "Nexus", "Elevate", 
    "Integra", "Aegis", "Vanguard", "Sentinel", "Synergy", "Invenio", "Zephyr", 
    "Krypton", "Astra", "Fortis", "Zenith", "Meridian", "Helix", "Sovereign", 
    "Beacon", "Vector", "Titan", "Polaris", "Summit", "Prism", "Echo", "Atlas"
]

COMPANY_NOUNS = [
    "Tech", "Solutions", "Systems", "Security", "Consulting", "Digital", "Software", 
    "Logistics", "Networks", "Analytics", "Intelligence", "Dynamics", "Labs", 
    "Technologies", "Media", "Ventures", "Cybernetics", "Cloud", "Automation", "Informatics"
]

COMPANY_SUFFIXES = [
    "Pte Ltd", "Ltd", "Singapore", "APAC", "Group", "International"
]

INDUSTRIES = [
    "Cybersecurity", "Cloud Computing", "Software Development", "Managed IT Services", 
    "Digital Marketing", "AI & Analytics", "HR Tech", "Fintech", "Logistics", "Edtech"
]


# ── Helper Functions ─────────────────────────────────────────────────────────

def generate_slug(name: str) -> str:
    """Generate URL-safe slug from company name."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "-", slug)
    slug = slug.strip("-")
    return slug[:200]


def generate_uen() -> str:
    """Generate a realistic Singapore Unique Entity Number (UEN)."""
    # E.g., 202412345A or T12LL1234A
    year = random.randint(1995, 2026)
    digits = "".join(str(random.randint(0, 9)) for _ in range(5))
    letter = random.choice("ABCDEFGHJKLMNOPRSTUWY")
    return f"{year}{digits}{letter}"


def clean_existing_mock_data(db) -> None:
    """Remove previously seeded mock data to maintain idempotency."""
    print("[seed_fake_users] Cleaning up existing mock data...")
    
    # 1. Delete matching users (email ending with @booppa-mock.sg)
    # SQLAlchemy will cascade delete verify_records, vendor_scores,
    # vendor_status_snapshots, and vendor_sectors if cascading is configured.
    # To be safe and thorough, we explicitly delete them or delete users.
    mock_users = db.query(User).filter(User.email.like("%@booppa-mock.sg")).all()
    mock_user_ids = [u.id for u in mock_users]
    
    if mock_user_ids:
        print(f"  Found {len(mock_user_ids)} mock users. Purging child records...")
        db.query(VendorSector).filter(VendorSector.vendor_id.in_(mock_user_ids)).delete(synchronize_session=False)
        db.query(VendorStatusSnapshot).filter(VendorStatusSnapshot.vendor_id.in_(mock_user_ids)).delete(synchronize_session=False)
        db.query(VendorScore).filter(VendorScore.vendor_id.in_(mock_user_ids)).delete(synchronize_session=False)
        db.query(VerifyRecord).filter(VerifyRecord.vendor_id.in_(mock_user_ids)).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(mock_user_ids)).delete(synchronize_session=False)
    
    # 2. Delete MarketplaceVendor and DiscoveredVendor mock data
    mv_deleted = db.query(MarketplaceVendor).filter(MarketplaceVendor.source == "mock_seed").delete(synchronize_session=False)
    dv_deleted = db.query(DiscoveredVendor).filter(DiscoveredVendor.source == "mock_seed").delete(synchronize_session=False)
    
    db.commit()
    print(f"  Purge complete. Deleted: {len(mock_user_ids)} users, {mv_deleted} marketplace vendors, {dv_deleted} discovered vendors.")


def run(clean: bool, dry_run: bool) -> None:
    db = SessionLocal()
    
    if clean and not dry_run:
        clean_existing_mock_data(db)
        
    # Target random counts
    target_verified = random.randint(485, 515)
    target_unclaimed = random.randint(200, 300)
    
    print(f"[seed_fake_users] Targets:")
    print(f"  Verified Users (Claimed Vendors) : {target_verified}")
    print(f"  Unclaimed/Fake Vendors           : {target_unclaimed}")
    
    if dry_run:
        print("[seed_fake_users] DRY RUN — No database operations will be written.")
        db.close()
        return

    # Pre-calculate password hash for "Password123!" to keep execution time under 10 seconds
    print("[seed_fake_users] Pre-calculating password hash...")
    hashed_pw = get_password_hash("Password123!")
    
    print("[seed_fake_users] Seeding verified users and claimed vendor profiles...")
    
    used_company_names = set()
    used_uens = set()
    
    for i in range(1, target_verified + 1):
        # Generate unique company details
        while True:
            prefix = random.choice(COMPANY_PREFIXES)
            noun = random.choice(COMPANY_NOUNS)
            suffix = random.choice(COMPANY_SUFFIXES)
            company_name = f"{prefix} {noun} {suffix}"
            if company_name not in used_company_names:
                used_company_names.add(company_name)
                break
                
        while True:
            uen = generate_uen()
            if uen not in used_uens:
                used_uens.add(uen)
                break
        
        industry = random.choice(INDUSTRIES)
        domain = generate_slug(f"{prefix} {noun}") + random.choice([".sg", ".com", ".net", ".co"])
        website = f"https://www.{domain}"
        
        # User details
        full_name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
        email_prefix = full_name.lower().replace(" ", ".")
        email = f"{email_prefix}@{domain}"
        # We also keep a secondary mock-identification address for cleanup purposes
        login_email = f"user{i}@{domain}" # simple clean pattern
        # Actually let's just make the login email end with @booppa-mock.sg for clean query deletes!
        cleanup_email = f"vendor_{i}_{email_prefix}@booppa-mock.sg"
        
        # Verify timestamp
        verified_days_ago = random.randint(5, 120)
        verified_at = datetime.now(timezone.utc) - timedelta(days=verified_days_ago)
        
        # Create User
        user = User(
            email=cleanup_email,
            hashed_password=hashed_pw,
            full_name=full_name,
            role="VENDOR",
            company=company_name,
            uen=uen,
            website=website,
            industry=industry,
            plan=random.choice(["free", "pro", "enterprise"]),
            verified_at=verified_at,
            company_description=f"A leading {industry.lower()} services provider in Singapore, focusing on delivering enterprise value and digital transformation.",
            created_at=verified_at - timedelta(days=2),
        )
        db.add(user)
        db.flush() # retrieve user.id
        
        # Create MarketplaceVendor linked to User
        vendor = MarketplaceVendor(
            company_name=company_name,
            slug=generate_slug(company_name),
            domain=domain,
            website=website,
            uen=uen,
            industry=industry,
            country="Singapore",
            city="Singapore",
            short_description=user.company_description,
            claimed_by_user_id=user.id,
            claimed_at=verified_at,
            source="mock_seed",
            scan_status="COMPLETE",
            contact_email=email,
            created_at=user.created_at,
        )
        db.add(vendor)
        
        # Create VerifyRecord
        compliance_score = random.randint(80, 98)
        visibility_score = random.randint(70, 95)
        
        vr = VerifyRecord(
            vendor_id=user.id,
            company_name=company_name,
            compliance_score=compliance_score,
            visibility_score=visibility_score,
            verification_level=random.choice([VerificationLevel.STANDARD, VerificationLevel.PREMIUM]),
            lifecycle_status=LifecycleStatus.ACTIVE,
            created_at=verified_at,
        )
        db.add(vr)
        
        # Create VendorScore
        engagement_score = random.randint(60, 95)
        recency_score = random.randint(70, 95)
        procurement_interest = random.randint(50, 95)
        total_score = (compliance_score + visibility_score + engagement_score + recency_score + procurement_interest) // 5
        
        vs = VendorScore(
            vendor_id=user.id,
            compliance_score=compliance_score,
            visibility_score=visibility_score,
            engagement_score=engagement_score,
            recency_score=recency_score,
            procurement_interest_score=procurement_interest,
            total_score=total_score,
            created_at=verified_at,
        )
        db.add(vs)
        
        # Create VendorStatusSnapshot
        notarization_depth = random.randint(1, 4)
        evidence_count = random.randint(2, 6)
        confidence_score = random.uniform(70.0, 96.0)
        risk_adjusted_pct = random.uniform(60.0, 98.0)
        
        vss = VendorStatusSnapshot(
            vendor_id=user.id,
            verification_depth=random.choice(["STANDARD", "DEEP", "CERTIFIED"]),
            monitoring_activity="ACTIVE",
            risk_signal="CLEAN",
            procurement_readiness="READY",
            risk_adjusted_pct=risk_adjusted_pct,
            dual_silent_mode="ELEVATED_VERIFIED",
            notarization_depth=notarization_depth,
            evidence_count=evidence_count,
            confidence_score=confidence_score,
            computed_at=verified_at,
            created_at=verified_at,
        )
        db.add(vss)
        
        # Create VendorSector
        vsector = VendorSector(
            vendor_id=user.id,
            sector=industry,
        )
        db.add(vsector)
        
        if i % 100 == 0:
            print(f"  Seeded {i} verified users...")
            
    db.commit()
    print(f"  Successfully seeded {target_verified} verified vendor profiles!")

    # ── Seed Unclaimed Fake Vendors ─────────────────────────────────────────
    print("[seed_fake_users] Seeding unclaimed fake vendors...")
    
    for i in range(1, target_unclaimed + 1):
        while True:
            prefix = random.choice(COMPANY_PREFIXES)
            noun = random.choice(COMPANY_NOUNS)
            suffix = random.choice(COMPANY_SUFFIXES)
            company_name = f"Unclaimed {prefix} {noun} {suffix}"
            if company_name not in used_company_names:
                used_company_names.add(company_name)
                break
                
        while True:
            uen = generate_uen()
            if uen not in used_uens:
                used_uens.add(uen)
                break
                
        industry = random.choice(INDUSTRIES)
        domain = generate_slug(f"{prefix} {noun}") + random.choice([".sg", ".com", ".net", ".co"])
        website = f"https://www.{domain}"
        
        # Unclaimed MarketplaceVendor
        vendor = MarketplaceVendor(
            company_name=company_name,
            slug=generate_slug(company_name),
            domain=domain,
            website=website,
            uen=uen,
            industry=industry,
            country="Singapore",
            city="Singapore",
            short_description=f"Specialized {industry.lower()} services offering tailored solutions to optimize enterprise agility.",
            claimed_by_user_id=None,
            claimed_at=None,
            source="mock_seed",
            scan_status="COMPLETE",
            contact_email=f"info@{domain}",
        )
        db.add(vendor)
        
        # Unclaimed DiscoveredVendor
        dv = DiscoveredVendor(
            company_name=company_name,
            uen=uen,
            domain=domain,
            industry=industry,
            country="Singapore",
            city="Singapore",
            website=website,
            source="mock_seed",
        )
        db.add(dv)
        
        if i % 100 == 0:
            print(f"  Seeded {i} unclaimed vendors...")
            
    db.commit()
    db.close()
    
    print(f"[seed_fake_users] Seeding completed successfully!")
    print(f"  Total Verified Users: {target_verified}")
    print(f"  Total Unclaimed Vendors: {target_unclaimed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Seed verified users and fake vendors for Booppa"
    )
    parser.add_argument("--clean", action="store_true",
                        help="Remove previous mock seeded data before starting")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview target counts without database writes")
    args = parser.parse_args()

    run(args.clean, args.dry_run)
