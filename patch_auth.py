import re

with open("app/api/auth.py", "r") as f:
    content = f.read()

# Make sure Request is imported
if "from fastapi import Request" not in content and "Request" not in content:
    content = content.replace("from fastapi import APIRouter", "from fastapi import APIRouter, Request")

if "from app.core.limiter import limiter" not in content:
    content = "from app.core.limiter import limiter\n" + content

# Add @limiter.limit("5/minute") and request: Request to login_for_access_token
content = re.sub(
    r'(@router\.post\("/token", response_model=dict\)\s*def login_for_access_token\(\s*)',
    r'\1request: Request, ',
    content
)
content = re.sub(
    r'@router\.post\("/token", response_model=dict\)',
    r'@router.post("/token", response_model=dict)\n@limiter.limit("5/minute")',
    content
)

# Add @limiter.limit("5/minute") and request: Request to register_new_user
content = re.sub(
    r'(@router\.post\("/register", response_model=dict, status_code=status\.HTTP_201_CREATED\)\s*def register_new_user\(\s*)',
    r'\1request: Request, ',
    content
)
content = re.sub(
    r'@router\.post\("/register", response_model=dict, status_code=status\.HTTP_201_CREATED\)',
    r'@router.post("/register", response_model=dict, status_code=status.HTTP_201_CREATED)\n@limiter.limit("5/minute")',
    content
)

with open("app/api/auth.py", "w") as f:
    f.write(content)

print("Auth rate limits added")
