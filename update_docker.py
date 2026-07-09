with open("docker-compose.yml", "r") as f:
    content = f.read()

# Replace the worker definition
old_worker = """  worker:
    build: .
    command: python -m celery -A app.workers.celery_app worker -B --loglevel=info -Q reports,default
    restart: unless-stopped
    env_file: .env
    depends_on:
      - redis
      - postgres
    volumes:
      - ./:/app:delegated"""

new_worker = """  worker_fast:
    build: .
    command: python -m celery -A app.workers.celery_app worker -B --loglevel=info -Q fast_queue
    restart: unless-stopped
    env_file: .env
    depends_on:
      - redis
      - postgres
    volumes:
      - ./:/app:delegated

  worker_heavy:
    build: .
    command: python -m celery -A app.workers.celery_app worker --loglevel=info -Q heavy_queue
    restart: unless-stopped
    env_file: .env
    depends_on:
      - redis
      - postgres
    volumes:
      - ./:/app:delegated"""

content = content.replace(old_worker, new_worker)

with open("docker-compose.yml", "w") as f:
    f.write(content)
