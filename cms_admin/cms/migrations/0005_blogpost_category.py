from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cms", "0004_rfp_tips_compliance_vendor_guides"),
    ]

    operations = [
        migrations.AddField(
            model_name="blogpost",
            name="category",
            field=models.CharField(max_length=64, null=True, blank=True),
        ),
    ]
