"""Add agency sectors (white-label partners) to the search list, keeping every existing sector."""
from django.db import migrations

NEW = ["office=advertising_agency", "office=web_design"]


def add(apps, schema_editor):
    AgentConfig = apps.get_model("agent", "AgentConfig")
    for config in AgentConfig.objects.all():
        lines = [l.strip() for l in config.target_categories.splitlines() if l.strip()]
        for tag in NEW:
            if tag not in lines:
                lines.append(tag)
        config.target_categories = "\n".join(lines)
        config.save(update_fields=["target_categories"])


class Migration(migrations.Migration):
    dependencies = [("agent", "0017_remove_duplicates")]
    operations = [migrations.RunPython(add, migrations.RunPython.noop)]
