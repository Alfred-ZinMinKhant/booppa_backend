"""adopt the five Django-owned CMS tables into Alembic

`blog_posts`, `blog_post_images`, `rfp_tips`, `compliance_posts` and
`vendor_guides` were created and owned by the `booppa-cms` Django service
(`cms_admin/cms/migrations/0001`-`0005`), which is being retired into this app.

This revision is deliberately **conditional**, which is not the usual shape for
a migration here. The two environments it has to satisfy disagree:

- **Production / any DB the Django CMS ever ran against**: the tables already
  exist with live content. An unconditional `create_table` fails on
  `DuplicateTable` and takes the whole deploy down with it (`entrypoint.sh` runs
  `alembic upgrade head` before uvicorn boots).
- **A fresh DB** (CI, `booppa_ci`, a new developer): the tables have never
  existed, because Django's migrations are not part of this chain and the
  service that ran them is going away. `conftest` builds its schema from
  Alembic, not `Base.metadata.create_all`, so without this they simply are not
  there and every CMS test errors on `UndefinedTable`.

So: create only what is missing, per table. On prod this is a no-op that exists
purely to move the tables under Alembic's ownership.

Column types mirror what Django actually emitted, not this repo's usual
conventions — see the section note in `app/core/models.py`. The one that will
look like a bug: **the timestamp columns are not uniform.** `blog_posts` and
`blog_post_images` came from Django migration 0001, written before `USE_TZ` was
turned on, so they are `TIMESTAMP WITHOUT TIME ZONE`; `rfp_tips`,
`compliance_posts` and `vendor_guides` came from 0003-0005 with it on, so they
are `TIMESTAMP WITH TIME ZONE`. Confirmed against the production database on
2026-08-04. Since this revision skips tables that already exist, prod keeps
whatever it has regardless — the split is reproduced here so that a *fresh* DB
(CI) gets the same schema prod has, instead of a tidier one the code was never
tested against. `blog_post_images.id` is likewise a plain integer identity
rather than a UUID.

Deliberately NOT dropped here: Django's own bookkeeping tables (`auth_*`,
`django_migrations`, `django_content_type`, `django_session`,
`django_admin_log`). They are dead once the service is gone, but dropping them
in the same revision that performs the cutover would remove the ability to roll
back to Django. That belongs in a follow-up once the cutover has held.

Revision ID: 2026_08_04_0001
Revises: 2026_08_03_0004
Create Date: 2026-08-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "2026_08_04_0001"
down_revision = "2026_08_03_0004"
branch_labels = None
depends_on = None


# The four content tables are column-identical apart from `blog_posts.category`
# and the tz-awareness of their timestamps (see the module docstring).
def _content_columns(*, tz: bool) -> list:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("author", sa.String(255), nullable=True),
        sa.Column("cta1_text", sa.String(255), nullable=True),
        sa.Column("cta1_url", sa.String(1024), nullable=True),
        sa.Column("cta2_text", sa.String(255), nullable=True),
        sa.Column("cta2_url", sa.String(1024), nullable=True),
        sa.Column("published", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("published_at", sa.DateTime(timezone=tz), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=tz), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=tz), nullable=True),
    ]


# table -> whether its timestamps are tz-aware. `blog_posts` predates USE_TZ.
_CONTENT_TABLES = {
    "blog_posts": False,
    "rfp_tips": True,
    "compliance_posts": True,
    "vendor_guides": True,
}


def _existing_tables() -> set:
    bind = op.get_bind()
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()

    for table, tz in _CONTENT_TABLES.items():
        if table in existing:
            continue
        columns = _content_columns(tz=tz)
        if table == "blog_posts":
            columns.append(sa.Column("category", sa.String(64), nullable=True))
        op.create_table(table, *columns)
        op.create_index(f"ix_{table}_slug", table, ["slug"])

    if "blog_post_images" not in existing:
        op.create_table(
            "blog_post_images",
            # Django AutoField -> integer identity, not a UUID.
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True,
                      nullable=False),
            sa.Column("blog_post_id", postgresql.UUID(as_uuid=True),
                      nullable=False),
            # Was a MEDIA_ROOT-relative path under Django; holds the S3 object
            # key from the media migration onward.
            sa.Column("image", sa.String(255), nullable=False),
            sa.Column("caption", sa.String(255), nullable=True),
            # Naive, like its parent table — Django migration 0001.
            sa.Column("created_at", sa.DateTime(timezone=False), nullable=True),
            sa.ForeignKeyConstraint(["blog_post_id"], ["blog_posts.id"],
                                    ondelete="CASCADE"),
        )
        op.create_index("ix_blog_post_images_blog_post_id", "blog_post_images",
                        ["blog_post_id"])


def downgrade() -> None:
    """Deliberately a no-op.

    Because `upgrade()` is conditional, it is not knowable at downgrade time
    whether a given table was created by this revision (fresh DB) or by Django
    years earlier (production). "Drop whatever exists" reads as the symmetrical
    inverse but is not: on production it would delete every blog post, RFP tip,
    compliance post and vendor guide — content this revision never created and
    does not own.

    The cost of not dropping is an unused-but-empty set of tables on a fresh DB
    that gets rolled back. The cost of dropping is unrecoverable loss of live
    content. Downgrading past this revision is expected to leave the tables in
    place; drop them by hand if you genuinely want them gone and have checked
    what is in them.
    """
    pass
