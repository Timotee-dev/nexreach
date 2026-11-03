import threading
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from agent.services import finder
from agent.services.loop import tick


class Command(BaseCommand):
    help = ("Run the NexReach agent: Claude check-ins every AGENT_INTERVAL_MINUTES (default 15), plus a fast "
            "email finder in the background every minute.")

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one finder round and one check-in, then exit.")
        parser.add_argument("--minutes", type=int, default=settings.AGENT_INTERVAL_MINUTES)
        parser.add_argument("--finder-seconds", type=int, default=60, help="Seconds between email finder rounds.")
        parser.add_argument("--no-finder", action="store_true", help="Old behaviour: find emails inside check-ins.")

    def handle(self, *args, once=False, minutes=15, finder_seconds=60, no_finder=False, **options):
        if once:
            if not no_finder:
                self.stdout.write(f"Email finder: {finder.cycle()}")
            self.stdout.write(self.style.SUCCESS(f"Check-in: {tick(finding=no_finder)}"))
            return

        stop = threading.Event()
        if not no_finder:
            thread = threading.Thread(
                target=finder.run_forever, args=(stop, finder_seconds, self.stdout.write), daemon=True
            )
            thread.start()
            self.stdout.write(self.style.SUCCESS(
                f"Email finder running every {finder_seconds}s (no Claude). Claude check-ins every {minutes} min."
            ))
        try:
            while True:
                self.stdout.write(f"[{time.strftime('%H:%M')}] Agent checking in...")
                results = tick(finding=no_finder)
                self.stdout.write(self.style.SUCCESS(f"  done: {results}"))
                self.stdout.write(f"  next check-in in {minutes} min (Ctrl+C to stop)")
                time.sleep(minutes * 60)
        except KeyboardInterrupt:
            stop.set()
            self.stdout.write("Stopped.")
