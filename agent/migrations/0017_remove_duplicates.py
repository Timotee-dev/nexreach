"""One-off clean-up: the same business was found twice and got two (or four) drafts."""
from django.db import migrations


def run(apps, schema_editor):
    from agent.services import research
    research.mark_duplicates()


class Migration(migrations.Migration):
    dependencies = [("agent", "0016_recheck_borderline")]
    operations = [migrations.RunPython(run, migrations.RunPython.noop)]
