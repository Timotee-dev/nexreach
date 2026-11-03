"""Researches a lead: reads its website, spots problems a dev agency can fix,
finds a contact email and phone/WhatsApp number, then asks Claude whether it's worth contacting."""
import datetime
import functools
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone

from ..contacts import find_socials, normalise_phone
from django.db.models import Case, IntegerField, Q, Value, When

from ..models import AgentConfig, Lead, SearchRun, log
from . import llm

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD_EMAIL_PARTS = ("example.", "sentry", "wixpress", "domain.com", "@2x", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                   ".svg", "yourname", "youremail", "your-email", "email@email", "noreply", "no-reply", "donotreply",
                   "do-not-reply", "@sentry", "godaddy.com", "squarespace.com", "wordpress.com", "placeholder")
ROLE_FIRST = ("info@", "contact@", "hello@", "office@", "enquiries@", "inquiries@", "admin@", "sales@", "mail@",
              "reception@", "frontdesk@", "team@")
ROLE_LAST = ("privacy@", "abuse@", "webmaster@", "postmaster@", "careers@", "jobs@", "hr@", "legal@", "dpo@")
# "name [at] domain [dot] com", "name(at)domain.com", "name at domain dot com"
OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+-]+)\s*(?:\[at\]|\(at\)|\{at\}|\s+at\s+)\s*([A-Za-z0-9-]+(?:\s*(?:\[dot\]|\(dot\)|\{dot\}|\s+dot\s+|\.)\s*[A-Za-z0-9-]+)+)",
    re.I,
)
CONTACT_PATHS = ["/contact", "/contact-us", "/contactus", "/contact.html", "/about", "/about-us", "/team",
                 "/impressum", "/kontakt"]
CONTACT_LINK_RE = re.compile(r"contact|about|team|impressum|kontakt|get[- ]in[- ]touch|reach[- ]us|location", re.I)
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


def fetch(url: str, timeout: int = 20):
    started = time.monotonic()
    resp = requests.get(
        url,
        headers={"User-Agent": settings.HTTP_USER_AGENT},
        timeout=timeout,
        allow_redirects=True,
    )
    return resp, time.monotonic() - started


def _decode_cfemail(hexstr: str) -> str:
    """Cloudflare hides emails as hex in data-cfemail; the first byte is the XOR key."""
    try:
        data = bytes.fromhex(hexstr)
        return "".join(chr(b ^ data[0]) for b in data[1:])
    except (ValueError, IndexError):
        return ""


def _rank(email: str, site_domain: str) -> tuple:
    own = 0 if site_domain and (email.endswith("@" + site_domain) or email.endswith("." + site_domain)) else 1
    role = 0 if email.startswith(ROLE_FIRST) else (2 if email.startswith(ROLE_LAST) else 1)
    return (own, role)


def find_emails(html: str, site_domain: str = "") -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for a in soup.select('a[href^="mailto:"], a[href^="MAILTO:"]'):
        found.append(a["href"][7:].split("?")[0])
    for tag in soup.select("[data-cfemail]"):
        found.append(_decode_cfemail(tag.get("data-cfemail", "")))
    for a in soup.select('a[href*="email-protection#"]'):
        found.append(_decode_cfemail(a["href"].rsplit("#", 1)[-1]))
    for script in soup.select('script[type="application/ld+json"]'):
        found += EMAIL_RE.findall(script.get_text() or "")
    text = soup.get_text(" ")
    found += EMAIL_RE.findall(text)
    for user, rest in OBFUSCATED_RE.findall(text):
        domain = re.sub(r"\s*(?:\[dot\]|\(dot\)|\{dot\}|\s+dot\s+)\s*", ".", rest, flags=re.I).replace(" ", "")
        if "." in domain:
            found.append(f"{user}@{domain}")
    clean = []
    for email in found:
        email = email.strip().strip(".").lower().removeprefix("mailto:")
        email = email.split("%20")[0]
        if EMAIL_RE.fullmatch(email) and not any(bad in email for bad in BAD_EMAIL_PARTS) and email not in clean:
            clean.append(email)
    clean.sort(key=lambda e: _rank(e, site_domain))
    return clean


@functools.lru_cache(maxsize=4096)
def has_mx(domain: str) -> bool:
    """True unless DNS clearly says the domain can't receive email. Network trouble counts as True."""
    try:
        import dns.exception
        import dns.resolver
    except ImportError:
        return True
    try:
        dns.resolver.resolve(domain, "MX", lifetime=5)
        return True
    except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
        return False
    except dns.resolver.NoAnswer:
        try:  # no MX record: mail falls back to the domain's own address
            dns.resolver.resolve(domain, "A", lifetime=5)
            return True
        except dns.exception.DNSException:
            return False
    except dns.exception.DNSException:
        return True


def usable_email(email: str) -> bool:
    if not email or not EMAIL_RE.fullmatch(email) or any(bad in email.lower() for bad in BAD_EMAIL_PARTS):
        return False
    if not settings.EMAIL_MX_CHECK:
        return True
    return has_mx(email.rsplit("@", 1)[-1].lower())


