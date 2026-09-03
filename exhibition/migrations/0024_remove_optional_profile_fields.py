from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("exhibition", "0023_voucher_defaults_and_email_role"),
    ]

    operations = [
        migrations.RemoveField(model_name="exhibitorinfo", name="contact_url"),
        migrations.RemoveField(model_name="exhibitorinfo", name="video_url"),
        migrations.RemoveField(model_name="exhibitorinfo", name="slides"),
        migrations.RemoveField(model_name="exhibitorinfo", name="slides_url"),
        migrations.RemoveField(model_name="exhibitionproposal", name="contact_url"),
        migrations.RemoveField(model_name="exhibitionproposal", name="video_url"),
        migrations.RemoveField(model_name="exhibitionproposal", name="slides"),
        migrations.RemoveField(model_name="exhibitionproposal", name="slides_url"),
        migrations.RemoveField(model_name="exhibitionproposal", name="notes"),
        migrations.RemoveField(model_name="exhibitorextralink", name="exhibitor"),
        migrations.RemoveField(model_name="exhibitionproposalextralink", name="proposal"),
        migrations.DeleteModel(name="ExhibitorExtraLink"),
        migrations.DeleteModel(name="ExhibitionProposalExtraLink"),
    ]
