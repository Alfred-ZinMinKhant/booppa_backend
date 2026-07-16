from app.services.pdf_service import _finding_key_from
from app.services.pdpc_precedents import finding_category

f = {
    "title": "DPO Contact Not Publicly Disclosed",
    "severity": "MEDIUM"
}

key = _finding_key_from(f)
print(f"Key: {key}")

cat = finding_category(key)
print(f"Category: {cat}")
