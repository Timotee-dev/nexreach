"""Reads your Gmail inbox (IMAP), matches replies to leads, classifies them
with Claude and drafts answers. Uses BODY.PEEK so it never marks mail as read."""
import email
import imaplib
import re
from datetime import timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone

from ..models import Lead, Message, Suppression, log
from . import llm, writer
from .mailer import check_config

BOUNCE_SENDERS = ("mailer-daemon", "postmaster")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

CLASSIFY_PROMPT = """Classify this reply to our cold email.

Our last email:
{ours}

Their reply:
{theirs}

Categories:
- "interested": wants to talk, asks for price/proposal/call, or says yes
- "question": asks something but isn't clearly interested yet
- "not_interested": declines
- "unsubscribe": asks not to be contacted again (e.g. "no thanks", "remove me", "stop")
- "out_of_office": auto-reply / away message
- "other": anything else

JSON fields: "category" (one of the above), "summary" (one sentence).
"""


def _decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def body_text(msg: email.message.Message) -> str:
    plain, html = "", ""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain" and not plain:
            plain = text
        elif part.get_content_type() == "text/html" and not html:
            html = BeautifulSoup(text, "html.parser").get_text("\n")
    return (plain or html).strip()


def strip_quoted(text: str) -> str:
    """Keep only the new part of a reply."""
    lines = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^On .+wrote:\s*$", line.strip()):
            break
        lines.append(line)
    return "\n".join(lines).strip() or text.strip()


def match_lead(sender: str, in_reply_to: str, references: str):
    ids = [i for i in re.findall(r"<[^>]+>", f"{in_reply_to} {references}")]
    if ids:
        hit = Message.objects.filter(message_id__in=ids, direction=Message.Direction.OUT).select_related("lead").first()
        if hit:
            return hit.lead
    return Lead.objects.filter(email__iexact=sender).first() if sender else None


def handle_bounce(text: str) -> int:
    marked = 0
    for address in set(a.lower() for a in EMAIL_RE.findall(text)):
        for lead in Lead.objects.filter(email__iexact=address).exclude(status=Lead.Status.BOUNCED):
            lead.status = Lead.Status.BOUNCED
            lead.save(update_fields=["status", "updated_at"])
            Suppression.objects.get_or_create(email=address, defaults={"reason": "bounced"})
            log(f"Email to {lead.name} bounced — address removed.", "warning", lead=lead)
            marked += 1
    return marked


def classify(lead: Lead, text: str) -> dict:
    ours = lead.messages.filter(direction=Message.Direction.OUT, status=Message.Status.SENT).order_by("-sent_at").first()
    data = llm.ask_json(CLASSIFY_PROMPT.format(ours=ours.body[:1500] if ours else "(none)", theirs=text[:3000]), llm.cheap())
    category = str(data.get("category", "other")).strip().lower()
    if category not in {"interested", "question", "not_interested", "unsubscribe", "out_of_office", "other"}:
        category = "other"
    return {"category": category, "summary": str(data.get("summary", ""))[:500]}


def process_raw(raw: bytes) -> str | None:
    """Handle one email. Returns what happened, or None if it wasn't for us."""
    msg = email.message_from_bytes(raw)
    message_id = (msg.get("Message-ID") or "").strip()
    if message_id and Message.objects.filter(message_id=message_id).exists():
        return None  # already processed
    sender = parseaddr(msg.get("From", ""))[1].lower()
    text = body_text(msg)

    if any(sender.startswith(prefix) for prefix in BOUNCE_SENDERS):
        return "bounce" if handle_bounce(text) else None

    lead = match_lead(sender, msg.get("In-Reply-To", ""), msg.get("References", ""))
    if not lead:
        return None

    try:
        received = parsedate_to_datetime(msg.get("Date"))
    except (TypeError, ValueError):
        received = timezone.now()
    new_text = strip_quoted(text)
    inbound = Message.objects.create(
        lead=lead,
        direction=Message.Direction.IN,
        kind=Message.Kind.INBOUND,
        status=Message.Status.RECEIVED,
        subject=_decode(msg.get("Subject"))[:300],
        body=new_text[:10000],
        message_id=message_id,
        in_reply_to=(msg.get("In-Reply-To") or "")[:300],
        received_at=received,
    )
    subject = inbound.subject.strip().lower()
    if subject == "unsubscribe" or (subject.startswith("unsubscribe") and len(new_text) < 40):
        result = {"category": "unsubscribe", "summary": "Clicked unsubscribe."}
    else:
        result = classify(lead, new_text)
    inbound.classification, inbound.summary = result["category"], result["summary"]
    inbound.save(update_fields=["classification", "summary"])

    # Any reply stops pending cold drafts so you don't double-email them.
    lead.messages.filter(
        status__in=[Message.Status.DRAFT, Message.Status.APPROVED],
        kind__in=[Message.Kind.INITIAL, Message.Kind.FOLLOWUP],
    ).update(status=Message.Status.REJECTED, error="Cancelled: they replied.")

    category = result["category"]
    if category == "unsubscribe":
        lead.status = Lead.Status.UNSUBSCRIBED
        if lead.email:
            Suppression.objects.get_or_create(email=lead.email.lower(), defaults={"reason": "asked to stop"})
        log(f"{lead.name} asked not to be contacted. Done.", "warning", lead=lead)
    elif category == "not_interested":
        lead.status = Lead.Status.NOT_INTERESTED
        log(f"{lead.name} isn't interested: {result['summary']}", lead=lead)
    elif category == "out_of_office":
        log(f"{lead.name} sent an auto-reply; follow-ups continue later.", lead=lead)
        lead.save()
        return category
    else:
        lead.status = Lead.Status.INTERESTED if category == "interested" else Lead.Status.REPLIED
        lead.save()
        writer.draft_reply(lead, category)
        return category
    lead.save()
    return category


def check_inbox(days: int = 7) -> int:
    check_config()
    since = (timezone.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    handled = 0
    imap = imaplib.IMAP4_SSL(settings.IMAP_HOST)
    try:
        imap.login(settings.GMAIL_ADDRESS, settings.GMAIL_APP_PASSWORD)
        imap.select("INBOX", readonly=True)
        status, data = imap.search(None, f"(SINCE {since})")
        if status != "OK":
            return 0
        for num in data[0].split()[-200:]:
            status, parts = imap.fetch(num, "(BODY.PEEK[])")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            try:
                if process_raw(parts[0][1]):
                    handled += 1
            except llm.LLMError as exc:
                log(f"Couldn't classify a reply: {exc}", "error")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return handled
