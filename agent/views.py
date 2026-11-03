from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Case, Count, F, IntegerField, Q, Value, When
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import ConfigForm, LeadContactForm, LeadForm
from .models import Activity, AgentConfig, Lead, Market, Message, Suppression, log
from .services import gigs as gig_service, research, writer
from .services.llm import LLMError
from .services.mailer import sent_today


def _nav_counts():
    return {
        "pending_count": Message.objects.filter(status=Message.Status.DRAFT).count(),
        "gig_count": Lead.objects.filter(kind="gig", status=Lead.Status.QUALIFIED, whatsapp_sent_at__isnull=True).count(),
    }


@login_required
def dashboard(request):
    config = AgentConfig.get()
    by_status = dict(Lead.objects.values_list("status").annotate(n=Count("id")))
    funnel = [
        ("Found", Lead.objects.count()),
        ("Researched", Lead.objects.exclude(researched_at=None).count()),
        ("Qualified", sum(by_status.get(s, 0) for s in ["qualified", "drafted", "contacted", "replied", "interested", "closed", "no_email"])),
        ("Contacted", Lead.objects.filter(messages__status=Message.Status.SENT).distinct().count()),
        ("Replied", Lead.objects.filter(messages__direction=Message.Direction.IN).distinct().count()),
        ("Interested", by_status.get("interested", 0) + by_status.get("closed", 0)),
    ]
    top = max([n for _, n in funnel] + [1])
    context = {
        "config": config,
        "funnel": [(label, n, round(n * 100 / top)) for label, n in funnel],
        "sent_today": sent_today(),
        "pending": Message.objects.filter(status=Message.Status.DRAFT).select_related("lead")[:5],
        "approved_count": Message.objects.filter(status=Message.Status.APPROVED).count(),
        "hot_leads": Lead.objects.filter(status__in=[Lead.Status.INTERESTED, Lead.Status.REPLIED])[:6],
        "call_list_count": by_status.get("no_email", 0),
        "emails_today": research.emails_found_today(),
        "per_search": research.average_emails_per_search(),
        "activity": Activity.objects.select_related("lead")[:12],
        **_nav_counts(),
    }
    return render(request, "agent/dashboard.html", context)


@login_required
def outbox(request):
    drafts = Message.objects.filter(status=Message.Status.DRAFT).select_related("lead").order_by("created_at")
    approved = Message.objects.filter(status=Message.Status.APPROVED).select_related("lead").order_by("created_at")
    config = AgentConfig.get()
    context = {
        "drafts": drafts,
        "approved": approved,
        "config": config,
        "address_missing": not config.postal_address.strip()
        and Market.objects.filter(enabled=True, requires_postal_address=True).exists(),
        **_nav_counts(),
    }
    return render(request, "agent/outbox.html", context)


@login_required
@require_POST
def message_action(request, pk):
    msg = get_object_or_404(Message, pk=pk, direction=Message.Direction.OUT)
    action = request.POST.get("action")
    if msg.status not in (Message.Status.DRAFT, Message.Status.APPROVED):
        flash.error(request, "That email can't be changed any more.")
        return redirect(request.POST.get("next") or "outbox")

    if action in ("save", "approve"):
        msg.subject = request.POST.get("subject", msg.subject).strip()[:300]
        msg.body = request.POST.get("body", msg.body).replace("\r\n", "\n").strip()
    if action == "approve":
        msg.status = Message.Status.APPROVED
        msg.save()
        log(f"You approved the email to {msg.lead.name}; it goes out on the next check-in.", "success", lead=msg.lead)
        flash.success(request, f"Approved — the email to {msg.lead.name} will be sent on the next check-in.")
    elif action == "save":
        msg.save()
        flash.success(request, "Saved.")
    elif action == "unapprove":
        msg.status = Message.Status.DRAFT
        msg.save(update_fields=["status"])
        flash.info(request, "Moved back to drafts.")
    elif action == "reject":
        msg.status = Message.Status.REJECTED
        msg.save(update_fields=["status"])
        if msg.kind == Message.Kind.INITIAL and msg.lead.status == Lead.Status.DRAFTED:
            msg.lead.status = Lead.Status.DISQUALIFIED
            msg.lead.save(update_fields=["status", "updated_at"])
        flash.info(request, f"Rejected. {msg.lead.name} won't be emailed with this draft.")
    elif action == "redraft":
        try:
            writer.redraft(msg)
            flash.success(request, "Claude wrote a fresh draft.")
        except LLMError as exc:
            msg.status = Message.Status.DRAFT
            msg.save(update_fields=["status"])
            flash.error(request, str(exc))
    return redirect(request.POST.get("next") or "outbox")


