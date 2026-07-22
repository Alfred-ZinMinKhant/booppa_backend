import os
import uuid
import datetime
from app.core.db import SessionLocal
from app.core.models import User, Organisation, TrmControl, TrmEvidence, MAS_TRM_DOMAINS
from app.workers.tasks import run_suite_trm_baseline_for_user

def main():
    db = SessionLocal()
    try:
        # 1. Setup user zinminkhant.alfred@gmail.com
        user = db.query(User).filter(User.email == 'zinminkhant.alfred@gmail.com').first()
        if not user:
            user = User(
                email='zinminkhant.alfred@gmail.com',
                company='thunes.com',
                legal_name='Thunes PTE LTD',
                hashed_password='xxx',
                plan='pro_suite'
            )
            db.add(user)
            db.commit()
        else:
            user.legal_name = 'Thunes PTE LTD'
            db.commit()

        # 3. Setup Organisation
        org = db.query(Organisation).filter(Organisation.owner_user_id == user.id).first()
        if not org:
            org = Organisation(name=user.company, owner_user_id=user.id, slug="thunes-alfred-test")
            db.add(org)
            db.commit()

        # 4. Seed all 13 domains if they don't exist
        existing_controls = db.query(TrmControl).filter(TrmControl.organisation_id == org.id).all()
        if not existing_controls:
            for i, domain in enumerate(MAS_TRM_DOMAINS, 1):
                db.add(TrmControl(
                    organisation_id=org.id,
                    domain=domain,
                    control_ref=f"TRM-{i}",
                    status="not_started"
                ))
            db.commit()
            existing_controls = db.query(TrmControl).filter(TrmControl.organisation_id == org.id).all()

        # Clear existing test evidence for clean run
        control_ids = [c.id for c in existing_controls]
        db.query(TrmEvidence).filter(TrmEvidence.control_id.in_(control_ids)).delete(synchronize_session=False)
        db.commit()

        # 5. Populate specific domains with required MAS references
        now = datetime.datetime.utcnow()
        for c in existing_controls:
            if c.domain == "Cyber Security":
                c.status = "compliant"
                c.gap_analysis = "Notice 655 (FSM-N06) requirement met: Layered perimeter defence configured, MFA enforced on all privileged accounts, and rapid patching SLA (14 days for critical vulnerabilities) implemented."
                c.risk_rating = "low"
                
                ev = TrmEvidence(
                    control_id=c.id,
                    file_name="cyber_security_mfa_patching_test.pdf",
                    hash_value=str(uuid.uuid4()).replace("-", ""),
                    evidence_type="tested",
                    tested_at=now,
                    attestation="Verified MFA coverage and zero critical unpatched CVEs in last 30 days"
                )
                db.add(ev)
                
            elif c.domain == "Incident Management":
                c.status = "compliant"
                c.gap_analysis = "Notice 644 (FSM-N05) requirement met: Incident response plan updated to guarantee major incident notification to MAS within the 1-hour statutory deadline. Escalation matrix tested."
                c.risk_rating = "low"
                
                ev = TrmEvidence(
                    control_id=c.id,
                    file_name="incident_response_drill_2024.pdf",
                    hash_value=str(uuid.uuid4()).replace("-", ""),
                    evidence_type="tested",
                    tested_at=now - datetime.timedelta(days=15),
                    attestation="Annual IR drill. Detected and escalated simulated breach in 25 mins"
                )
                db.add(ev)
                
            elif c.domain == "Business Continuity and Disaster Recovery":
                c.status = "compliant"
                c.gap_analysis = "Notice 644 (FSM-N05) requirement met: Critical system RTO established at 2 hours (well within the 4-hour mandate) and RPO at 1 hour. Annual failover test conducted successfully."
                c.risk_rating = "low"
                
                ev = TrmEvidence(
                    control_id=c.id,
                    file_name="annual_dr_failover_test.pdf",
                    hash_value=str(uuid.uuid4()).replace("-", ""),
                    evidence_type="tested",
                    tested_at=now - datetime.timedelta(days=45),
                    attestation="Full active-active DR failover to secondary AWS region. RTO: 34m"
                )
                db.add(ev)

        db.commit()

        # 5.5 Seed WhiteLabelConfig
        from app.core.models import WhiteLabelConfig
        wl = db.query(WhiteLabelConfig).filter(WhiteLabelConfig.organisation_id == org.id).first()
        if not wl:
            wl = WhiteLabelConfig(
                organisation_id=org.id,
                primary_color="#10b981",
                secondary_color="#0f172a",
                footer_text="Thunes PTE LTD - Confidential TRM Baseline",
                report_header_text="Thunes Security & Compliance"
            )
            # Upload a dummy logo to S3
            try:
                from app.services.storage import S3Service
                s3 = S3Service()
                import base64
                dummy_png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
                logo_key = f"logos/{org.id}/logo.png"
                s3.s3_client.put_object(
                    Bucket=s3.bucket,
                    Key=logo_key,
                    Body=dummy_png,
                    ContentType="image/png"
                )
                wl.logo_s3_key = logo_key
            except Exception as e:
                print(f"Failed to upload mock logo: {e}")
            db.add(wl)
            db.commit()

        # 5.6 Seed subsidiaries
        for sub_name in ["Thunes Europe", "Thunes APAC"]:
            sub_email = f"admin@{sub_name.lower().replace(' ', '')}.com"
            sub_u = db.query(User).filter(User.email == sub_email).first()
            if not sub_u:
                sub_u = User(
                    email=sub_email,
                    company=sub_name,
                    legal_name=f"{sub_name} Subsidiary",
                    hashed_password='xxx',
                    plan='pro_suite',
                    parent_user_id=user.id
                )
                db.add(sub_u)
                db.commit()
            
            sub_org = db.query(Organisation).filter(Organisation.owner_user_id == sub_u.id).first()
            if not sub_org:
                sub_org = Organisation(name=sub_u.company, owner_user_id=sub_u.id, slug=f"{sub_name.lower().replace(' ', '')}-test")
                db.add(sub_org)
                db.commit()
            
            existing_sub_controls = db.query(TrmControl).filter(TrmControl.organisation_id == sub_org.id).all()
            if not existing_sub_controls:
                for i, domain in enumerate(MAS_TRM_DOMAINS, 1):
                    db.add(TrmControl(
                        organisation_id=sub_org.id,
                        domain=domain,
                        control_ref=f"TRM-{i}",
                        status="compliant" if domain in ["Cyber Security", "Cryptography"] else "in_progress" if domain == "Incident Management" else "not_started"
                    ))
                db.commit()
        
        # 6. Generate TRM Baseline PDF
        print(f"Triggering run_suite_trm_baseline_for_user for user {user.id}")
        run_suite_trm_baseline_for_user(str(user.id))
        
        db.refresh(user)
        print(f"User Legal Name after TRM gen: {user.legal_name}")
        print("Done! The PDF should be generated and ready.")

    finally:
        db.close()

if __name__ == "__main__":
    main()
