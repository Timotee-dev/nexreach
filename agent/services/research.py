"""Researches a lead: reads its website, spots problems a dev agency can fix,
finds a contact email and phone/WhatsApp number, then asks Claude whether it's worth contacting."""
import datetime
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone

from ..contacts import find_socials, normalise_phone
from ..models import AgentConfig, Lead, log
from . import llm

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD_EMAIL_PARTS = ("example.", "sentry", "wixpress", "domain.com", "@2x", ".png", ".jpg", ".webp", "yourname")
CONTACT_PATHS = ["/contact", "/contact-us", "/contact.html", "/about"]
# Nigerian numbers: +234 / 234 / 0 followed by 7, 8 or 9 then 0/1 and 8 digits (spaces/dashes allowed)
NG_PHONE_RE = re.compile(r"(?<!\d)(?:\+?234[\s.-]?|0)[789][01](?:[\s.-]?\d){8}(?!\d)")
WA_RE = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=|whatsapp\.com/send\?phone=)\+?(\d{8,15})", re.I)


US_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?[2-9]\d{2}\)?[\s.-]?\d{3}[\s.-]\d{4}(?!\d)")
UK_PHONE_RE = re.compile(r"(?<!\d)(?:\+44\s?(?:\(0\)\s?)?|0)\d{2,4}[\s-]?\d{3,4}[\s-]?\d{3,4}(?!\d)")
TEXT_PHONE_RES = {"234": NG_PHONE_RE, "1": US_PHONE_RE, "44": UK_PHONE_RE}


def find_whatsapp_numbers(html: str, country_code: str = "234") -> list[str]:
    """Numbers the business itself links to WhatsApp (wa.me / api.whatsapp.com links)."""
    clean = []
    for raw in WA_RE.findall(html or ""):
        number = normalise_phone("+" + raw.lstrip("+"), country_code)
        if number and number not in clean:
            clean.append(number)
    return clean


def find_phones(html: str, country_code: str = "234") -> list[str]:
    """Phone and WhatsApp numbers from a page: WhatsApp links first, then tel: links, then Nigerian numbers in the text."""
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for a in soup.find_all("a", href=True):
        match = WA_RE.search(a["href"])
        if match:
            found.append(match.group(1))
    found += WA_RE.findall(html)
    for a in soup.select('a[href^="tel:"]'):
        found.append(a["href"][4:])
    text_re = TEXT_PHONE_RES.get(country_code)
    if text_re:
        found += text_re.findall(soup.get_text(" "))
    clean = []
    for raw in found:
        number = normalise_phone(raw, country_code)
        if number and number not in clean:
            clean.append(number)
    return clean


def fetch(url: str):
    started = time.monotonic()
    resp = requests.get(
        url,
        headers={"User-Agent": settings.HTTP_USER_AGENT},
        timeout=20,
        allow_redirects=True,
    )
    return resp, time.monotonic() - started


def find_emails(html: str, site_domain: str = "") -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for a in soup.select('a[href^="mailto:"]'):
        found.append(a["href"][7:].split("?")[0])
    found += EMAIL_RE.findall(soup.get_text(" "))
    clean = []
    for email in found:
        email = email.strip().strip(".").lower()
        if EMAIL_RE.fullmatch(email) and not any(bad in email for bad in BAD_EMAIL_PARTS) and email not in clean:
            clean.append(email)
    # Prefer an address on the business's own domain
    if site_domain:
        clean.sort(key=lambda e: 0 if e.endswith(site_domain) else 1)
    return clean


def site_signals(resp, seconds: float, html: str) -> list[str]:
    """Concrete, checkable problems — the kind a web developer can fix."""
    signals = []
    soup = BeautifulSoup(html, "html.parser")
    if not resp.url.startswith("https://"):
        signals.append("Site isn't served over HTTPS (browsers show 'Not secure').")
    if not soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)}):
        signals.append("No mobile viewport tag — the site probably doesn't adapt to phones.")
    if seconds > 4:
        signals.append(f"Homepage took about {seconds:.0f}s to load.")
    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(20\d{2})", html, re.I)]
    if years and max(years) <= datetime.date.today().year - 2:
        signals.append(f"Footer copyright says {max(years)}, so the site may not have been updated in a while.")
    if not soup.title or not soup.title.get_text(strip=True):
        signals.append("Homepage has no page title (hurts Google search).")
    if not soup.find("meta", attrs={"name": re.compile("^description$", re.I)}):
        signals.append("No meta description for Google search results.")
    builders = {"wix.com": "Wix", "blogspot.": "Blogspot", "wordpress.com": "WordPress.com", "business.site": "Google Business Sites"}
    for marker, label in builders.items():
        if marker in resp.url or marker in html[:20000]:
            signals.append(f"Built on {label}.")
            break
    return signals


