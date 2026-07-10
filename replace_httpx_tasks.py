import re

with open("app/workers/tasks.py", "r") as f:
    content = f.read()

# Make sure we import get_async_client if not imported
if "from app.core.http_client import get_async_client" not in content:
    content = "from app.core.http_client import get_async_client\n" + content

content = re.sub(
    r"httpx\.AsyncClient\(timeout=([0-9.]+),\s*follow_redirects=True\)",
    r"get_async_client(timeout=\1, follow_redirects=True)",
    content
)

content = re.sub(
    r"httpx\.AsyncClient\(timeout=timeout,\s*follow_redirects=True\)",
    r"get_async_client(timeout=timeout, follow_redirects=True)",
    content
)

content = re.sub(
    r"httpx\.AsyncClient\(timeout=30\)",
    r"get_async_client(timeout=30.0)",
    content
)

with open("app/workers/tasks.py", "w") as f:
    f.write(content)

print("Replaced httpx in tasks.py")
