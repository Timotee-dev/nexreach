from django.core.management.base import BaseCommand

from agent.services.discovery import discover


class Command(BaseCommand):
    help = 'Search OpenStreetMap now, e.g.: python manage.py find_leads "Lagos" "shop=clothes"'

    def add_arguments(self, parser):
        parser.add_argument("city")
        parser.add_argument("tag", help="OpenStreetMap tag such as shop=clothes")
        parser.add_argument("--limit", type=int, default=40)

    def handle(self, city, tag, limit, **options):
        created = discover(city, tag, limit)
        self.stdout.write(self.style.SUCCESS(f"Added {created} new businesses."))
