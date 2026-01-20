"""
Generated migration to add CTA fields to BlogPost.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cms", "0002_alter_blogpost_content"),
    ]

    operations = [
        migrations.AddField(
            model_name="blogpost",
            name="cta1_text",
            field=models.CharField(max_length=255, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="blogpost",
            name="cta1_url",
            field=models.CharField(max_length=1024, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="blogpost",
            name="cta2_text",
            field=models.CharField(max_length=255, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="blogpost",
            name="cta2_url",
            field=models.CharField(max_length=1024, null=True, blank=True),
        ),
    ]
