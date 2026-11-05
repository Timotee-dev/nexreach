"""Sends APPROVED emails through Gmail SMTP (App Password)."""
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from ..models import AgentConfig, Lead, Message, Suppression, log


class MailConfigError(Exception):
    pass


class InboxUnavailable(Exception):
    """Gmail refused for reasons about the INBOX (daily limit, dropped connection), not about this email."""


_blocked_until: dict = {}
BLOCK_HOURS = 4


def is_inbox_problem(exc: Exception) -> bool:
    text = str(exc).lower()
    code = getattr(exc, "smtp_code", 0) or 0
    return (
        isinstance(exc, smtplib.SMTPServerDisconnected)
        or code in (421, 450, 451, 452, 454)
        or any(w in text for w in ("5.4.5", "sending limit", "daily user", "try again later", "connect() first",
                                   "unexpectedly closed", "rate limit"))
    )


def check_config():
    if not settings.GMAIL_ADDRESS or not settings.GMAIL_APP_PASSWORD:
        raise MailConfigError("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in your .env file.")


def accounts() -> list[tuple[str, str]]:
    """Your sending inboxes: the main one first, then any GMAIL_ADDRESS_2.. from .env."""
    main = [(settings.GMAIL_ADDRESS, settings.GMAIL_APP_PASSWORD)] if settings.GMAIL_ADDRESS else []
    extra = [a for a in getattr(settings, "EXTRA_GMAIL_ACCOUNTS", []) if a[0].lower() != settings.GMAIL_ADDRESS.lower()]
    return main + extra


def _day_start():
    return timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)


def sent_today_by(address: str) -> int:
    qs = Message.objects.filter(direction=Message.Direction.OUT, status=Message.Status.SENT, sent_at__gte=_day_start())
    qs = qs.exclude(kind=Message.Kind.REPLY)
    if address.lower() == settings.GMAIL_ADDRESS.lower():
        return qs.filter(sender__in=["", address]).count()  # older emails have no sender recorded
    return qs.filter(sender__iexact=address).count()


def limit_for(address: str, config: AgentConfig) -> int:
    """Main inbox: your daily limit. Extra inboxes warm up: 10 on day one, +3 a day, up to the limit."""
    if address.lower() == settings.GMAIL_ADDRESS.lower():
        return config.daily_send_limit
    first = (Message.objects.filter(direction=Message.Direction.OUT, status=Message.Status.SENT, sender__iexact=address)
             .order_by("sent_at").values_list("sent_at", flat=True).first())
    days = (timezone.localdate() - timezone.localtime(first).date()).days if first else 0
    return min(config.daily_send_limit, settings.WARMUP_START + settings.WARMUP_STEP * days)


def capacity(config: AgentConfig) -> int:
    return sum(limit_for(a, config) for a, _ in accounts()) or config.daily_send_limit


def thread_sender(lead: Lead) -> str:
    """Follow-ups and replies go from the inbox that first wrote to them."""
    prev = (lead.messages.filter(direction=Message.Direction.OUT, status=Message.Status.SENT)
            .order_by("sent_at").values_list("sender", flat=True).first())
    if prev is None:
        return ""
    return prev or settings.GMAIL_ADDRESS


def _log_once(text: str, minutes: int = 120, level: str = "warning"):
    """Warn without filling Activity with the same line every check-in."""
    from datetime import timedelta
    from ..models import Activity
    if not Activity.objects.filter(text=text, created_at__gte=timezone.now() - timedelta(minutes=minutes)).exists():
        log(text, level)


def sent_today() -> int:
    return Message.objects.filter(direction=Message.Direction.OUT, status=Message.Status.SENT, sent_at__gte=_day_start()).count()


def build(message: Message, config: AgentConfig, address: str = "") -> EmailMessage:
    address = address or settings.GMAIL_ADDRESS
    domain = address.split("@")[-1] or "localhost"
    em = EmailMessage()
    em["From"] = formataddr((f"{config.sender_name} from {config.company_name}", address))
    em["To"] = message.lead.email
    em["Subject"] = message.subject
    em["Message-ID"] = make_msgid(domain=domain)
    # Lets Gmail/Outlook show a one-click "Unsubscribe" — good for deliverability and required by bulk-sender rules.
    em["List-Unsubscribe"] = f"<mailto:{address}?subject=unsubscribe>"
    if message.in_reply_to:
        em["In-Reply-To"] = message.in_reply_to
        em["References"] = message.in_reply_to
    em.set_content(message.body)
    return em


