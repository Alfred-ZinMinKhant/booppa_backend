from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("cms", "0003_add_cta_fields"),
    ]

    operations = [
        # ── RFP Tips ──────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="RfpTip",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("title", models.CharField(max_length=255)),
                ("slug", models.SlugField(max_length=255, unique=True)),
                ("content", models.TextField()),
                ("author", models.CharField(blank=True, max_length=255, null=True)),
                ("cta1_text", models.CharField(blank=True, max_length=255, null=True)),
                ("cta1_url", models.CharField(blank=True, max_length=1024, null=True)),
                ("cta2_text", models.CharField(blank=True, max_length=255, null=True)),
                ("cta2_url", models.CharField(blank=True, max_length=1024, null=True)),
                ("published", models.BooleanField(default=False)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "rfp_tips", "verbose_name": "RFP Tip", "verbose_name_plural": "RFP Tips"},
        ),
        # ── Compliance Education ──────────────────────────────────────────────
        migrations.CreateModel(
            name="CompliancePost",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("title", models.CharField(max_length=255)),
                ("slug", models.SlugField(max_length=255, unique=True)),
                ("content", models.TextField()),
                ("author", models.CharField(blank=True, max_length=255, null=True)),
                ("cta1_text", models.CharField(blank=True, max_length=255, null=True)),
                ("cta1_url", models.CharField(blank=True, max_length=1024, null=True)),
                ("cta2_text", models.CharField(blank=True, max_length=255, null=True)),
                ("cta2_url", models.CharField(blank=True, max_length=1024, null=True)),
                ("published", models.BooleanField(default=False)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "compliance_posts", "verbose_name": "Compliance Education", "verbose_name_plural": "Compliance Education"},
        ),
        # ── Vendor Guides ─────────────────────────────────────────────────────
        migrations.CreateModel(
            name="VendorGuide",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("title", models.CharField(max_length=255)),
                ("slug", models.SlugField(max_length=255, unique=True)),
                ("content", models.TextField()),
                ("author", models.CharField(blank=True, max_length=255, null=True)),
                ("cta1_text", models.CharField(blank=True, max_length=255, null=True)),
                ("cta1_url", models.CharField(blank=True, max_length=1024, null=True)),
                ("cta2_text", models.CharField(blank=True, max_length=255, null=True)),
                ("cta2_url", models.CharField(blank=True, max_length=1024, null=True)),
                ("published", models.BooleanField(default=False)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "vendor_guides", "verbose_name": "Vendor Guide", "verbose_name_plural": "Vendor Guides"},
        ),
    ]
