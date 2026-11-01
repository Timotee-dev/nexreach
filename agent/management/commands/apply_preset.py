"""Switch NexReach between selling worldwide in dollars and selling in Nigeria in naira.

    python manage.py apply_preset global    # high-paying foreign markets, all high-value sectors, USD
    python manage.py apply_preset nigeria   # Nigerian businesses, naira prices

Changes the offer, ideal customer, sectors, writing rules and which markets are on.
Your name, signature, postal address, Gmail and limits stay as they are.
The prices are starting points — edit them in Settings to your real prices.
"""
from django.core.management.base import BaseCommand, CommandError

from agent.models import (
    DEFAULT_ICP, DEFAULT_OFFER, DEFAULT_WRITING_RULES, HIGH_VALUE_SECTORS, AgentConfig, Market, log,
)

PRESETS = {
    "global": {
        "label": "Worldwide high-value businesses, priced in US dollars",
        "markets_on": ["United States", "United Kingdom"],
        "markets_off": ["Nigeria"],
        "config": {
            "offer": DEFAULT_OFFER,
            "ideal_customer": DEFAULT_ICP,
            "writing_rules": DEFAULT_WRITING_RULES,
            "target_categories": HIGH_VALUE_SECTORS,
            "opt_out_line": "Not relevant? Reply \"unsubscribe\" and I won't email again.",
            "send_hour_start": 9,
            "send_hour_end": 16,
            "send_weekdays_only": True,
        },
    },
    "nigeria": {
        "label": "Nigerian businesses, priced in naira",
        "markets_on": ["Nigeria"],
        "markets_off": "all-others",
        "config": {
            "offer": (
                "Nexlab builds software for businesses: websites (from ₦150,000), online stores (from ₦250,000) "
                "and custom platforms such as booking, voting, inventory or logistics systems (from ₦600,000). "
                "Portfolio: https://nexlab0.netlify.app"
            ),
            "ideal_customer": (
                "Small and medium businesses that have no website, an outdated website, or a site that is not "
                "mobile friendly - e.g. restaurants, fashion stores, salons, schools, clinics, hotels and event centres."
            ),
            "writing_rules": (
                "Friendly and direct. Never invent facts, stats, clients or testimonials. Never use the word 'Founder'."
            ),
            "target_categories": "shop=clothes\namenity=restaurant\nshop=beauty\namenity=school\ntourism=hotel\namenity=clinic",
            "opt_out_line": "Not relevant? Just reply \"no thanks\" and I won't email again.",
            "send_hour_start": 8,
            "send_hour_end": 17,
            "send_weekdays_only": True,
        },
    },
}


class Command(BaseCommand):
    help = "Switch target: " + ", ".join(PRESETS)

    def add_arguments(self, parser):
        parser.add_argument("preset", nargs="?", help="One of: " + ", ".join(PRESETS))

    def handle(self, preset=None, **options):
        if not preset:
            for name, data in PRESETS.items():
                self.stdout.write(f"  {name:<9} {data['label']}")
            return
        if preset not in PRESETS:
            raise CommandError(f"Unknown preset '{preset}'. Choose from: {', '.join(PRESETS)}")
        data = PRESETS[preset]
        config = AgentConfig.get()
        for field, value in data["config"].items():
            setattr(config, field, value)
        config.discovery_cursor = 0
        config.full_clean()
        config.save()

        if data["markets_off"] == "all-others":
            Market.objects.exclude(name__in=data["markets_on"]).update(enabled=False)
        else:
            Market.objects.filter(name__in=data["markets_off"]).update(enabled=False)
        Market.objects.filter(name__in=data["markets_on"]).update(enabled=True)

        log(f"Switched target to: {data['label']}.", "success")
        self.stdout.write(self.style.SUCCESS(f"Now targeting: {data['label']}"))
        for market in Market.objects.filter(enabled=True):
            self.stdout.write(f"  ON  {market.name} ({len(market.city_list)} cities, {market.timezone})")
        others = list(Market.objects.filter(enabled=False).values_list("name", flat=True))
        if others:
            self.stdout.write(f"  off {', '.join(others)}  (turn on from the Markets page)")
        self.stdout.write(f"  Sectors: {len(config.categories)}")
        if Market.objects.filter(enabled=True, requires_postal_address=True).exists() and not config.postal_address.strip():
            self.stdout.write(self.style.WARNING(
                "  Add your business postal address in Settings: cold emails to these countries stay on hold until you do."
            ))
        self.stdout.write("  Review the offer and prices in Settings before approving any emails.")
