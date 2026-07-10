import os

conftest = "tests/conftest.py"
with open(conftest, 'r') as f:
    content = f.read()

# Make conftest set os.environ["DATABASE_URL"] before importing app
patch = """import os
os.environ["DATABASE_URL"] = "postgresql+psycopg2://booppa:password@localhost:5432/booppa_test"

"""

if "os.environ[\"DATABASE_URL\"]" not in content:
    content = patch + content
    with open(conftest, 'w') as f:
        f.write(content)
    print("Patched conftest to set DATABASE_URL")
