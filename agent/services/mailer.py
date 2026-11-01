"""Sends APPROVED emails through Gmail SMTP (App Password)."""
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from django.conf import settings
from django.utils import timezone

from ..models import AgentConfig, Lead, Message, Suppression, log


class MailConfigError(Exception):
    pass


def check_config():
    if not settings.GMAIL_ADDRESS or not settings.GMAIL_APP_PASSWORD:
        raise MailConfigError("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in your .env file.")


def _log_once(text: str, minutes: int = 120, level: str = "warning"):
    """Warn without filling Activity with the same line every check-in."""
    from datetime import timedelta
    from ..models import Activity
    if not Activity.objects.filter(text=text, created_at__gte=timezone.now() - timedelta(minutes=minutes)).exists():
        log(text, level)


def sent_today() -> int:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return Message.objects.filter(direction=Message.Direction.OUT, status=Message.Status.SENT, sent_at__gte=start).count()


def build(message: Message, config: AgentConfig) -> EmailMessage:
    domain = settings.GMAIL_ADDRESS.split("@")[-1] or "localhost"
    em = EmailMessage()
    em["From"] = formataddr((f"{config.sender_name} from {config.company_name}", settings.GMAIL_ADDRESS))
    em["To"] = message.lead.email
    em["Subject"] = message.subject
    em["Message-ID"] = make_msgid(domain=domain)
    # Lets Gmail/Outlook show a one-click "Unsubscribe" — good for deliverability and required by bulk-sender rules.
    em["List-Unsubscribe"] = f"<mailto:{settings.GMAIL_ADDRESS}?subject=unsubscribe>"
    if message.in_reply_to:
        em["In-Reply-To"] = message.in_reply_to
        em["References"] = message.in_reply_to
    em.set_content(message.body)
    return em


def send_message(message: Message, config: AgentConfig | None = None, smtp=None) -> bool:
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

    em = build(message, config)
    try:
        smtp.send_message(em)
    except smtplib.SMTPException as exc:
        message.status, message.error = Message.Status.FAILED, str(exc)[:1000]
        message.save(update_fields=["status", "error"])
        log(f"Sending to {lead.name} failed: {exc}", "error", lead=lead)
        return False

    now = timezone.now()
    message.status, message.message_id, message.sent_at, message.error = Message.Status.SENT, em["Message-ID"], now, ""
    message.save(update_fields=["status", "message_id", "sent_at", "error"])

    lead.last_contacted_at = now
    if message.kind == Message.Kind.FOLLOWUP:
        lead.followups_sent += 1
    if message.kind in (Message.Kind.INITIAL, Message.Kind.FOLLOWUP) and lead.status in {
        Lead.Status.DRAFTED, Lead.Status.QUALIFIED, Lead.Status.NEW, Lead.Status.CONTACTED
    }:
        lead.status = Lead.Status.CONTACTED
    lead.save()
    log(f"Sent {message.get_kind_display().lower()} to {lead.name} <{lead.email}>.", "success", lead=lead)
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

    room = max(0, config.daily_send_limit - sent_today())
    sent = 0
    cold_sent = 0
    limit_hit = False
    with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=60) as smtp:
        smtp.login(settings.GMAIL_ADDRESS, settings.GMAIL_APP_PASSWORD)
        for message in ready:
            is_cold = message.kind != Message.Kind.REPLY
            if is_cold and (cold_sent >= room or cold_sent >= config.sends_per_tick):
                limit_hit = limit_hit or cold_sent >= room
                continue
            if send_message(message, config, smtp=smtp):
                sent += 1
                cold_sent += int(is_cold)
    if limit_hit:
        _log_once(f"Daily limit of {config.daily_send_limit} cold emails reached; the rest wait for tomorrow.", level="info")
    return sent
