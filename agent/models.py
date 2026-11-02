import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def validate_timezone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(f"'{value}' isn't a time zone name. Examples: Africa/Lagos, America/Chicago, Europe/London.") from exc


DEFAULT_OFFER = (
    "Nexlab is a software studio that builds custom software for established businesses:\n"
    "- Modern, fast, mobile-friendly websites with online booking or enquiry forms (from $3,000)\n"
    "- Client portals, booking and payment systems, and dashboards (from $5,000)\n"
    "- Internal tools that replace spreadsheets, paperwork and manual admin (from $6,000)\n"
    "- AI assistants that answer enquiries, qualify leads and book appointments (from $4,000)\n"
    "- Ongoing hosting, support and improvements (from $300/month)\n"
    "Every project gets a fixed price before work starts, and we can send a free mockup or a short plan first.\n"
    "Portfolio: https://nexlab0.netlify.app"
)
DEFAULT_ICP = (
    "Established, profitable small and mid-sized businesses (roughly 5-200 staff) in high-value sectors - "
    "healthcare, legal, accounting, financial services, real estate, architecture, construction and home "
    "services, hospitality, education and other professional services - that can afford projects of $3,000+ "
    "and show a clear gap we can fix: an outdated, slow, insecure or non-mobile website, no online booking or "
    "client portal, signs of manual processes, or slow enquiry handling. Not large enterprises, chains or "
    "franchises with corporate websites, and not businesses that already have modern systems."
)
DEFAULT_WRITING_RULES = (
    "Use the spelling of the recipient's country (US or UK English). Short, specific and professional. "
    "Never mention prices in a first email or a follow-up - offer a free mockup or a short plan instead. "
    "If someone asks about cost, give the starting price in US dollars from WHAT WE SELL and offer a fixed quote. "
    "Never invent facts, stats, clients or testimonials. Never use the word 'Founder'."
)
HIGH_VALUE_SECTORS = "\n".join([
    "office=lawyer", "office=accountant", "amenity=dentist", "amenity=clinic", "amenity=doctors",
    "office=estate_agent", "office=financial_advisor", "office=insurance", "office=architect",
    "amenity=veterinary", "craft=hvac", "craft=plumber", "craft=roofer", "craft=electrician",
    "shop=car", "tourism=hotel", "leisure=fitness_centre",
    "healthcare=physiotherapist", "healthcare=optometrist", "office=property_management", "office=consulting",
    "office=employment_agency", "office=travel_agent", "amenity=school", "amenity=college", "craft=carpenter",
    "shop=jewelry", "shop=furniture", "tourism=guest_house",
])


class Market(models.Model):
    """A country you sell into: its cities, time zone, phone code and currency."""

    name = models.CharField(max_length=60, unique=True)
    enabled = models.BooleanField(default=False, help_text="Search, research and email businesses here.")
    currency = models.CharField(max_length=3, default="USD")
    calling_code = models.CharField(max_length=4, help_text="Phone calling code, e.g. 1, 44, 61, 971, 234.")
    timezone = models.CharField(
        max_length=64, validators=[validate_timezone],
        help_text="Main business time zone, e.g. America/Chicago, Europe/London.",
    )
    cities = models.TextField(help_text="One city per line, with the country, e.g. 'Manchester, United Kingdom'.")
    requires_postal_address = models.BooleanField(
        default=True, help_text="Hold cold emails until your postal address is set in Settings."
    )
    rules_note = models.CharField(max_length=300, blank=True, help_text="Cold-email rules to keep in mind here.")
    language = models.CharField(max_length=40, default="English", help_text="Language Claude writes in for this country.")
    area_ids = models.JSONField(default=dict, blank=True, editable=False)
    sort = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort", "name"]

    def __str__(self):
        return self.name

    @property
    def city_list(self):
        return [line.strip() for line in self.cities.splitlines() if line.strip()]

    def local_now(self):
        try:
            return timezone.now().astimezone(ZoneInfo(self.timezone))
        except (ZoneInfoNotFoundError, ValueError):
            return timezone.localtime()


