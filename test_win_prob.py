from app.core.db import SessionLocal
from app.services.tender_service import compute_tender_win_probability
from app.core.models import User
db = SessionLocal()
user = db.query(User).filter(User.company.ilike('%ecloudvalley%')).first()
if user:
    print(f"Vendor: {user.company_name}, ID: {user.id}")
    from app.services.vendor_active_insights import get_tender_matches
    matches = get_tender_matches(db, str(user.id), limit=10, with_win_probability=True)
    for m in matches:
        print(f"{m['tender_no']}: {m.get('win_probability')}% - {m['title'][:30]}")
else:
    print("User not found")
