import os
import sys
import base64
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.enterprise_api import sso_router
from app.core.db import SessionLocal
from app.core.models import User, Organisation, SsoConfig, OrganisationMember

app = FastAPI()
app.include_router(sso_router, prefix="/api/v1/enterprise")
client = TestClient(app)

def main():
    db = SessionLocal()
    try:
        # Find test org
        org = db.query(Organisation).filter(Organisation.slug == "thunes-alfred-test").first()
        if not org:
            print("Org 'thunes-alfred-test' not found. Run seed_trm_test_case.py first.")
            return
            
        # Ensure user has PRO plan so SSO is allowed
        owner = db.query(User).filter(User.id == org.owner_user_id).first()
        if owner.plan != "pro_suite":
            owner.plan = "pro_suite"
            db.commit()

        # Setup SSO config
        sso = db.query(SsoConfig).filter(SsoConfig.organisation_id == org.id).first()
        if not sso:
            sso = SsoConfig(
                organisation_id=org.id,
                protocol="saml",
                idp_metadata_url="https://mock-idp.example.com/metadata",
                idp_entity_id="https://mock-idp.example.com",
                is_active=True
            )
            db.add(sso)
            db.commit()

        test_email = "jit-sso-user@thunes.com"
        
        # Clean up existing test user if present
        existing_user = db.query(User).filter(User.email == test_email).first()
        if existing_user:
            db.query(OrganisationMember).filter(OrganisationMember.user_id == existing_user.id).delete()
            db.delete(existing_user)
            db.commit()

        mock_identity = {
            "email": test_email,
            "name_id": test_email,
            "attributes": {}
        }
        
        with patch("app.services.saml_service.parse_assertion", return_value=mock_identity):
            print(f"Hitting SAML ACS endpoint for org: {org.slug}...")
            response = client.post(
                f"/api/v1/enterprise/sso/saml/acs/{org.slug}",
                data={"SAMLResponse": base64.b64encode(b"dummy_response").decode("utf-8"), "RelayState": "test"},
                follow_redirects=False
            )
            
            print(f"Response status: {response.status_code}")
            if response.status_code == 302:
                print(f"Redirect Location: {response.headers.get('location')}")
                if "token=" in response.headers.get('location', ''):
                    print("SUCCESS: Token fragment found in redirect!")
            else:
                print(f"Response: {response.text}")
                
            # Verify JIT user
            db.expire_all()
            jit_user = db.query(User).filter(User.email == test_email).first()
            if jit_user:
                print(f"SUCCESS: JIT user successfully provisioned: {jit_user.email}")
                member = db.query(OrganisationMember).filter(
                    OrganisationMember.user_id == jit_user.id, 
                    OrganisationMember.organisation_id == org.id
                ).first()
                if member:
                    print("SUCCESS: User successfully added to organisation")
                else:
                    print("FAILED: User NOT added to organisation!")
            else:
                print("FAILED: JIT user NOT provisioned!")
    finally:
        db.close()

if __name__ == "__main__":
    main()
