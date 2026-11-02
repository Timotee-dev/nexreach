from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from agent.models import Lead, log
from agent.services.research import apply_contacts, scan_many


class Command(BaseCommand):
    help = ("Re-scan websites of leads missing an email (or phone/socials) with the latest email finder. "
            "Free: doesn't use Claude.")

    def handle(self, *args, **options):
        leads = list(
            Lead.objects.filter(kind="business").exclude(website="")
            .exclude(status__in=[Lead.Status.UNSUBSCRIBED, Lead.Status.BOUNCED, Lead.Status.CLOSED])
            .filter(Q(email="") | Q(phone="") | Q(socials={}))
            .select_related("market")
        )
        self.stdout.write(f"Scanning {len(leads)} websites (8 at a time)...")
        emails = updated = 0
        for start in range(0, len(leads), 40):
            batch = leads[start:start + 40]
            for lead, facts in zip(batch, scan_many(batch)):
                changed = apply_contacts(lead, facts)
                lead.contacts_checked_at = lead.contacts_checked_at or timezone.now()
                if "email" in changed:
                    emails += 1
                    if lead.status == Lead.Status.NO_EMAIL:
                        lead.status = Lead.Status.QUALIFIED  # already a good fit: an email draft comes next
                    elif lead.status == Lead.Status.DISQUALIFIED and lead.fit_reason.startswith("No email, phone"):
                        lead.status = Lead.Status.NEW  # set aside only for lack of contact: research it now
                lead.save()
                if changed:
                    updated += 1
                    log(f"Found {', '.join(changed)} for {lead.name}.", "success", lead=lead)
                    self.stdout.write(f"  {lead.name}: found {', '.join(changed)}")
            self.stdout.write(f"  ...{min(start + 40, len(leads))}/{len(leads)} checked, {emails} new emails so far")
        self.stdout.write(self.style.SUCCESS(f"Done: {emails} new emails, {updated} leads updated."))