class AgentConfig(models.Model):
    """Single row holding everything the agent needs to know about you."""

    agent_enabled = models.BooleanField(default=True, help_text="Untick to pause the agent loop.")

    # Who you are
    sender_name = models.CharField(max_length=100, default="Nexora")
    company_name = models.CharField(max_length=100, default="Nexlab")
    company_website = models.URLField(default="https://nexlab0.netlify.app")
    signature = models.TextField(
        default="Nexora from Nexlab\nSoftware. Systems. Built to Scale.\nhttps://nexlab0.netlify.app"
    )

    # What you sell and to whom
    offer = models.TextField(default=DEFAULT_OFFER, help_text="Your services, starting prices (USD) and proof. Claude picks the most relevant one for each business.")
    ideal_customer = models.TextField(default=DEFAULT_ICP, help_text="Who is worth contacting.")
    writing_rules = models.TextField(
        blank=True, default=DEFAULT_WRITING_RULES, help_text="Extra rules Claude must follow when writing."
    )

    # What to look for (cities live on each Market)
    target_categories = models.TextField(
        default=HIGH_VALUE_SECTORS,
        help_text="Sectors to search, one OpenStreetMap tag per line, e.g. office=lawyer or amenity=dentist.",
    )
    discovery_cursor = models.PositiveIntegerField(default=0)

    # Limits
    daily_send_limit = models.PositiveIntegerField(default=20)
    sends_per_tick = models.PositiveIntegerField(default=5)
    research_per_tick = models.PositiveIntegerField(default=5)
    drafts_per_tick = models.PositiveIntegerField(default=5)
    min_fit_score = models.PositiveIntegerField(default=60)
    followup_days = models.PositiveIntegerField(default=4)
    max_followups = models.PositiveIntegerField(default=2)
    min_new_leads = models.PositiveIntegerField(
        default=10, help_text="Search for more businesses when unresearched leads drop below this."
    )
    opt_out_line = models.CharField(
        max_length=200,
        default="Not relevant? Reply \"unsubscribe\" and I won't email again.",
    )

    # Sending & compliance
    postal_address = models.CharField(
        max_length=255,
        blank=True,
        help_text="Your real business mailing address. Added to every email; US law (CAN-SPAM) requires one.",
    )
    send_hour_start = models.PositiveSmallIntegerField(default=9, help_text="Earliest hour to send, in the business's own time (0-23).")
    send_hour_end = models.PositiveSmallIntegerField(default=16, help_text="Stop sending at this hour, in the business's own time (1-24).")
    send_weekdays_only = models.BooleanField(default=True, help_text="Don't send cold emails on their Saturday or Sunday.")
    home_timezone = models.CharField(
        max_length=64, default="Africa/Lagos", validators=[validate_timezone],
        help_text="Used for leads that aren't linked to a market.",
    )

    # Email finding
    daily_email_target = models.PositiveIntegerField(
        default=30, help_text="Keep searching each day until this many new business emails are found."
    )
    scan_per_tick = models.PositiveIntegerField(
        default=25, help_text="Websites to scan for contact details per check-in (free, no Claude)."
    )
    research_dm_leads = models.BooleanField(
        default=False,
        help_text="Also spend Claude on businesses with no email (for direct messages). Off = emails first.",
    )
    searches_when_behind = models.PositiveIntegerField(
        default=6, help_text="Map searches per check-in while today's email target isn't met."
    )
    max_sector_share = models.PositiveIntegerField(
        default=20, help_text="Max % of the last 7 days' leads one sector may take before other sectors go first."
    )
    skip_poor_sectors = models.BooleanField(
        default=True, help_text="Skip sectors where scanned websites rarely show an email."
    )
    only_contactable = models.BooleanField(
        default=True, help_text="When searching maps, only collect businesses that list a website or email."
    )

    # Extra business source
    foursquare_monthly_limit = models.PositiveIntegerField(
        default=450,
        help_text="Max Foursquare searches per month (500 are free). Needs FOURSQUARE_API_KEY in .env.",
    )

    # Gig postings: people asking for a developer
    gigs_enabled = models.BooleanField(default=True, help_text="Look for posts from people who want something built.")
    gig_subreddits = models.TextField(
        default="forhire\nfreelance_forhire\nhiring\njobbit\nWebDeveloperJobs",
        help_text="Subreddits to check for [Hiring] posts, one per line (without r/).",
    )
    hn_gigs_enabled = models.BooleanField(default=True, help_text="Check Hacker News' monthly 'Seeking freelancer' thread.")
    gig_keywords = models.TextField(
        default="website\nweb app\nweb developer\ndeveloper\nprogrammer\nlanding page\nwordpress\nshopify\n"
                "django\npython\nreact\nnext.js\njavascript\nfull stack\nbackend\nfrontend\napp\nsaas\n"
                "dashboard\nportal\nautomation\nchatbot\nai\napi\nscraper\nsoftware",
        help_text="A post must mention at least one of these, one per line.",
    )
    remotive_enabled = models.BooleanField(
        default=True, help_text="Contract and freelance software listings from Remotive (checked 4 times a day)."
    )
    mastodon_enabled = models.BooleanField(default=True, help_text="Look for hiring posts on Mastodon hashtags.")
    mastodon_tags = models.TextField(
        default="hiring\nfreelance\nhiringnow\nwebdevelopment",
        help_text="Hashtags to check (without #), one per line.",
    )
    mastodon_instance = models.CharField(max_length=100, default="mastodon.social")
    remotive_checked_at = models.DateTimeField(null=True, blank=True)
    gig_source_pauses = models.JSONField(default=dict, blank=True)  # {"reddit:jobbit": "2026-11-03T..."}
    gig_max_age_days = models.PositiveIntegerField(default=7, help_text="Ignore posts older than this.")
    gig_check_minutes = models.PositiveIntegerField(default=60, help_text="How often to look for new gigs.")
    gigs_checked_at = models.DateTimeField(null=True, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Agent settings"
        verbose_name_plural = "Agent settings"

    def __str__(self):
        return "Agent settings"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def categories(self):
        return [c.strip() for c in self.target_categories.splitlines() if "=" in c]

    @property
    def subreddits(self):
        return [s.strip().removeprefix("r/").strip("/") for s in self.gig_subreddits.splitlines() if s.strip()]

    @property
    def hashtags(self):
        return [t.strip().lstrip("#") for t in self.mastodon_tags.splitlines() if t.strip()]

    @property
    def keywords(self):
        return [k.strip().lower() for k in self.gig_keywords.splitlines() if k.strip()]

    def clean(self):
        if not 0 <= self.send_hour_start < self.send_hour_end <= 24:
            raise ValidationError("Sending hours must satisfy: start < end, between 0 and 24.")

    def within_send_window(self, now: datetime.datetime) -> bool:
        """Is `now` (already in the business's local time) inside sending hours?"""
        if self.send_weekdays_only and now.weekday() >= 5:
            return False
        return self.send_hour_start <= now.hour < self.send_hour_end

    def lead_local_now(self, lead):
        if lead.market_id:
            return lead.market.local_now()
        try:
            return timezone.now().astimezone(ZoneInfo(self.home_timezone))
        except (ZoneInfoNotFoundError, ValueError):
            return timezone.localtime()

    @property
    def send_window_label(self):
        days = "weekdays" if self.send_weekdays_only else "every day"
        return f"{self.send_hour_start}:00–{self.send_hour_end}:00 in each business's own time zone, {days}"

    @staticmethod
    def focus_filter():
        """Leads the agent works on: those in enabled markets, gig posts, and ones you added by hand."""
        return models.Q(source="manual") | models.Q(market__enabled=True) | models.Q(kind="gig")


class Lead(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "New"
        QUALIFIED = "qualified", "Qualified"
        DISQUALIFIED = "disqualified", "Not a fit"
        NO_EMAIL = "no_email", "No email (DM / call)"
        DRAFTED = "drafted", "Email drafted"
        CONTACTED = "contacted", "Contacted"
        REPLIED = "replied", "Replied"
        INTERESTED = "interested", "Interested"
        NOT_INTERESTED = "not_interested", "Not interested"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"
        BOUNCED = "bounced", "Bounced"
        CLOSED = "closed", "Closed / won"

    name = models.CharField(max_length=255)
    category = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=100, blank=True)
    address = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=60, blank=True)
    website = models.URLField(max_length=500, blank=True)
    email = models.EmailField(blank=True)

    market = models.ForeignKey(Market, null=True, blank=True, on_delete=models.SET_NULL, related_name="leads")
    kind = models.CharField(max_length=10, default="business", choices=[("business", "Business"), ("gig", "Gig post")], db_index=True)
    post_url = models.URLField(max_length=500, blank=True)
    post_title = models.CharField(max_length=300, blank=True)
    post_text = models.TextField(blank=True)
    post_author = models.CharField(max_length=100, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    budget = models.CharField(max_length=120, blank=True)
    source = models.CharField(max_length=30, default="manual")
    source_id = models.CharField(max_length=100, blank=True, db_index=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW, db_index=True)
    fit_score = models.PositiveIntegerField(null=True, blank=True)
    fit_reason = models.TextField(blank=True)
    research_summary = models.TextField(blank=True)
    hook = models.TextField(blank=True, help_text="The specific detail the email opens with.")
    site_signals = models.JSONField(default=list, blank=True)
    researched_at = models.DateTimeField(null=True, blank=True)

    socials = models.JSONField(default=dict, blank=True, help_text="Social accounts, e.g. {'instagram': 'https://...'}")
    whatsapp_confirmed = models.BooleanField(default=False, help_text="Number came from a WhatsApp link or listing.")
    whatsapp_message = models.TextField(blank=True, help_text="Ready-to-send direct message written by Claude.")
    whatsapp_sent_at = models.DateTimeField(null=True, blank=True)
    dm_channel = models.CharField(max_length=20, blank=True, help_text="Where you sent the direct message.")

    contacts_checked_at = models.DateTimeField(null=True, blank=True, help_text="When the free website scan ran.")
    email_found_at = models.DateTimeField(null=True, blank=True, db_index=True)
    scan_cache = models.JSONField(default=dict, blank=True)

    followups_sent = models.PositiveIntegerField(default=0)
    last_contacted_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name

    @property
    def calling_code(self):
        return self.market.calling_code if self.market_id else "234"

    @property
    def phone_kind(self):
        from .contacts import phone_kind
        return phone_kind(self.phone, self.calling_code, self.whatsapp_confirmed)

    @property
    def whatsapp_number(self):
        """Digits for wa.me links (international format, no +). Empty for landlines."""
        from .contacts import whatsapp_digits
        if not self.phone or self.phone_kind == "landline":
            return ""
        return whatsapp_digits(self.phone, self.calling_code)

    @property
    def whatsapp_link(self):
        from .contacts import whatsapp_url
        return whatsapp_url(self.whatsapp_number)

    @property
    def whatsapp_send_link(self):
        from .contacts import whatsapp_url
        return whatsapp_url(self.whatsapp_number, self.whatsapp_message)

    @property
    def dm_channels(self):
        """[(network, label, profile_url, dm_url)] in the order worth trying."""
        from .contacts import CHANNEL_ORDER, LABELS, dm_link
        socials = self.socials or {}
        return [(n, LABELS[n], socials[n], dm_link(n, socials[n])) for n in CHANNEL_ORDER if socials.get(n)]

    @property
    def reach_rank(self):
        """Lower is better: confirmed WhatsApp, then social DMs, mobile, unknown phone, landline, nothing."""
        kind = self.phone_kind
        if kind == "whatsapp":
            return 0
        if self.socials:
            return 1
        return {"mobile": 2, "unknown": 3, "landline": 4}.get(kind, 5)

    @property
    def is_stopped(self):
        return self.status in {
            self.Status.UNSUBSCRIBED,
            self.Status.BOUNCED,
            self.Status.NOT_INTERESTED,
            self.Status.DISQUALIFIED,
            self.Status.CLOSED,
        }


class Message(models.Model):
    class Direction(models.TextChoices):
        OUT = "out", "Outgoing"
        IN = "in", "Incoming"

    class Kind(models.TextChoices):
        INITIAL = "initial", "First email"
        FOLLOWUP = "followup", "Follow-up"
        REPLY = "reply", "Reply"
        INBOUND = "inbound", "Their message"

    class Status(models.TextChoices):
        DRAFT = "draft", "Waiting for approval"
        APPROVED = "approved", "Approved (queued)"
        SENT = "sent", "Sent"
        REJECTED = "rejected", "Rejected"
        FAILED = "failed", "Failed"
        RECEIVED = "received", "Received"

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="messages")
    direction = models.CharField(max_length=3, choices=Direction.choices)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, db_index=True)
    subject = models.CharField(max_length=300, blank=True)
    body = models.TextField(blank=True)

    message_id = models.CharField(max_length=300, blank=True, db_index=True)
    in_reply_to = models.CharField(max_length=300, blank=True)
    classification = models.CharField(max_length=30, blank=True)
    summary = models.TextField(blank=True)
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} — {self.lead}"


