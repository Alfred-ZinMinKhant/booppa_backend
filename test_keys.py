import asyncio
from app.services.evidence_enricher import load_pdpc_precedent_index

async def main():
    index = load_pdpc_precedent_index()
    dpo_cases = index.get("categories", {}).get("openness_dpo", [])
    if dpo_cases:
        print("Keys:", list(dpo_cases[0].keys()))
        print("Summary:", dpo_cases[0].get("summary"))

asyncio.run(main())