def page_text(html: str, limit: int = 4000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return text[:limit]


def gather(lead: Lead, country_code: str | None = None) -> dict:
    """Collect facts about the lead without using Claude."""
    country_code = country_code or lead.calling_code
    facts = {"has_website": bool(lead.website), "signals": [], "text": "", "title": "", "emails": [], "phones": [],
             "whatsapp": [], "socials": {}}
    if not lead.website:
        facts["signals"].append("No website listed — customers can only find them on maps or social media.")
        return facts
    try:
        resp, seconds = fetch(lead.website)
    except requests.RequestException as exc:
        facts["signals"].append(f"Website listed but didn't load ({exc.__class__.__name__}).")
        facts["site_down"] = True
        return facts
    if resp.status_code >= 400:
        facts["signals"].append(f"Website returned an error (HTTP {resp.status_code}).")
        facts["site_down"] = True
        return facts

    html = resp.text
    domain = urlparse(resp.url).netloc.removeprefix("www.")
    soup = BeautifulSoup(html, "html.parser")
    facts["title"] = soup.title.get_text(strip=True)[:200] if soup.title else ""
    facts["signals"] = site_signals(resp, seconds, html)
    facts["text"] = page_text(html)
    facts["emails"] = find_emails(html, domain)
    facts["phones"] = find_phones(html, country_code)
    facts["whatsapp"] = find_whatsapp_numbers(html, country_code)
    facts["socials"] = find_socials(html)

    # Look on contact/about pages for whatever is still missing.
    if not facts["emails"] or not facts["phones"] or not facts["socials"]:
        for path in CONTACT_PATHS:
            try:
                sub, _ = fetch(urljoin(resp.url, path))
            except requests.RequestException:
                continue
            if sub.status_code >= 400:
                continue
            if not facts["emails"]:
                facts["emails"] = find_emails(sub.text, domain)
            if not facts["phones"]:
                facts["phones"] = find_phones(sub.text, country_code)
            if not facts["whatsapp"]:
                facts["whatsapp"] = find_whatsapp_numbers(sub.text, country_code)
            for network, url in find_socials(sub.text).items():
                facts["socials"].setdefault(network, url)
            if facts["emails"] and facts["phones"]:
                break
    return facts


QUALIFY_PROMPT = """You qualify sales leads for {company}.

WHAT WE SELL
{offer}

IDEAL CUSTOMER
{icp}

THE BUSINESS
Name: {name}
Category: {category}
City: {city}
Country: {country}
Address: {address}
Website: {website}
Page title: {title}
Problems we detected: {signals}
Website text (truncated):
{text}

Decide how good a fit this business is (0-100) for what we sell. Score higher when there is a
clear, specific problem we can solve. Only use facts given above — never invent details.

JSON fields:
- "fit_score": integer 0-100
- "fit_reason": one sentence on why
- "summary": 2-3 sentences describing the business, from the facts above only
- "hook": one specific, true observation to open a cold email with (e.g. a problem we detected or something
  notable on their site). Empty string if there is nothing specific.
"""


def qualify(lead: Lead, facts: dict, config: AgentConfig) -> dict:
    prompt = QUALIFY_PROMPT.format(
        company=config.company_name,
        offer=config.offer,
        icp=config.ideal_customer,
        name=lead.name,
        category=lead.category or "unknown",
        city=lead.city or "unknown",
        country=lead.market.name if lead.market_id else "unknown",
        address=lead.address or "unknown",
        website=lead.website or "none",
        title=facts.get("title") or "n/a",
        signals="; ".join(facts["signals"]) or "none found",
        text=facts.get("text") or "(no website text)",
    )
    data = llm.ask_json(prompt)
    try:
        score = max(0, min(100, int(data.get("fit_score", 0))))
    except (TypeError, ValueError):
        score = 0
    return {
        "fit_score": score,
        "fit_reason": str(data.get("fit_reason", ""))[:1000],
        "summary": str(data.get("summary", ""))[:2000],
        "hook": str(data.get("hook", ""))[:1000],
    }


def apply_contacts(lead: Lead, facts: dict) -> list[str]:
    """Fill in missing contact details from gathered facts. Returns what changed."""
    changed = []
    if not lead.email and facts.get("emails"):
        lead.email = facts["emails"][0]
        changed.append("email")
    if facts.get("whatsapp") and not lead.whatsapp_confirmed:
        lead.phone = facts["whatsapp"][0][:60]   # a number they link to WhatsApp beats any other
        lead.whatsapp_confirmed = True
        changed.append("WhatsApp")
    elif not lead.phone and facts.get("phones"):
        lead.phone = facts["phones"][0][:60]
        changed.append("phone")
    socials = dict(lead.socials or {})
    for network, url in (facts.get("socials") or {}).items():
        if network not in socials:
            socials[network] = url
            changed.append(network)
    lead.socials = socials
    return changed


def research_lead(lead: Lead, config: AgentConfig | None = None) -> Lead:
    config = config or AgentConfig.get()
    facts = gather(lead, lead.calling_code)
    apply_contacts(lead, facts)
    result = qualify(lead, facts, config)

    lead.site_signals = facts["signals"]
    lead.fit_score = result["fit_score"]
    lead.fit_reason = result["fit_reason"]
    lead.research_summary = result["summary"]
    lead.hook = result["hook"]
    lead.researched_at = timezone.now()

    if lead.fit_score < config.min_fit_score:
        lead.status = Lead.Status.DISQUALIFIED
    elif not lead.email:
        lead.status = Lead.Status.NO_EMAIL
    else:
        lead.status = Lead.Status.QUALIFIED
    lead.save()
    log(f"Researched {lead.name}: fit {lead.fit_score}/100 → {lead.get_status_display()}.", lead=lead)
    return lead
