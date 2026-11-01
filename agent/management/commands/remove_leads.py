from django.core.management.base import BaseCommand, CommandError

from agent.models import Lead, log


class Command(BaseCommand):
    help = (
        "Delete leads that came from the wrong place. Examples:\n"
        "  python manage.py remove_leads --phone-prefix +856      (Laos numbers)\n"
        "  python manage.py remove_leads --no-market               (old leads not linked to any country)"
    )

    def add_arguments(self, parser):
        parser.add_argument("--phone-prefix", help="Delete leads whose phone starts with this, e.g. +856")
        parser.add_argument("--no-market", action="store_true", help="Delete found leads not linked to any market")
        parser.add_argument("--yes", action="store_true", help="Delete without asking")

    def handle(self, phone_prefix=None, no_market=False, yes=False, **options):
        if not phone_prefix and not no_market:
            raise CommandError("Say which leads to remove: --phone-prefix +856 or --no-market")
        qs = Lead.objects.none()
        if phone_prefix:
            digits = phone_prefix.lstrip("+")
            qs = qs | Lead.objects.filter(phone__startswith="+" + digits) | Lead.objects.filter(phone__startswith=digits)
        if no_market:
            qs = qs | Lead.objects.filter(market__isnull=True, source="osm")
        qs = qs.distinct()
        count = qs.count()
        if not count:
            self.stdout.write("Nothing matched.")
            return
        for name in qs.values_list("name", flat=True)[:10]:
            self.stdout.write(f"  {name}")
        if count > 10:
            self.stdout.write(f"  ...and {count - 10} more")
        if not yes and input(f"Delete these {count} leads? Type yes: ").strip().lower() != "yes":
            self.stdout.write("Cancelled.")
            return
        qs.delete()
        log(f"Removed {count} leads.", "warning")
        self.stdout.write(self.style.SUCCESS(f"Deleted {count} leads."))