class ApiUsage(models.Model):
    """Calls made to metered services, per calendar month."""

    name = models.CharField(max_length=40)
    month = models.CharField(max_length=7)  # e.g. 2026-11
    count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("name", "month")]

    @staticmethod
    def this_month():
        return timezone.now().strftime("%Y-%m")

    @classmethod
    def used(cls, name):
        row = cls.objects.filter(name=name, month=cls.this_month()).first()
        return row.count if row else 0

    @classmethod
    def add(cls, name, n=1):
        row, _ = cls.objects.get_or_create(name=name, month=cls.this_month())
        cls.objects.filter(pk=row.pk).update(count=models.F("count") + n)


class Suppression(models.Model):
    """Addresses that must never be emailed again."""

    email = models.EmailField(unique=True)
    reason = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.email

    @classmethod
    def blocked(cls, email):
        return bool(email) and cls.objects.filter(email__iexact=email).exists()


class Activity(models.Model):
    class Level(models.TextChoices):
        INFO = "info", "Info"
        SUCCESS = "success", "Success"
        WARNING = "warning", "Warning"
        ERROR = "error", "Error"

    level = models.CharField(max_length=10, choices=Level.choices, default=Level.INFO)
    text = models.TextField()
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "Activity"

    def __str__(self):
        return self.text[:80]


def log(text, level=Activity.Level.INFO, lead=None):
    return Activity.objects.create(text=text, level=level, lead=lead)
