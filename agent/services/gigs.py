"""Gig postings: people who publicly asked for a developer.

- Reddit: [Hiring] posts in the subreddits listed in Settings (public JSON feed, read-only).
- Hacker News: top-level "SEEKING FREELANCER" comments in the monthly
  "Freelancer? Seeking freelancer?" thread (via the free Algolia HN API).
- Remotive: contract and freelance software listings (public API; at most 4 checks a day,
  and every listing links back to Remotive as their terms require).
- Mastodon: public posts on hiring hashtags that clearly ask for a developer.

Nothing is posted automatically. Claude scores each gig and drafts a reply; you send it yourself.
"""
import datetime
import html
import re

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone

from ..models import AgentConfig, Lead, log
from . import llm
from .research import EMAIL_RE

REDDIT_NEW = "https://{host}/r/{sub}/new.json"
HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM = "https://hn.algolia.com/api/v1/items/{id}"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
REMOTIVE_EVERY = datetime.timedelta(hours=6)  # Remotive asks for no more than ~4 fetches a day
MASTODON_TAG = "https://{instance}/api/v1/timelines/tag/{tag}"

# Social posts have no [Hiring] tag, so they must clearly ask for someone to build something.
INTENT = re.compile(
    r"\b(looking (for|to hire)|seeking|need(s|ed)? (a|an|someone|help)|hiring (a|an)|want(s|ed)? (a|an|someone)|"
    r"anyone (know|recommend)|recommend a|in search of)\b[^.?!]{0,60}\b(developer|dev|programmer|engineer|"
    r"freelancer|web ?designer|agency|someone to build|website|web app|app)\b",
    re.I,
)
SELF_PROMO = re.compile(r"\b(for ?hire|open to work|available for (work|hire|projects)|i('m| am) (a|an) "
                        r"(freelance|developer|designer)|hire me|my services|dm me for)\b", re.I)


def _headers():
    return {"User-Agent": settings.HTTP_USER_AGENT}


