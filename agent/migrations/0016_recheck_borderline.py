"""The cheaper sorting model scored good businesses too low (50-59 with positive reasons).
Send recent borderline 'not a fit' ones back to be scored again with the clearer scale."""
from datetime import timedelta

from django.db import migrations
from django.utils import timezone


def recheck(apps, schema_editor):
    Lead = apps.get_model("agent", "Lead")
    (Lead.objects.filter(kind="business", status="disqualified", fit_score__gte=40, fit_score__lt=60,
                         researched_at__gte=timezone.now() - timedelta(days=2))
     .exclude(email="").exclude(fit_reason__startswith="Chain")
     .update(status="new", researched_at=None, fit_score=0))


class Migration(migrations.Migration):
    dependencies = [("agent", "0015_claude_limit_pause")]
    operations = [migrations.RunPython(recheck, migrations.RunPython.noop)]