@login_required
@require_POST
def approve_all(request):
    count = Message.objects.filter(status=Message.Status.DRAFT).update(status=Message.Status.APPROVED)
    log(f"You approved {count} drafts at once.", "success")
    flash.success(request, f"Approved {count} emails.")
    return redirect("outbox")


@login_required
def leads(request):
    qs = Lead.objects.annotate(msg_count=Count("messages"))
    status = request.GET.get("status", "")
    q = request.GET.get("q", "").strip()
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(city__icontains=q) | Q(category__icontains=q) | Q(market__name__iexact=q))
    page = Paginator(qs.order_by("-updated_at"), 30).get_page(request.GET.get("page"))
    counts = dict(Lead.objects.values_list("status").annotate(n=Count("id")))
    statuses = [(value, label, counts.get(value, 0)) for value, label in Lead.Status.choices]
    return render(
        request,
        "agent/leads.html",
        {"page": page, "statuses": statuses, "status": status, "q": q, "total": Lead.objects.count(), **_nav_counts()},
    )


@login_required
def lead_add(request):
    form = LeadForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        lead = form.save(commit=False)
        lead.source = "manual"
        lead.save()
        log(f"You added {lead.name} by hand.", lead=lead)
        flash.success(request, f"Added {lead.name}. The agent will research it on the next check-in.")
        return redirect("lead_detail", pk=lead.pk)
    return render(request, "agent/lead_form.html", {"form": form, **_nav_counts()})


