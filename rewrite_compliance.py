import re

with open("app/api/compliance.py", "r") as f:
    content = f.read()

# 1. Imports
if "from app.core.repositories.report_repository import ReportRepository" not in content:
    content = content.replace("from app.core.models import Report, User", "from app.core.models import Report, User\nfrom app.core.repositories.report_repository import ReportRepository\nfrom app.core.repositories.user_repository import UserRepository")

# 2. Cover sheet download query
old_1 = """        report = (
            db.query(Report)
            .filter(
                Report.id == report_id,
                Report.framework == "compliance_evidence_pack",
            )
            .first()
        )"""
new_1 = """        report = ReportRepository.get_by_id_and_framework(db, report_id, "compliance_evidence_pack")"""
content = content.replace(old_1, new_1)

# 3. cover_sheet_status queries
old_user = """        user = db.query(User).filter(User.email == email).first()"""
new_user = """        user = UserRepository.get_by_email(db, email)"""
content = content.replace(old_user, new_user)

old_pdpa = """        pdpa = (
            db.query(Report)
            .filter(
                Report.owner_id == user.id,
                Report.framework.in_(["pdpa_quick_scan", "pdpa_snapshot"]),
            )
            .order_by(Report.created_at.desc())
            .first()
        )"""
new_pdpa = """        pdpa = ReportRepository.get_latest_for_owner_by_frameworks(db, user.id, ["pdpa_quick_scan", "pdpa_snapshot"])"""
content = content.replace(old_pdpa, new_pdpa)

old_rfp = """        rfp = (
            db.query(Report)
            .filter(Report.owner_id == user.id, Report.framework == "rfp_complete")
            .order_by(Report.created_at.desc())
            .first()
        )"""
new_rfp = """        rfp = ReportRepository.get_latest_for_owner_by_framework(db, user.id, "rfp_complete")"""
content = content.replace(old_rfp, new_rfp)

old_cs = """        cs = (
            db.query(Report)
            .filter(Report.owner_id == user.id, Report.framework == "compliance_evidence_pack")
            .order_by(Report.created_at.desc())
            .first()
        )"""
new_cs = """        cs = ReportRepository.get_latest_for_owner_by_framework(db, user.id, "compliance_evidence_pack")"""
content = content.replace(old_cs, new_cs)

old_existing = """        existing = (
            db.query(Report)
            .filter(
                Report.owner_id == user.id,
                Report.framework == "compliance_evidence_pack",
            )
            .order_by(Report.created_at.desc())
            .first()
        )"""
new_existing = """        existing = ReportRepository.get_latest_for_owner_by_framework(db, user.id, "compliance_evidence_pack")"""
content = content.replace(old_existing, new_existing)

old_signed = """        signed_q = db.query(Report).filter(
            Report.owner_id == user.id,
            Report.framework == "compliance_evidence_signed_sheet",
        )
        if pdpa and pdpa.created_at:
            signed_q = signed_q.filter(Report.created_at >= pdpa.created_at)
        signed = signed_q.order_by(Report.created_at.desc()).first()"""
new_signed = """        # Keep inline query for conditional timestamp filter since it's highly specific
        signed_q = db.query(Report).filter(
            Report.owner_id == user.id,
            Report.framework == "compliance_evidence_signed_sheet",
        )
        if pdpa and pdpa.created_at:
            signed_q = signed_q.filter(Report.created_at >= pdpa.created_at)
        signed = signed_q.order_by(Report.created_at.desc()).first()"""
content = content.replace(old_signed, new_signed)


old_user_lock = """        user = (
            db.query(User)
            .filter(User.email == email)
            .with_for_update()
            .first()
        )"""
new_user_lock = """        user = UserRepository.get_by_email(db, email, lock_for_update=True)"""
content = content.replace(old_user_lock, new_user_lock)

old_user_payload_lock = """        user = (
            db.query(User)
            .filter(User.email == payload.email)
            .with_for_update()
            .first()
        )"""
new_user_payload_lock = """        user = UserRepository.get_by_email(db, payload.email, lock_for_update=True)"""
content = content.replace(old_user_payload_lock, new_user_payload_lock)


old_completed_cs = """        cover_sheet = (
            db.query(Report)
            .filter(
                Report.owner_id == user.id,
                Report.framework == "compliance_evidence_pack",
                Report.status == "completed",
            )
            .order_by(Report.created_at.desc())
            .first()
        )"""
new_completed_cs = """        cover_sheet = ReportRepository.get_latest_for_owner_by_framework(
            db, user.id, "compliance_evidence_pack", status="completed"
        )"""
content = content.replace(old_completed_cs, new_completed_cs)


with open("app/api/compliance.py", "w") as f:
    f.write(content)
