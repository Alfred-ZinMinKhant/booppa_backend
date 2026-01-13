Django admin service (side-by-side)

Local quickstart

- Build image:
  ```bash
  docker build -t booppa-django-admin:local .
  ```

- Run with `.env` in repo root (it must contain `DATABASE_URL` pointing at your Postgres):
  ```bash
  docker run --rm -p 8001:8001 --env-file ../.env booppa-django-admin:local
  ```

- Create superuser (run inside container):
  ```bash
  docker run --rm --env-file ../.env -it booppa-django-admin:local python manage.py createsuperuser
  ```

- Access admin at: http://localhost:8001/django-admin/

Notes
- The `BlogPost` model in `cms/models.py` maps to the existing `blog_posts` table and is `managed=False` to avoid Django migrations touching it. If you'd rather Django own the table, set `managed=True` and create migrations.
