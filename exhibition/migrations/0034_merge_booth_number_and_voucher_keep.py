from django.db import migrations


class Migration(migrations.Migration):
    """Join the two 0033 leaves that landed from #354 and #332 onto 0032."""

    dependencies = [
        ("exhibition", "0033_exhibitorvoucher_keep_after_delete"),
        ("exhibition", "0033_rename_booth_id_booth_number"),
    ]

    operations = []