def send_message(message: Message, config: AgentConfig | None = None, smtp=None, address: str = "") -> bool:
    config = config or AgentConfig.get()
    lead = message.lead
    if message.status != Message.Status.APPROVED:
        return False
    if not lead.email:
        message.status, message.error = Message.Status.FAILED, "Lead has no email address."
        message.save(update_fields=["status", "error"])
        return False
    if Suppression.blocked(lead.email) or lead.status in {Lead.Status.UNSUBSCRIBED, Lead.Status.BOUNCED}:
        message.status, message.error = Message.Status.REJECTED, "Address is on the do-not-contact list."
        message.save(update_fields=["status", "error"])
        log(f"Skipped {lead.name}: on the do-not-contact list.", "warning", lead=lead)
        return False

    address = address or settings.GMAIL_ADDRESS
    em = build(message, config, address)
    try:
        smtp.send_message(em)
    except smtplib.SMTPException as exc:
        if is_inbox_problem(exc):
            raise InboxUnavailable(str(exc)) from exc  # the email stays approved and is tried again later
        message.status, message.error = Message.Status.FAILED, str(exc)[:1000]
        message.save(update_fields=["status", "error"])
        log(f"Sending to {lead.name} failed: {exc}", "error", lead=lead)
        return False

    now = timezone.now()
    message.status, message.message_id, message.sent_at, message.error = Message.Status.SENT, em["Message-ID"], now, ""
    message.sender = address
    message.save(update_fields=["status", "message_id", "sent_at", "error", "sender"])

    lead.last_contacted_at = now
    if message.kind == Message.Kind.FOLLOWUP:
        lead.followups_sent += 1
    if message.kind in (Message.Kind.INITIAL, Message.Kind.FOLLOWUP) and lead.status in {
        Lead.Status.DRAFTED, Lead.Status.QUALIFIED, Lead.Status.NEW, Lead.Status.CONTACTED
    }:
        lead.status = Lead.Status.CONTACTED
    lead.save()
    via = f" from {address}" if len(accounts()) > 1 else ""
    log(f"Sent {message.get_kind_display().lower()} to {lead.name} <{lead.email}>{via}.", "success", lead=lead)
    return True


def cold_hold_reason(message: Message, config: AgentConfig) -> str:
    """Why a cold email can't go out right now ('' if it can). Replies are never held."""
    if message.kind == Message.Kind.REPLY:
        return ""
    lead = message.lead
    if lead.market_id and lead.market.requires_postal_address and not config.postal_address.strip():
        return "postal_address"
    if not config.within_send_window(config.lead_local_now(lead)):
        return "outside_hours"
    return ""


def send_approved(config: AgentConfig | None = None) -> int:
    """Send approved emails. Cold emails go out only during each business's own working hours,
    within the daily limit; replies to people who wrote back go out straight away."""
    config = config or AgentConfig.get()
    queue = list(
        Message.objects.filter(direction=Message.Direction.OUT, status=Message.Status.APPROVED)
        .select_related("lead", "lead__market")
        .order_by("created_at")
    )
    if not queue:
        return 0
    ready = []
    for message in queue:
        reason = cold_hold_reason(message, config)
        if reason == "postal_address":
            _log_once("Cold emails to some countries are on hold: add your postal address in Settings (required by law).")
        elif not reason:
            ready.append(message)
    if not ready:
        return 0
    check_config()

    import contextlib

    sent = 0
    limit_hit = False
    with contextlib.ExitStack() as stack:
        # Log in to every inbox FIRST and plan only with the ones that work, so emails are never handed
        # to an inbox whose password is wrong.
        connections = {}
        for address, password in accounts():
            until = _blocked_until.get(address.lower())
            if until and until > timezone.now():
                continue  # resting after Gmail's daily limit
            try:
                smtp = stack.enter_context(smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=60))
                smtp.login(address, password)
                connections[address] = smtp
            except (smtplib.SMTPException, OSError) as exc:
                if address.lower() == settings.GMAIL_ADDRESS.lower():
                    raise
                _log_once(f"Can't send from {address}: Google rejected its App Password, so it is skipped. "
                          f"Fix GMAIL_APP_PASSWORD for it in .env, or remove its lines. ({str(exc)[:80]})", level="error")
        if not connections:
            return 0
        main = settings.GMAIL_ADDRESS if settings.GMAIL_ADDRESS in connections else next(iter(connections))
        room = {a: max(0, limit_for(a, config) - sent_today_by(a)) for a in connections}
        plan = {a: [] for a in connections}
        per_tick = {a: 0 for a in connections}
        for message in ready:
            is_cold = message.kind != Message.Kind.REPLY
            owner = thread_sender(message.lead)
            if owner not in plan:
                owner = ""  # that inbox is gone or not working right now
            if not is_cold:
                plan[owner or main].append(message)
                continue
            choices = [owner] if owner else sorted(plan, key=lambda a: -(room[a] - per_tick[a]))
            target = next((a for a in choices if per_tick[a] < room[a] and per_tick[a] < config.sends_per_tick), None)
            if target is None:
                limit_hit = limit_hit or all(per_tick[a] >= room[a] for a in plan)
                continue
            plan[target].append(message)
            per_tick[target] += 1
        for address, messages in plan.items():
            for message in messages:
                try:
                    if send_message(message, config, smtp=connections[address], address=address):
                        sent += 1
                except InboxUnavailable as exc:
                    resume = timezone.now() + timedelta(hours=BLOCK_HOURS)
                    _blocked_until[address.lower()] = resume
                    _log_once(
                        f"Gmail stopped {address} from sending ({str(exc)[:90]}). Emails stay approved and "
                        f"sending from it resumes around {timezone.localtime(resume):%H:%M}. "
                        f"This usually means its daily limit was reached: let it rest and lower your daily limit.",
                        level="error")
                    break  # stop using this inbox for now; nothing is marked failed
    if limit_hit:
        _log_once(f"Today's limit of {capacity(config)} cold emails reached; the rest wait for tomorrow.", level="info")
    return sent
