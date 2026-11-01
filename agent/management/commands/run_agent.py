import time

from django.conf import settings
from django.core.management.base import BaseCommand

from agent.services.loop import tick


class Command(BaseCommand):
    help = "Run the NexReach agent: one check-in every AGENT_INTERVAL_MINUTES (default 15)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run a single check-in and exit.")
        parser.add_argument("--minutes", type=int, default=settings.AGENT_INTERVAL_MINUTES)

    def handle(self, *args, once=False, minutes=15, **options):
        while True:
            started = time.strftime("%H:%M")
            self.stdout.write(f"[{started}] Agent checking in...")
            results = tick()
            self.stdout.write(self.style.SUCCESS(f"  done: {results}"))
            if once:
                return
            self.stdout.write(f"  next check-in in {minutes} min (Ctrl+C to stop)")
            try:
                time.sleep(minutes * 60)
            except KeyboardInterrupt:
                self.stdout.write("Stopped.")
                return
