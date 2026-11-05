"""Gmail's daily limit made a batch of emails fail with 'connect() first' / 'connection closed' errors,
and they were wrongly marked Failed. Put them back in the approved queue."""
from django.db import migrations
from django.db.models import Q


def requeue(apps, schema_editor):
    Message = apps.get_model("agent", "Message")
    (Message.objects.filter(direction="out", status="failed")
     .filter(Q(error__icontains="5.4.5") | Q(error__icontains="connect() first")
             | Q(error__icontains="unexpectedly closed") | Q(error__icontains="sending limit")
             | Q(error__icontains="daily user"))
     .update(status="approved", error=""))


class Migration(migrations.Migration):
    dependencies = [("agent", "0020_multiple_inboxes")]
    operations = [migrations.RunPython(requeue, migrations.RunPython.noop)]
