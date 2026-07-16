import asyncio
from app.services.evidence_enricher import build_pdpc_precedent_index, load_pdpc_precedent_index

async def main():
    index = load_pdpc_precedent_index()
    if not index:
        index = await build_pdpc_precedent_index()
    
    cats = index.get("categories", {})
    print("Categories available:", list(cats.keys()))
    
    dpo_cases = cats.get("openness_dpo", [])
    print(f"openness_dpo cases: {len(dpo_cases)}")
    for c in dpo_cases[:2]:
        print(f" - {c.get('vendor')} (URL: {c.get('url')})")

asyncio.run(main())
