with open("docker-compose.yml", "r") as f:
    content = f.read()

services_to_patch = ["app:", "worker_fast:", "worker_heavy:", "django_admin:"]
for svc in services_to_patch:
    # Find the service block and add environment overrides after env_file
    target = f"    env_file: .env"
    replacement = f"    env_file: .env\n    environment:\n      - DATABASE_URL=postgresql://booppa:password@postgres:5432/booppa\n      - REDIS_URL=redis://redis:6379/0"
    # Ensure we only replace within the specific service block? No, just replace all env_file: .env with the new block.
    # But doing a global replace is easier and correct for all 4 services.

content = content.replace("    env_file: .env", "    env_file: .env\n    environment:\n      - DATABASE_URL=postgresql://booppa:password@postgres:5432/booppa\n      - REDIS_URL=redis://redis:6379/0")

with open("docker-compose.yml", "w") as f:
    f.write(content)
