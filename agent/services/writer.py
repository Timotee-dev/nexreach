"""Writes first emails, follow-ups and replies. Everything lands as a DRAFT —
nothing is sent until you approve it in the dashboard."""
from ..models import AgentConfig, Lead, Message, log
from . import llm

COMMON_RULES = """Rules:
- Plain text, no markdown, no emojis, no placeholders like [Name].
- Under {words} words in the body. Short paragraphs.
- Only use facts given here. Never invent clients, numbers, results or testimonials.
- Sound like one real person writing to another, not a marketing blast.
- Do NOT add a signature or sign-off name; it is added automatically.
- {extra_rules}"""


INITIAL_PROMPT = """Write a personalised cold email from {sender} at {company} to {name}.

WHAT WE SELL (pick the ONE service most relevant to this business; don't list them all)
{offer}

ABOUT THEM
Category: {category} in {city} ({country})
Summary: {summary}
Specific observation to open with: {hook}
Problems we detected: {signals}

Structure: open with the specific observation, connect it to one concrete benefit, end with a
low-pressure question (e.g. whether they'd like a quick idea or a short call). One link at most.

{rules}

JSON fields: "subject" (under 8 words, lowercase is fine, no clickbait), "body".
"""


FOLLOWUP_PROMPT = """Write follow-up number {number} to a cold email that got no reply.

From: {sender} at {company}
To: {name} ({category} in {city}, {country})
What we sell: {offer}
Previous emails in this thread:
{history}

Add something new (a different benefit or a quick idea for them) instead of just "bumping".
{final_note}

{rules}

JSON fields: "body".
"""


REPLY_PROMPT = """Draft a reply to a prospect who answered our cold email.

From: {sender} at {company}
What we sell: {offer}
Prospect: {name} ({category} in {city}, {country})
Conversation so far (oldest first):
{history}

Their reply was classified as: {classification}
Answer their questions directly using only facts given above. If they're interested, suggest a
concrete next step (a short call or sending a quick proposal). If you don't know something, say
you'll confirm it rather than guessing.

{rules}

JSON fields: "body".
"""


def _rules(config, words=120):
    return COMMON_RULES.format(words=words, extra_rules=config.writing_rules or "Be friendly and direct.")


def _history(lead: Lead, limit: int = 6) -> str:
    parts = []
    for msg in lead.messages.exclude(status__in=[Message.Status.REJECTED, Message.Status.FAILED]).order_by("-created_at")[:limit]:
        who = "THEM" if msg.direction == Message.Direction.IN else "US"
        parts.append(f"[{who}] Subject: {msg.subject}\n{msg.body.strip()[:1500]}")
    return "\n\n".join(reversed(parts)) or "(none)"


def footer(config: AgentConfig) -> str:
    address = f"\n{config.postal_address.strip()}" if config.postal_address.strip() else ""
    return f"\n\n{config.signature.strip()}{address}\n\n{config.opt_out_line.strip()}"


def _thread_subject(lead: Lead) -> str:
    first = lead.messages.filter(kind=Message.Kind.INITIAL).order_by("created_at").first()
    base = first.subject if first else f"{lead.name}"
    return base if base.lower().startswith("re:") else f"Re: {base}"


def _last_message_id(lead: Lead) -> str:
    last = lead.messages.exclude(message_id="").order_by("-created_at").first()
    return last.message_id if last else ""


def draft_initial(lead: Lead, config: AgentConfig | None = None) -> Message:
    config = config or AgentConfig.get()
    data = llm.ask_json(
        INITIAL_PROMPT.format(
            sender=config.sender_name,
            company=config.company_name,
            name=lead.name,
            offer=config.offer,
            category=lead.category or "business",
            city=lead.city or "their city",
            country=lead.market.name if lead.market_id else "unknown country",
            summary=lead.research_summary or "n/a",
            hook=lead.hook or "none",
            signals="; ".join(lead.site_signals or []) or "none",
            rules=_rules(config),
        )
    )
    msg = Message.objects.create(
        lead=lead,
        direction=Message.Direction.OUT,
        kind=Message.Kind.INITIAL,
        status=Message.Status.DRAFT,
        subject=str(data.get("subject", f"Quick idea for {lead.name}"))[:300],
        body=str(data.get("body", "")).strip() + footer(config),
    )
    lead.status = Lead.Status.DRAFTED
    lead.save(update_fields=["status", "updated_at"])
    log(f"Drafted first email to {lead.name} — waiting for your approval.", lead=lead)
    return msg


