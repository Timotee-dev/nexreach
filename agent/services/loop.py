"""One 'tick' of the agent: look at the pipeline and do what's needed next."""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from ..models import AgentConfig, Lead, Message, log
from . import discovery, gigs, inbox, mailer, research, writer
from .discovery import OverpassBusy
from .llm import LLMError, LLMLimit
from .mailer import MailConfigError


def _step(name, fn):
    try:
        return fn()
    except LLMLimit as exc:
        config = AgentConfig.get()
        config.claude_paused_until = exc.resets_at
        config.save(update_fields=["claude_paused_until"])
        when = timezone.localtime(exc.resets_at).strftime("%a %H:%M") if exc.resets_at else "later"
        log(f"{name}: Claude usage limit reached. Claude work pauses until {when}; finding emails carries on.", "error")
    except OverpassBusy as exc:
        log(f"{name}: {exc}", "warning")
    except (LLMError, MailConfigError) as exc:
        log(f"{name}: {exc}", "error")
    except Exception as exc:  # keep the loop alive no matter what
        log(f"{name} failed: {exc.__class__.__name__}: {exc}", "error")
    return 0


def queue_followups(config):
    cutoff = timezone.now() - timedelta(days=config.followup_days)
    due = (
        Lead.objects.filter(
            status=Lead.Status.CONTACTED,
            kind="business",
            last_contacted_at__lte=cutoff,
            followups_sent__lt=config.max_followups,
        )
        .exclude(messages__status__in=[Message.Status.DRAFT, Message.Status.APPROVED])
        .distinct()[: config.drafts_per_tick]
    )
    count = 0
    for lead in due:
        writer.draft_followup(lead, config)
        count += 1
    return count


def research_batch(config, limit: int | None = None):
    """Gigs one by one; businesses 8 per Claude call. Chains are set aside first (free)."""
    research.mark_chains(config)
    queue = list(research.research_queue(config)[: limit or config.research_per_tick])
    count = 0
    for lead in [l for l in queue if l.kind == "gig"]:
        gigs.research_gig(lead, config)
        count += 1
    businesses = [l for l in queue if l.kind != "gig"]
    if businesses:
        count += research.research_many(businesses, config)
    return count


def draft_batch(config):
    count = 0
    leads = (
        Lead.objects.filter(status=Lead.Status.QUALIFIED, kind="business")
        .filter(config.focus_filter())
        .exclude(email="")
        .order_by("-fit_score", "created_at")[: config.drafts_per_tick]
    )
    for lead in leads:
        writer.draft_initial(lead, config)
        count += 1
    return count


def whatsapp_batch(config):
    """Write a direct message for good-fit businesses with no email but a phone or social account."""
    if not config.research_dm_leads:
        return 0
    count = 0
    leads = (
        Lead.objects.filter(status=Lead.Status.NO_EMAIL, kind="business", whatsapp_message="", whatsapp_sent_at__isnull=True)
        .filter(Q(phone__gt="") | ~Q(socials={}))
        .filter(config.focus_filter())
        .order_by("-fit_score", "created_at")[: config.drafts_per_tick]
    )
    for lead in leads:
        writer.draft_whatsapp(lead, config)
        count += 1
    return count


def maybe_discover(config):
    """Keep searching while today's email target isn't met; several searches per check-in when behind."""
    waiting = Lead.objects.filter(status=Lead.Status.NEW, kind="business").filter(config.focus_filter())
    behind = research.emails_found_today() < config.daily_email_target
    if not behind and waiting.count() >= config.min_new_leads:
        return 0
    found = 0
    rounds = max(1, config.searches_when_behind) if behind else 1
    for _ in range(rounds):
        if waiting.filter(contacts_checked_at__isnull=True).count() >= config.scan_per_tick * 2:
            break  # enough queued for the next scans
        found += discovery.discover_next(config)
    return found


def claude_available(config=None) -> bool:
    """False while a Claude usage limit is in force (so we don't hammer it every check-in)."""
    config = config or AgentConfig.get()
    until = config.claude_paused_until
    if until and until > timezone.now():
        return False
    if until:
        config.claude_paused_until = None
        config.save(update_fields=["claude_paused_until"])
    return True


def tick(finding: bool = True) -> dict:
    """One check-in. With finding=False, searching and website scanning are left to the separate
    fast email finder (see services/finder.py) and this only does inbox, sending and Claude work."""
    config = AgentConfig.get()
    if not config.agent_enabled:
        return {"paused": True}
    claude_ok = claude_available(config)
    results = {
        "replies": _step("Checking inbox", inbox.check_inbox),
        "sent": _step("Sending approved emails", lambda: mailer.send_approved(config)),
    }
    if claude_ok:
        results["followups"] = _step("Drafting follow-ups", lambda: queue_followups(config))
    if finding:
        results["discovered"] = _step("Finding businesses", lambda: maybe_discover(config))
        results["emails_found"] = _step("Scanning websites for contacts", lambda: research.scan_contacts(config))
    if claude_ok:
        results["researched"] = _step("Researching leads", lambda: research_batch(config))
        if claude_available():
            results["drafted"] = _step("Drafting emails", lambda: draft_batch(config))
        if claude_available():
            results["messages"] = _step("Writing direct messages", lambda: whatsapp_batch(config))
    else:
        log(f"Claude usage limit: research and drafting wait until "
            f"{timezone.localtime(config.claude_paused_until).strftime('%a %H:%M')}. Emails are still being found.", "warning")
    results["gigs"] = _step("Finding gigs", lambda: gigs.find_gigs(config))
    summary = ", ".join(f"{k} {v}" for k, v in results.items() if v)
    log(f"Agent check-in done — {summary or 'nothing needed doing'}.")
    return results
