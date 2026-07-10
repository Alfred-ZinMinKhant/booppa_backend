import os
import re

conftest = "tests/conftest.py"
with open(conftest, 'r') as f:
    content = f.read()

patch_code = """
@pytest.fixture(autouse=True)
def _patch_sessionlocal(monkeypatch):
    from app.core import db
    monkeypatch.setattr(db, "SessionLocal", TestingSessionLocal)
"""

if "_patch_sessionlocal" not in content:
    content += patch_code
    with open(conftest, 'w') as f:
        f.write(content)
    print("Patched conftest.py")