def _plain(text: str) -> str:
    text = BeautifulSoup(html.unescape(text or ""), "html.parser").get_text("\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def matches_keywords(text: str, keywords: list[str]) -> bool:
    text = text.lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", text) for k in keywords)


def is_hiring_post(title: str, flair: str = "") -> bool:
    t = f"{flair} {title}".lower()
    if re.search(r"\[\s*for\s*hire\s*\]|\bfor hire\b", t):
        return False
    return bool(re.search(r"\[\s*hiring\s*\]|\bhiring\b|\btask\b", t))


def wants_developer(text: str) -> bool:
    return bool(INTENT.search(text)) and not SELF_PROMO.search(text)


def _save_gig(source, source_id, title, text, author, url, posted_at) -> bool:
    if Lead.objects.filter(source_id=source_id).exists():
        return False
    email = next(iter(EMAIL_RE.findall(text)), "")
    Lead.objects.create(
        kind="gig",
        name=(title or f"{source} post by {author}")[:255],
        category=source,
        source=source,
        source_id=source_id,
        post_title=title[:300],
        post_text=text[:8000],
        post_author=author[:100],
        post_url=url[:500],
        posted_at=posted_at,
        email=email[:254].strip(".").lower(),
    )
    return True


# ------------------------------------------------------------------ Reddit

def fetch_reddit(sub: str) -> list[dict]:
    last_error = None
    for host in ("www.reddit.com", "old.reddit.com"):
        try:
            resp = requests.get(REDDIT_NEW.format(host=host, sub=sub), params={"limit": 50, "raw_json": 1},
                                headers=_headers(), timeout=30)
        except requests.RequestException as exc:
            last_error = exc.__class__.__name__
            continue
        if resp.status_code == 200:
            return [c.get("data", {}) for c in resp.json().get("data", {}).get("children", [])]
        last_error = f"HTTP {resp.status_code}"
    raise requests.RequestException(f"Reddit r/{sub} unavailable ({last_error})")


def reddit_gigs(config: AgentConfig) -> int:
    cutoff = timezone.now() - datetime.timedelta(days=config.gig_max_age_days)
    created = 0
    pauses = dict(config.gig_source_pauses or {})
    for sub in config.subreddits:
        key = f"reddit:{sub.lower()}"
        until = pauses.get(key)
        if until and timezone.now().isoformat() < until:
            continue
        try:
            posts = fetch_reddit(sub)
        except requests.RequestException as exc:
            pauses[key] = (timezone.now() + datetime.timedelta(hours=24)).isoformat()
            log(f"Gigs: {exc}. Skipping r/{sub} for 24 hours.", "warning")
            continue
        pauses.pop(key, None)
        for post in posts:
            posted = datetime.datetime.fromtimestamp(post.get("created_utc", 0), tz=datetime.timezone.utc)
            title, body = post.get("title", ""), post.get("selftext", "")
            if posted < cutoff or post.get("over_18") or post.get("author") in (None, "[deleted]"):
                continue
            if not is_hiring_post(title, post.get("link_flair_text") or ""):
                continue
            if not matches_keywords(f"{title}\n{body}", config.keywords):
                continue
            url = "https://www.reddit.com" + post.get("permalink", "")
            if _save_gig("reddit", f"reddit:{post.get('id')}", title, body, post.get("author", ""), url, posted):
                created += 1
    if pauses != (config.gig_source_pauses or {}):
        config.gig_source_pauses = pauses
        config.save(update_fields=["gig_source_pauses"])
    return created


# ------------------------------------------------------------------ Hacker News

def latest_freelancer_thread() -> str | None:
    resp = requests.get(HN_SEARCH, params={"tags": "story,author_whoishiring", "query": "freelancer", "hitsPerPage": 5},
                        headers=_headers(), timeout=30)
    resp.raise_for_status()
    for hit in resp.json().get("hits", []):
        if "freelancer" in (hit.get("title") or "").lower():
            return str(hit["objectID"])
    return None


def hn_gigs(config: AgentConfig) -> int:
    thread = latest_freelancer_thread()
    if not thread:
        return 0
    resp = requests.get(HN_ITEM.format(id=thread), headers=_headers(), timeout=60)
    resp.raise_for_status()
    cutoff = timezone.now() - datetime.timedelta(days=max(config.gig_max_age_days, 31))  # thread runs all month
    created = 0
    for child in resp.json().get("children", []):
        text = _plain(child.get("text") or "")
        if not text.upper().startswith("SEEKING FREELANCER"):
            continue
        if not matches_keywords(text, config.keywords):
            continue
        try:
            posted = datetime.datetime.fromisoformat((child.get("created_at") or "").replace("Z", "+00:00"))
        except ValueError:
            posted = timezone.now()
        if posted < cutoff:
            continue
        first_line = text.splitlines()[0][:160]
        url = f"https://news.ycombinator.com/item?id={child.get('id')}"
        if _save_gig("hn", f"hn:{child.get('id')}", first_line, text, child.get("author") or "", url, posted):
            created += 1
    return created


# ------------------------------------------------------------------ Remotive

def remotive_gigs(config: AgentConfig, force: bool = False) -> int:
    if config.remotive_checked_at and not force and timezone.now() - config.remotive_checked_at < REMOTIVE_EVERY:
        return 0
    config.remotive_checked_at = timezone.now()
    config.save(update_fields=["remotive_checked_at"])
    resp = requests.get(REMOTIVE_URL, params={"category": "software-dev"}, headers=_headers(), timeout=60)
    if resp.status_code != 200:
        raise requests.RequestException(f"HTTP {resp.status_code}")
    data = resp.json()
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    cutoff = timezone.now() - datetime.timedelta(days=max(config.gig_max_age_days, 14))
    created = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if (job.get("job_type") or "").lower() not in ("contract", "freelance"):
            continue
        text = _plain(job.get("description") or "")
        title = f"{job.get('title', '')} at {job.get('company_name', '')}".strip()
        if not matches_keywords(f"{title}\n{text}", config.keywords):
            continue
        try:
            posted = datetime.datetime.fromisoformat(job.get("publication_date", "")).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            posted = timezone.now()
        if posted < cutoff:
            continue
        extra = " | ".join(x for x in [job.get("job_type", "").title(), job.get("candidate_required_location", ""),
                                        job.get("salary", "")] if x)
        if _save_gig("remotive", f"remotive:{job.get('id')}", title, f"{extra}\n\n{text}", job.get("company_name", ""),
                     job.get("url", ""), posted):
            created += 1
    return created


# ------------------------------------------------------------------ Mastodon

def mastodon_gigs(config: AgentConfig) -> int:
    cutoff = timezone.now() - datetime.timedelta(days=config.gig_max_age_days)
    created = 0
    for tag in config.hashtags:
        url = MASTODON_TAG.format(instance=config.mastodon_instance.strip().strip("/") or "mastodon.social", tag=tag)
        try:
            resp = requests.get(url, params={"limit": 40}, headers=_headers(), timeout=30)
        except requests.RequestException as exc:
            log(f"Gigs: Mastodon #{tag} unavailable ({exc.__class__.__name__}).", "warning")
            continue
        if resp.status_code != 200:
            log(f"Gigs: Mastodon #{tag} unavailable (HTTP {resp.status_code}).", "warning")
            continue
        data = resp.json()
        for status in data if isinstance(data, list) else []:
            if not isinstance(status, dict):
                continue
            text = _plain(status.get("content") or "")
            if not text or not wants_developer(text) or not matches_keywords(text, config.keywords):
                continue
            try:
                posted = datetime.datetime.fromisoformat((status.get("created_at") or "").replace("Z", "+00:00"))
            except ValueError:
                posted = timezone.now()
            if posted < cutoff:
                continue
            account = status.get("account") or {}
            title = text.splitlines()[0][:140]
            if _save_gig("mastodon", f"mastodon:{status.get('uri') or status.get('id')}", title, text,
                         "@" + (account.get("acct") or "unknown"), status.get("url") or "", posted):
                created += 1
    return created


# ------------------------------------------------------------------ the agent step

def find_gigs(config: AgentConfig | None = None, force: bool = False) -> int:
    config = config or AgentConfig.get()
    if not config.gigs_enabled:
        return 0
    due = config.gigs_checked_at is None or timezone.now() - config.gigs_checked_at >= datetime.timedelta(
        minutes=config.gig_check_minutes
    )
    if not (due or force):
        return 0
    config.gigs_checked_at = timezone.now()
    config.save(update_fields=["gigs_checked_at"])
    created = reddit_gigs(config) if config.subreddits else 0
    if config.hn_gigs_enabled:
        try:
            created += hn_gigs(config)
        except requests.RequestException as exc:
            log(f"Gigs: Hacker News unavailable ({exc.__class__.__name__}).", "warning")
    if config.remotive_enabled:
        try:
            created += remotive_gigs(config, force=force)
        except requests.RequestException as exc:
            log(f"Gigs: Remotive unavailable ({exc}).", "warning")
    if config.mastodon_enabled and config.hashtags:
        created += mastodon_gigs(config)
    if created:
        log(f"Found {created} new gig post{'s' if created != 1 else ''} from people looking for a developer.", "success")
    return created


GIG_PROMPT = """Someone posted publicly that they want work done. Decide if {company} should reply, and draft the reply.

WHAT WE SELL
{offer}

THE POST ({where}, by {author}, {age})
Title: {title}
{text}

Score 0-100 how good a fit this is: high if it's software/web/app/automation work we can do and the
poster is a client (not someone offering their own services), lower for tiny/unpaid jobs or skills we
don't offer. Only use facts in the post.

Reply rules:
- From {sender} at {company}. Plain text, under 130 words, no markdown, no placeholders.
- Write in the same language as their post.
- If it's a job listing, write it as a short application note for the contract.
- Open by referring to exactly what they asked for. Say in one or two sentences how we'd approach it.
- Never invent past clients, results or experience. Mention the portfolio link once.
- If they ask for rates or give a budget, you may quote the matching starting price from WHAT WE SELL.
- End with one clear next step (a quick call, or questions to scope it).
- {extra_rules}

JSON fields: "fit_score" (int), "fit_reason" (one sentence), "summary" (one sentence: what they need),
"budget" (their stated budget or "" if none), "reply" (the message).
"""


def research_gig(lead: Lead, config: AgentConfig | None = None) -> Lead:
    config = config or AgentConfig.get()
    where = {"reddit": "Reddit", "hn": "Hacker News", "remotive": "a contract job listing on Remotive",
             "mastodon": "Mastodon"}.get(lead.source, lead.source)
    age = f"{(timezone.now() - lead.posted_at).days} days ago" if lead.posted_at else "recently"
    data = llm.ask_json(
        GIG_PROMPT.format(
            company=config.company_name, sender=config.sender_name, offer=config.offer, where=where,
            author=lead.post_author or "unknown", age=age, title=lead.post_title, text=lead.post_text[:5000],
            extra_rules=config.writing_rules or "Be friendly and direct.",
        )
    )
    try:
        lead.fit_score = max(0, min(100, int(data.get("fit_score", 0))))
    except (TypeError, ValueError):
        lead.fit_score = 0
    lead.fit_reason = str(data.get("fit_reason", ""))[:1000]
    lead.research_summary = str(data.get("summary", ""))[:2000]
    lead.budget = str(data.get("budget", ""))[:120]
    lead.whatsapp_message = str(data.get("reply", "")).strip()[:2000]
    lead.researched_at = timezone.now()
    lead.status = Lead.Status.QUALIFIED if lead.fit_score >= config.min_fit_score else Lead.Status.DISQUALIFIED
    lead.save()
    log(f"Checked gig '{lead.post_title[:60]}': fit {lead.fit_score}/100.", lead=lead)
    return lead
