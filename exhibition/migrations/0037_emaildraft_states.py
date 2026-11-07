from django.db import migrations, models


def state_to_states(apps, schema_editor):
    draft_model = apps.get_model("exhibition", "ExhibitionEmailDraft")
    for draft in draft_model.objects.exclude(state=""):
        draft.states = [draft.state]
        draft.save(update_fields=["states"])


def states_to_state(apps, schema_editor):
    draft_model = apps.get_model("exhibition", "ExhibitionEmailDraft")
    for draft in draft_model.objects.all():
        if draft.states:
            draft.state = draft.states[0]
            draft.save(update_fields=["state"])


class Migration(migrations.Migration):
    dependencies = [
        ("exhibition", "0036_exhibition_sales"),
    ]

    operations = [
        migrations.AddField(
            model_name="exhibitionemaildraft",
            name="states",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(state_to_states, reverse_code=states_to_state),
        migrations.RemoveField(
            model_name="exhibitionemaildraft",
            name="state",
        ),
    ]
