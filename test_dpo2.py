import asyncio
from app.services.pdpc_precedents import precedent_summary, regulatory_basis

key = "free:dpo_contact_not_publicly_disclosed"
ps = precedent_summary(key)
rb = regulatory_basis(key)

print("Precedent Summary:", ps)
print("Regulatory Basis:", rb)
