from app.core.db import SessionLocal
from app.core.models import User

db = SessionLocal()
users = db.query(User).filter(User.company == 'SPQR').all()
for u in users:
    print(f"ID: {u.id}")
    print(f"Email: {u.email}")
    print(f"Company: {u.company}")
    print(f"Full Name: {u.full_name}")
    
    # Fix the user
    u.company = "Test Company"
    u.full_name = "Test User"
    
db.commit()
print("Fixed.")
