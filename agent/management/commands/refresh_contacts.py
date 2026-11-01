from django.core.management.base import BaseCommand
from django.db.models import Q

from agent.models import Lead, log
from agent.services.research import apply_contacts, gather


class Command(BaseCommand):
    help = "Re-check websites for emails, phone/WhatsApp numbers and social accounts. Free: doesn't use Claude."

    def handle(self, *args, **options):
        leads = Lead.objects.exclude(website="").exclude(
            status__in=[Lead.Status.UNSUBSCRIBED, Lead.Status.BOUNCED, Lead.Status.DISQUALIFIED]
        ).filter(Q(email="") | Q(phone="") | Q(socials={}) | Q(whatsapp_confirmed=False))
        total = leads.count()
        updated = 0
        for lead in leads:
            facts = gather(lead)
            changed = apply_contacts(lead, facts)
            if not changed:
                continue
            if lead.status == Lead.Status.NO_EMAIL and lead.email:
                lead.status = Lead.Status.QUALIFIED  # the agent will draft an email next check-in
            lead.save()
            updated += 1
            log(f"Found {', '.join(changed)} for {lead.name}.", "success", lead=lead)
            self.stdout.write(f"  {lead.name}: found {', '.join(changed)}")
        self.stdout.write(self.style.SUCCESS(f"Checked {total} leads, updated {updated}."))