def contact_links(html: str, base_url: str, limit: int = 3) -> list[str]:
    """Contact/about pages linked from the homepage, on the same site."""
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(base_url).netloc
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        if not (CONTACT_LINK_RE.search(href) or CONTACT_LINK_RE.search(a.get_text(" ") or "")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        if urlparse(url).netloc == host and url not in links and url.rstrip("/") != base_url.rstrip("/"):
            links.append(url)
        if len(links) >= limit:
            break
    return links


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


def gather(lead: Lead, country_code: str | None = None, timeout: int = 20) -> dict:
    """Collect facts about the lead without using Claude."""
    country_code = country_code or lead.calling_code
    facts = {"has_website": bool(lead.website), "signals": [], "text": "", "title": "", "emails": [], "phones": [],
             "whatsapp": [], "socials": {}}
    if not lead.website:
        facts["signals"].append("No website listed — customers can only find them on maps or social media.")
        return facts
    try:
        resp, seconds = fetch(lead.website, timeout=timeout)
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

    # Look on contact/about pages (the ones the site links to first) for whatever is still missing.
    if not facts["emails"] or not facts["phones"] or not facts["socials"]:
        pages = contact_links(html, resp.url)
        pages += [urljoin(resp.url, p) for p in CONTACT_PATHS if urljoin(resp.url, p) not in pages]
        tried = 0
        for url in pages:
            if tried >= 5 or (facts["emails"] and facts["phones"]):
                break
            tried += 1
            try:
                sub, _ = fetch(url, timeout=timeout)
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
            for network, url_ in find_socials(sub.text).items():
                facts["socials"].setdefault(network, url_)
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
    if not lead.email:
        for email in facts.get("emails") or []:
            if usable_email(email):
                lead.email = email[:254]
                lead.email_found_at = timezone.now()
                changed.append("email")
                break
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
    facts = lead.scan_cache if lead.scan_cache.get("signals") is not None and lead.contacts_checked_at else None
    if facts is None:
        facts = gather(lead, lead.calling_code)
        apply_contacts(lead, facts)
    result = qualify(lead, facts, config)

    lead.site_signals = facts["signals"]
    lead.fit_score = result["fit_score"]
    lead.fit_reason = result["fit_reason"]
    lead.research_summary = result["summary"]
    lead.hook = result["hook"]
    lead.researched_at = timezone.now()
    lead.scan_cache = {}

    if lead.fit_score < config.min_fit_score:
        lead.status = Lead.Status.DISQUALIFIED
    elif not lead.email:
        lead.status = Lead.Status.NO_EMAIL
    else:
        lead.status = Lead.Status.QUALIFIED
    lead.save()
    log(f"Researched {lead.name}: fit {lead.fit_score}/100 → {lead.get_status_display()}.", lead=lead)
    return lead


def scan_many(leads: list[Lead], timeout: int = 12, workers: int = 8) -> list[dict]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda l: gather(l, l.calling_code, timeout=timeout), leads))


def scan_contacts(config: AgentConfig | None = None, limit: int | None = None, workers: int = 8) -> int:
    """Free step (no Claude): visit new businesses' websites in parallel to find emails, phones and
    socials. Leads with no way to contact them are set aside so Claude isn't spent on them.
    Returns how many new emails were found."""
    config = config or AgentConfig.get()
    leads = list(
        Lead.objects.filter(status=Lead.Status.NEW, kind="business", contacts_checked_at__isnull=True)
        .filter(config.focus_filter())
        .select_related("market")
        .order_by("-created_at")[: limit or config.scan_per_tick]  # newest search first
    )
    if not leads:
        return 0
    results = scan_many(leads, workers=workers)
    found = 0
    for lead, facts in zip(leads, results):
        changed = apply_contacts(lead, facts)
        found += int("email" in changed)
        lead.contacts_checked_at = timezone.now()
        lead.scan_cache = {k: facts.get(k) for k in ("signals", "text", "title", "has_website", "site_down")}
        if not lead.email and not lead.phone and not lead.socials:
            lead.status = Lead.Status.DISQUALIFIED
            lead.fit_reason = "No email, phone or social account found."
            lead.scan_cache = {}
        lead.save()
    if found:
        log(f"Found {found} new email{'s' if found != 1 else ''} by scanning {len(leads)} websites.", "success")
    report_finished_searches({l.search_run_id for l in leads if l.search_run_id}, config)
    return found


def report_finished_searches(run_ids: set, config: AgentConfig | None = None):
    """Once every business from a search has been scanned, log how many emails that search gave."""
    config = config or AgentConfig.get()
    for run in SearchRun.objects.filter(pk__in=run_ids, reported=False):
        leads = run.leads.all()
        if leads.filter(contacts_checked_at__isnull=True).exists():
            continue
        total = leads.count()
        emails = leads.exclude(email="").count()
        run.reported = True
        run.save(update_fields=["reported"])
        ok = emails >= config.min_emails_per_search
        log(f"Search '{run.sector} in {run.city.split(',')[0]}': {total} businesses → {emails} emails"
            f"{'' if ok else f' (below your {config.min_emails_per_search} minimum)'}.", "success" if ok else "info")


def average_emails_per_search(last: int = 20) -> float | None:
    from django.db.models import Count, Q

    runs = list(
        SearchRun.objects.filter(reported=True)
        .annotate(n=Count("leads"), emails=Count("leads", filter=~Q(leads__email="")))
        .filter(n__gt=0)
        .values_list("emails", flat=True)[:last]
    )
    return round(sum(runs) / len(runs), 1) if runs else None


def emails_found_today() -> int:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return Lead.objects.filter(kind="business", email_found_at__gte=start).count()


def research_queue(config: AgentConfig):
    """Scanned leads and gigs waiting for Claude: ones with an email first, then ones you can DM."""
    return (
        Lead.objects.filter(status=Lead.Status.NEW)
        .filter(config.focus_filter())
        .filter(Q(kind="gig") | Q(contacts_checked_at__isnull=False))
        .filter(Q(kind="gig") | ~Q(email="") | Q(pk__in=[] if not config.research_dm_leads else Lead.objects.values("pk")))
        .annotate(has_email=Case(When(email="", then=Value(1)), default=Value(0), output_field=IntegerField()))
        .order_by("has_email", "created_at")
    )
