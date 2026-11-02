from django.core.management.base import BaseCommand

from agent.services.gigs import find_gigs


class Command(BaseCommand):
    help = "Look for gig posts (Reddit [Hiring] posts and Hacker News 'Seeking freelancer') right now."

    def handle(self, *args, **options):
        found = find_gigs(force=True)
        self.stdout.write(self.style.SUCCESS(f"Found {found} new gig posts. Run run_agent --once to score them and write replies."))
