import asyncio
from app.core.db import SessionLocal
from app.core.models import Report

async def main():
    db = SessionLocal()
    reports = db.query(Report).all()
    for r in reports:
        if r.company_website and 'ensign' in r.company_website.lower():
            print(f"Found via company_website: {r.company_website} (ID: {r.id})")
        if r.company_name and 'ensign' in r.company_name.lower():
            print(f"Found via company_name: {r.company_name} (ID: {r.id})")
        
        # also check assessment_data
        if isinstance(r.assessment_data, dict):
            url = r.assessment_data.get('website_url') or r.assessment_data.get('url') or ''
            if 'ensign' in url.lower():
                print(f"Found via assessment_data url: {url} (ID: {r.id})")

if __name__ == "__main__":
    asyncio.run(main())