@login_required
def lead_detail(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    form = LeadContactForm(request.POST or None, instance=lead)
    if request.method == "POST" and form.is_valid():
        lead = form.save(commit=False)
        if lead.email and "email" in form.changed_data:
            lead.email_found_at = timezone.now()
        lead.save()
        if lead.status == Lead.Status.NO_EMAIL and lead.email:
            lead.status = Lead.Status.QUALIFIED
            lead.save(update_fields=["status", "updated_at"])
        flash.success(request, "Saved.")
        return redirect("lead_detail", pk=lead.pk)
    return render(
        request,
        "agent/lead_detail.html",
        {
            "lead": lead,
            "form": form,
            "thread": lead.messages.exclude(status=Message.Status.REJECTED).order_by("created_at"),
            "statuses": Lead.Status.choices,
            "suppressed": Suppression.blocked(lead.email),
            "social_fields": [("instagram", "Instagram"), ("facebook", "Facebook"), ("linkedin", "LinkedIn"), ("x", "X"), ("tiktok", "TikTok")],
            **_nav_counts(),
        },
    )


@login_required
@require_POST
def lead_action(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    action = request.POST.get("action")
    try:
        if action == "research":
            if lead.kind == "gig":
                gig_service.research_gig(lead)
            else:
                research.research_lead(lead)
            flash.success(request, f"Research done: fit {lead.fit_score}/100.")
        elif action == "research_and_draft":
            research.research_lead(lead)
            if lead.status == Lead.Status.QUALIFIED and lead.email and not Suppression.blocked(lead.email):
                writer.draft_initial(lead)
                flash.success(request, f"{lead.name}: fit {lead.fit_score}/100. Email drafted: it's in Approve emails.")
            else:
                flash.info(request, f"{lead.name}: fit {lead.fit_score}/100, not drafted. {lead.fit_reason}")
        elif action == "draft":
            if not lead.email:
                flash.error(request, "Add an email address first.")
            elif Suppression.blocked(lead.email):
                flash.error(request, "This address is on the do-not-contact list.")
            else:
                writer.draft_initial(lead)
                flash.success(request, "Draft ready in the approval queue.")
        elif action == "status":
            new = request.POST.get("status")
            if new in Lead.Status.values:
                lead.status = new
                lead.save(update_fields=["status", "updated_at"])
                flash.success(request, f"Status set to {lead.get_status_display()}.")
        elif action == "suppress":
            if lead.email:
                Suppression.objects.get_or_create(email=lead.email.lower(), defaults={"reason": "manual"})
            lead.status = Lead.Status.UNSUBSCRIBED
            lead.save(update_fields=["status", "updated_at"])
            lead.messages.filter(status__in=[Message.Status.DRAFT, Message.Status.APPROVED]).update(
                status=Message.Status.REJECTED
            )
            flash.info(request, "Added to the do-not-contact list.")
        elif action == "whatsapp_write":
            if lead.kind == "gig":
                gig_service.research_gig(lead)
            else:
                writer.draft_whatsapp(lead)
            flash.success(request, f"New message written for {lead.name}.")
        elif action == "whatsapp_save":
            lead.whatsapp_message = request.POST.get("whatsapp_message", "").replace("\r\n", "\n").strip()[:1500]
            lead.save(update_fields=["whatsapp_message", "updated_at"])
            if request.headers.get("x-requested-with") == "fetch":
                return JsonResponse({"ok": True})
            flash.success(request, "Message saved.")
        elif action == "whatsapp_sent":
            channel = request.POST.get("channel", "")[:20] or "whatsapp"
            lead.whatsapp_sent_at = timezone.now()
            lead.last_contacted_at = lead.whatsapp_sent_at
            lead.dm_channel = channel
            lead.save(update_fields=["whatsapp_sent_at", "last_contacted_at", "dm_channel", "updated_at"])
            label = {"whatsapp": "WhatsApp", "call": "phone"}.get(channel, channel.title())
            log(f"You messaged {lead.name} on {label}.", "success", lead=lead)
            flash.success(request, f"Marked {lead.name} as messaged on {label}.")
        elif action == "gig_email":
            if not lead.email or Suppression.blocked(lead.email):
                flash.error(request, "No usable email address in this post.")
            else:
                config = AgentConfig.get()
                address = f"\n{config.postal_address.strip()}" if config.postal_address.strip() else ""
                Message.objects.create(
                    lead=lead,
                    direction=Message.Direction.OUT,
                    kind=Message.Kind.REPLY,  # they asked to be contacted: sent straight after approval
                    status=Message.Status.DRAFT,
                    subject=("Re: " + (lead.post_title or "your post"))[:300],
                    body=f"{lead.whatsapp_message.strip()}\n\n{config.signature.strip()}{address}",
                )
                flash.success(request, "Email added to Approve emails. Approve it there to send.")
        elif action == "socials_save":
            from .contacts import SOCIAL_PATTERNS, social_url
            socials = {}
            for network in SOCIAL_PATTERNS:
                url = social_url(network, request.POST.get(f"social_{network}", ""))
                if url:
                    socials[network] = url
            lead.socials = socials
            lead.save(update_fields=["socials", "updated_at"])
            flash.success(request, "Social accounts saved.")
        elif action == "whatsapp_unsent":
            lead.whatsapp_sent_at = None
            lead.save(update_fields=["whatsapp_sent_at", "updated_at"])
    except LLMError as exc:
        flash.error(request, str(exc))
    if request.POST.get("next", "").startswith("/emails/"):
        return redirect(request.POST["next"])
    if request.POST.get("next") == "call_list":
        return redirect(f"{reverse('call_list')}#lead-{lead.pk}")
    if request.POST.get("next") == "gigs":
        return redirect(f"{reverse('gigs')}#lead-{lead.pk}")
    return redirect("lead_detail", pk=lead.pk)


@login_required
def call_list(request):
    leads_qs = list(Lead.objects.filter(status=Lead.Status.NO_EMAIL).select_related("market"))
    # Unsent first, then the easiest to reach (confirmed WhatsApp, social DMs, mobile...), then best fit.
    leads_qs.sort(key=lambda l: (l.whatsapp_sent_at is not None, l.reach_rank, -(l.fit_score or 0)))
    counts = {
        "whatsapp": sum(1 for l in leads_qs if l.phone_kind == "whatsapp"),
        "social": sum(1 for l in leads_qs if l.socials),
        "landline": sum(1 for l in leads_qs if l.phone_kind == "landline" and not l.socials),
        "sent": sum(1 for l in leads_qs if l.whatsapp_sent_at),
    }
    return render(request, "agent/call_list.html", {"leads": leads_qs, "counts": counts, **_nav_counts()})


@login_required
def settings_view(request):
    config = AgentConfig.get()
    form = ConfigForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        form.save()
        flash.success(request, "Settings saved.")
        return redirect("settings")
    groups = [
        ("You & your offer", ["agent_enabled", "sender_name", "company_name", "company_website", "offer",
                              "ideal_customer", "writing_rules", "signature", "opt_out_line"]),
        ("Sectors to search", ["target_categories", "foursquare_monthly_limit", "daily_email_target",
                               "searches_when_behind", "min_emails_per_search", "search_size", "scan_per_tick", "only_contactable", "skip_poor_sectors", "max_sector_share",
                               "research_dm_leads"]),
        ("Gig postings", ["gigs_enabled", "gig_subreddits", "hn_gigs_enabled", "remotive_enabled",
                          "mastodon_enabled", "mastodon_tags", "mastodon_instance", "gig_keywords",
                          "gig_max_age_days", "gig_check_minutes"]),
        ("Sending & compliance", ["postal_address", "send_hour_start", "send_hour_end", "send_weekdays_only",
                                  "home_timezone"]),
        ("Limits", ["daily_send_limit", "sends_per_tick", "research_per_tick", "drafts_per_tick",
                    "min_fit_score", "followup_days", "max_followups", "min_new_leads"]),
    ]
    sections = [(title, [form[name] for name in names if name in form.fields]) for title, names in groups]
    return render(request, "agent/settings.html", {"form": form, "sections": sections, **_nav_counts()})


@login_required
def activity(request):
    page = Paginator(Activity.objects.select_related("lead"), 50).get_page(request.GET.get("page"))
    return render(request, "agent/activity.html", {"page": page, **_nav_counts()})


@login_required
def markets(request):
    if request.method == "POST" and request.POST.get("all") in ("on", "off"):
        turn_on = request.POST["all"] == "on"
        Market.objects.update(enabled=turn_on)
        log(f"{'Enabled' if turn_on else 'Paused'} all markets.", "success" if turn_on else "info")
        flash.success(request, f"All countries {'enabled' if turn_on else 'paused'}. Check each country's rules note.")
        return redirect("markets")
    if request.method == "POST":
        market = get_object_or_404(Market, pk=request.POST.get("market"))
        market.enabled = request.POST.get("enabled") == "1"
        market.save(update_fields=["enabled"])
        log(f"{'Enabled' if market.enabled else 'Paused'} market: {market.name}.", "success" if market.enabled else "info")
        flash.success(request, f"{market.name} {'enabled' if market.enabled else 'paused'}.")
        return redirect("markets")
    rows = Market.objects.order_by("-enabled", "sort").annotate(
        lead_count=Count("leads"),
        contacted=Count("leads", filter=Q(leads__status__in=["contacted", "replied", "interested", "closed", "not_interested"])),
        replied=Count("leads", filter=Q(leads__status__in=["replied", "interested", "closed"])),
    )
    now = {m.pk: m.local_now() for m in rows}
    config = AgentConfig.get()
    table = [(m, now[m.pk].strftime("%a %H:%M"), config.within_send_window(now[m.pk])) for m in rows]
    on_count = sum(1 for m, _, _ in table if m.enabled)
    return render(request, "agent/markets.html", {"table": table, "config": config, "on_count": on_count, **_nav_counts()})


@login_required
def gigs_view(request):
    from .services.gigs import find_gigs

    config = AgentConfig.get()
    if request.method == "POST" and request.POST.get("action") == "check_now":
        try:
            found = find_gigs(config, force=True)
            flash.success(request, f"Checked for gigs: {found} new.")
        except Exception as exc:  # network problems shouldn't break the page
            flash.error(request, f"Couldn't check for gigs: {exc}")
        return redirect("gigs")
    show_all = request.GET.get("all") == "1"
    qs = Lead.objects.filter(kind="gig")
    counts = {
        "waiting": qs.filter(status=Lead.Status.NEW).count(),
        "good": qs.filter(status=Lead.Status.QUALIFIED, whatsapp_sent_at__isnull=True).count(),
        "replied": qs.filter(whatsapp_sent_at__isnull=False).count(),
        "skipped": qs.filter(status=Lead.Status.DISQUALIFIED).count(),
    }
    if not show_all:
        qs = qs.exclude(status__in=[Lead.Status.DISQUALIFIED, Lead.Status.NEW])
    leads_qs = sorted(qs, key=lambda l: (l.whatsapp_sent_at is not None, -(l.fit_score or 0)))
    return render(request, "agent/gigs.html", {
        "leads": leads_qs, "counts": counts, "show_all": show_all, "config": config, **_nav_counts(),
    })


STAGES = [
    ("waiting", "Waiting for Claude"),
    ("ready", "Good fit, draft coming"),
    ("drafted", "Drafted: approve it"),
    ("sent", "Sent"),
    ("replied", "Replied"),
    ("not_fit", "Not a fit"),
    ("stopped", "Stopped"),
]
STAGE_STATUSES = {
    "waiting": [Lead.Status.NEW],
    "ready": [Lead.Status.QUALIFIED],
    "drafted": [Lead.Status.DRAFTED],
    "sent": [Lead.Status.CONTACTED],
    "replied": [Lead.Status.REPLIED, Lead.Status.INTERESTED, Lead.Status.CLOSED],
    "not_fit": [Lead.Status.DISQUALIFIED, Lead.Status.NO_EMAIL],
    "stopped": [Lead.Status.NOT_INTERESTED, Lead.Status.UNSUBSCRIBED, Lead.Status.BOUNCED],
}
STATUS_STAGE = {status: stage for stage, statuses in STAGE_STATUSES.items() for status in statuses}


@login_required
def emails_found(request):
    """Every business email NexReach has found, and which step each one is at."""
    from .services import loop

    if request.method == "POST" and request.POST.get("action") == "process_next":
        config = AgentConfig.get()
        try:
            research.mark_chains(config)
            researched = research.research_many(list(research.research_queue(config).filter(kind="business")[:16]), config)
            drafted = loop.draft_batch(config) if researched else 0
            drafted += loop.draft_batch(config) if drafted >= config.drafts_per_tick else 0
            flash.success(request, f"Checked {researched} businesses with Claude and drafted {drafted} emails.")
        except LLMError as exc:
            flash.error(request, f"Claude couldn't run: {exc}")
        return redirect(request.get_full_path())

    day = request.GET.get("day", "today")
    stage = request.GET.get("stage", "")
    qs = Lead.objects.filter(kind="business").exclude(email="").select_related("market")
    if day == "today":
        start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        qs = qs.filter(email_found_at__gte=start)
    counts = dict(qs.values_list("status").annotate(n=Count("id")))
    stage_counts = [(key, label, sum(counts.get(s, 0) for s in STAGE_STATUSES[key])) for key, label in STAGES]
    if stage in STAGE_STATUSES:
        qs = qs.filter(status__in=STAGE_STATUSES[stage])
    page = Paginator(qs.order_by(F("email_found_at").desc(nulls_last=True), "-pk"), 50).get_page(request.GET.get("page"))
    for lead in page:
        lead.stage = STATUS_STAGE.get(lead.status, "")
    return render(request, "agent/emails_found.html", {
        "page": page, "day": day, "stage": stage, "stage_counts": stage_counts,
        "total": sum(n for _, _, n in stage_counts), **_nav_counts(),
    })
