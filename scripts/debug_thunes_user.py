import sys
import os

from app.core.db import SessionLocal
from app.core.models import User, Organisation, TrmControl, TrmEvidence

db = SessionLocal()
user = db.query(User).filter(User.email.like('%@thunes.com')).first()
if not user:
    print("No thunes user found")
    sys.exit(0)

org = db.query(Organisation).filter(Organisation.owner_user_id == user.id).first()
if not org:
    print("No org for thunes user")
    sys.exit(0)

print(f"User: {user.email}, Legal Name: {user.legal_name}, Company: {user.company}")
print(f"Org: {org.name}")

controls = db.query(TrmControl).filter(TrmControl.organisation_id == org.id).all()
for c in controls:
    print(f"TRM Control: {c.domain} - {c.status}")

