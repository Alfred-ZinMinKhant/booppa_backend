from fastapi.testclient import TestClient
from app.main import app
import os
import time

os.environ["ENVIRONMENT"] = "test"
os.environ["SECRET_KEY"] = "dummy"

from app.api.admin import _admin_auth
app.dependency_overrides[_admin_auth] = lambda: True

client = TestClient(app)

def verify_product(product_type):
    print(f"\n--- Simulating Purchase for: {product_type} ---")
    response = client.post(
        "/api/admin/simulate-purchase",
        json={
            "product_type": product_type,
            "customer_email": "zinminkhant.alfred@gmail.com",
            "vendor_url": "https://www.google.com",
            "company_name": "Test Company",
            "rfp_description": "Test verification"
        }
    )
    print("Status:", response.status_code)
    try:
        print("Response:", response.json())
    except:
        print("Response:", response.text)

if __name__ == "__main__":
    verify_product("compliance_evidence_pack")
    verify_product("pdpa_monitor_monitoring")
    verify_product("rfp_accelerator")
