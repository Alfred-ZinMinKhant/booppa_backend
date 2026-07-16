import sys
from app.api.auth import RegisterRequest
from app.api.admin import SimulatePurchaseRequest
from pydantic import ValidationError

def test_register():
    try:
        RegisterRequest(email="test@booppa.io", password="pass", company="SPQR")
        print("FAIL: RegisterRequest allowed 'SPQR'")
        sys.exit(1)
    except ValidationError as e:
        print("PASS: RegisterRequest blocked 'SPQR'")
        
    try:
        RegisterRequest(email="test@booppa.io", password="pass", company="Acme Corp")
        print("PASS: RegisterRequest allowed 'Acme Corp'")
    except ValidationError as e:
        print(f"FAIL: RegisterRequest blocked 'Acme Corp': {e}")
        sys.exit(1)

def test_admin():
    try:
        SimulatePurchaseRequest(product_type="compliance_evidence_pack", customer_email="test@booppa.io", company_name="SPQR")
        print("FAIL: SimulatePurchaseRequest allowed 'SPQR'")
        sys.exit(1)
    except ValidationError as e:
        print("PASS: SimulatePurchaseRequest blocked 'SPQR'")
        
    try:
        SimulatePurchaseRequest(product_type="compliance_evidence_pack", customer_email="test@booppa.io", company_name="Booppa QA")
        print("PASS: SimulatePurchaseRequest allowed 'Booppa QA'")
    except ValidationError as e:
        print(f"FAIL: SimulatePurchaseRequest blocked 'Booppa QA': {e}")
        sys.exit(1)

if __name__ == "__main__":
    test_register()
    test_admin()
    print("All tests passed.")