def draft_followup(lead: Lead, config: AgentConfig | None = None) -> Message:
    config = config or AgentConfig.get()
    number = lead.followups_sent + 1
    final = number >= config.max_followups
    data = llm.ask_json(
        FOLLOWUP_PROMPT.format(
            number=number,
            sender=config.sender_name,
            company=config.company_name,
            name=lead.name,
            category=lead.category or "business",
            city=lead.city or "",
            country=lead.market.name if lead.market_id else "",
            offer=config.offer,
            history=_history(lead),
            final_note="This is the last follow-up: politely say you won't email again." if final else "",
            rules=_rules(config, words=80),
        )
    )
    msg = Message.objects.create(
        lead=lead,
        direction=Message.Direction.OUT,
        kind=Message.Kind.FOLLOWUP,
        status=Message.Status.DRAFT,
        subject=_thread_subject(lead),
        body=str(data.get("body", "")).strip() + footer(config),
        in_reply_to=_last_message_id(lead),
    )
    log(f"Drafted follow-up #{number} to {lead.name} — waiting for your approval.", lead=lead)
    return msg


def draft_reply(lead: Lead, classification: str, config: AgentConfig | None = None) -> Message:
    config = config or AgentConfig.get()
    data = llm.ask_json(
        REPLY_PROMPT.format(
            sender=config.sender_name,
            company=config.company_name,
            offer=config.offer,
            name=lead.name,
            category=lead.category or "business",
            city=lead.city or "",
            country=lead.market.name if lead.market_id else "",
            history=_history(lead),
            classification=classification,
            rules=_rules(config, words=150),
        )
    )
    msg = Message.objects.create(
        lead=lead,
        direction=Message.Direction.OUT,
        kind=Message.Kind.REPLY,
        status=Message.Status.DRAFT,
        subject=_thread_subject(lead),
        body=str(data.get("body", "")).strip() + f"\n\n{config.signature.strip()}",
        in_reply_to=_last_message_id(lead),
    )
    log(f"{lead.name} replied — drafted an answer for you to approve.", "success", lead=lead)
    return msg


def redraft(message: Message) -> Message:
    """Throw away a draft and write a fresh one of the same kind."""
    lead, kind = message.lead, message.kind
    message.status = Message.Status.REJECTED
    message.save(update_fields=["status"])
    if kind == Message.Kind.INITIAL:
        return draft_initial(lead)
    if kind == Message.Kind.FOLLOWUP:
        return draft_followup(lead)
    last_in = lead.messages.filter(direction=Message.Direction.IN).order_by("-created_at").first()
    return draft_reply(lead, last_in.classification if last_in else "question")


WHATSAPP_PROMPT = """Write a short direct message from {sender} at {company} to {name}, a {category} in {city} ({country}).
It will be sent on {channels}, so it must read naturally as a chat message on any of them.

WHAT WE SELL (pick the ONE service most relevant to this business)
{offer}

ABOUT THEM
Summary: {summary}
Specific observation: {hook}
Problems we detected: {signals}

This is a first message, so it must feel personal and brief:
- Under 70 words. Plain text, no markdown, no subject line, at most one emoji (or none).
- Greet them by business name, say who you are in a few words, and mention you came across
  their business online.
- Open with the specific observation, link it to one concrete benefit, end with a short,
  low-pressure question (e.g. whether you can send a quick idea or mockup).
- Include the portfolio link only if it fits naturally. Never mention prices.
- Write in the language and spelling normally used for business in {country}.
- {extra_rules}

JSON fields: "message".
"""


def draft_whatsapp(lead: Lead, config: AgentConfig | None = None) -> str:
    config = config or AgentConfig.get()
    data = llm.ask_json(
        WHATSAPP_PROMPT.format(
            sender=config.sender_name,
            company=config.company_name,
            name=lead.name,
            category=lead.category or "business",
            city=lead.city or "their city",
            country=lead.market.name if lead.market_id else "their country",
            offer=config.offer,
            summary=lead.research_summary or "n/a",
            hook=lead.hook or "none",
            signals="; ".join(lead.site_signals or []) or "none",
            extra_rules=config.writing_rules or "Be friendly and direct.",
            channels=" or ".join(
                (["WhatsApp"] if lead.whatsapp_number else []) + [label for _, label, _, _ in lead.dm_channels]
            ) or "WhatsApp",
        )
    )
    lead.whatsapp_message = str(data.get("message", "")).strip()[:1500]
    lead.save(update_fields=["whatsapp_message", "updated_at"])
    log(f"Wrote a direct message for {lead.name}.", lead=lead)
    return lead.whatsapp_message
