"""Finds businesses on OpenStreetMap (free, no account needed).

Nominatim turns a city name into an area; Overpass lists businesses in it that
match an OSM tag such as shop=clothes. Please keep volumes modest — these are
free community services.
"""
import requests
from django.conf import settings

from ..contacts import social_url
from ..models import AgentConfig, Lead, Market, log

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Public copies of the same service, tried in order when one is busy or down.
OVERPASS_URLS = [
    OVERPASS_URL,
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]


class OverpassBusy(Exception):
    pass


def run_overpass(query: str):
    """Run a query, falling back to the next server on overload (429/5xx), timeouts or connection errors."""
    problems = []
    for url in OVERPASS_URLS:
        try:
            resp = requests.post(url, data={"data": query}, headers=_headers(), timeout=90)
        except requests.RequestException as exc:
            problems.append(f"{url.split('/')[2]}: {exc.__class__.__name__}")
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            problems.append(f"{url.split('/')[2]}: HTTP {resp.status_code}")
            continue
        resp.raise_for_status()
        return resp.json()
    raise OverpassBusy("All map servers are busy right now (" + "; ".join(problems) + "). The agent will try again next check-in.")


def _headers():
    return {"User-Agent": settings.HTTP_USER_AGENT}


CITY_TYPES = {"city", "town", "municipality", "village", "state", "county", "administrative", "suburb", "borough"}


def cached_area_id(city: str, market: Market | None) -> int | None:
    """City boundary lookups rarely change, so remember them per country (saves time and is polite
    to the free Nominatim service)."""
    if market is not None:
        known = (market.area_ids or {}).get(city)
        if known:
            return known
    area_id = city_area_id(city)
    if area_id and market is not None:
        market.area_ids = {**(market.area_ids or {}), city: area_id}
        Market.objects.filter(pk=market.pk).update(area_ids=market.area_ids)
    return area_id


def city_area_id(city: str):
    """Find the OpenStreetMap boundary for a city. Only accepts real places (not shops or
    streets that happen to share the name), most important match first."""
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": city, "format": "jsonv2", "limit": 10},
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    places = [
        p for p in resp.json()
        if p.get("osm_type") == "relation"
        and (p.get("category") in {"boundary", "place"} or p.get("class") in {"boundary", "place"})
        and (p.get("addresstype") in CITY_TYPES or p.get("type") in CITY_TYPES)
    ]
    if not places:
        return None
    best = max(places, key=lambda p: float(p.get("importance") or 0))
    log(f"Searching inside: {best.get('display_name', city)[:150]}")
    return 3600000000 + int(best["osm_id"])


CONTACT_KEYS = ["website", "contact:website", "url", "email", "contact:email"]


def overpass_query(area_id: int, tag: str, limit: int, contactable_only: bool = False) -> str:
    """Businesses with this tag and a name. With contactable_only, only ones that list a website or
    email, which is where emails come from."""
    key, value = [part.strip() for part in tag.split("=", 1)]
    base = f'["{key}"="{value}"]["name"]'
    selectors = [base + f'["{k}"]' for k in CONTACT_KEYS] if contactable_only else [base]
    parts = "".join(f"{t}{sel}(area.a);" for sel in selectors for t in ("node", "way", "relation"))
    return f"[out:json][timeout:90];area(id:{area_id})->.a;({parts});out center tags {limit};"


OSM_SOCIAL_TAGS = {
    "instagram": ("contact:instagram", "instagram"),
    "facebook": ("contact:facebook", "facebook"),
    "linkedin": ("contact:linkedin", "linkedin"),
    "x": ("contact:twitter", "twitter", "contact:x"),
    "tiktok": ("contact:tiktok", "tiktok"),
}


def _first(tags, *keys):
    for key in keys:
        if tags.get(key):
            return tags[key].split(";")[0].strip()
    return ""


def parse_element(element: dict, city: str, tag: str) -> dict | None:
    tags = element.get("tags", {})
    name = tags.get("name", "").strip()
    if not name:
        return None
    website = _first(tags, "website", "contact:website", "url")
    if website and not website.startswith(("http://", "https://")):
        website = "http://" + website
    address = ", ".join(
        part for part in [tags.get("addr:housenumber", ""), tags.get("addr:street", ""), tags.get("addr:city", "")] if part
    )
    return {
        "name": name[:255],
        "category": tag.split("=", 1)[1].replace("_", " "),
        "city": city,
        "address": address[:255],
        "phone": (_first(tags, "contact:whatsapp") or _first(tags, "phone", "contact:phone", "contact:mobile"))[:60],
        "whatsapp_confirmed": bool(_first(tags, "contact:whatsapp")),
        "socials": {
            network: url
            for network, keys in OSM_SOCIAL_TAGS.items()
            if (url := social_url(network, _first(tags, *keys)))
        },
        "website": website[:500],
        "email": _first(tags, "email", "contact:email")[:254],
        "source": "osm",
        "source_id": f"osm:{element.get('type')}/{element.get('id')}",
    }


def discover(city: str, tag: str, limit: int = 60, market: Market | None = None, contactable_only: bool | None = None) -> int:
    if contactable_only is None:
        contactable_only = AgentConfig.get().only_contactable
    area_id = cached_area_id(city, market)
    if not area_id:
        log(f"Couldn't find the area for '{city}' on OpenStreetMap.", "warning")
        return 0
    result = run_overpass(overpass_query(area_id, tag, limit, contactable_only))
    created = 0
    for element in result.get("elements", []):
        data = parse_element(element, city, tag)
        if not data or Lead.objects.filter(source_id=data["source_id"]).exists():
            continue
        if Lead.objects.filter(name__iexact=data["name"], city__iexact=city).exists():
            continue
        _create_lead(data, market)
        created += 1
    log(f"Found {created} new businesses for {tag} in {city}.", "success" if created else "info")
    return created


def _create_lead(data: dict, market: Market | None):
    """Save a found business; an email from the listing counts only if its domain can receive mail."""
    from django.utils import timezone

    from .research import usable_email

    if data.get("email") and not usable_email(data["email"]):
        data["email"] = ""
    lead = Lead(market=market, **data)
    if lead.email:
        lead.email_found_at = timezone.now()
    lead.save()
    return lead


def search_plan(config: AgentConfig) -> list[tuple[Market, str, str]]:
    """Every (market, city, sector) combination, interleaved so each country and sector gets a turn
    instead of exhausting one city first."""
    markets = list(Market.objects.filter(enabled=True))
    longest = max((len(m.city_list) for m in markets), default=0)
    tags = config.categories
    plan = []
    for i in range(longest):
        for t in range(len(tags)):
            for m, market in enumerate(markets):
                cities = market.city_list
                if i < len(cities) and tags:
                    # Shift the sector per country so back-to-back searches cover different
                    # countries AND different sectors, not one sector everywhere.
                    plan.append((market, cities[i], tags[(t + m) % len(tags)]))
    return plan


POOR_SECTOR_MIN_SCANNED = 30
POOR_SECTOR_RATE = 0.08


def sector_email_rates() -> dict:
    """Share of scanned businesses with an email, per sector (category word)."""
    from django.db.models import Count, Q

    rows = (
        Lead.objects.filter(kind="business", contacts_checked_at__isnull=False)
        .values("category")
        .annotate(scanned=Count("id"), with_email=Count("id", filter=~Q(email="")))
    )
    return {r["category"]: (r["scanned"], r["with_email"] / r["scanned"]) for r in rows if r["scanned"]}


SHARE_MIN_LEADS = 50


def sector_shares(days: int = 7) -> tuple[dict, int]:
    """How many business leads each sector produced recently, and the total."""
    import datetime

    from django.db.models import Count
    from django.utils import timezone

    since = timezone.now() - datetime.timedelta(days=days)
    rows = Lead.objects.filter(kind="business", created_at__gte=since).values("category").annotate(n=Count("id"))
    counts = {r["category"]: r["n"] for r in rows}
    return counts, sum(counts.values())


def is_crowded_sector(tag: str, shares: tuple[dict, int], max_share: int) -> bool:
    from . import foursquare

    counts, total = shares
    if total < SHARE_MIN_LEADS or max_share >= 100:
        return False
    n = sum(counts.get(c, 0) for c in {tag.split("=", 1)[-1].replace("_", " "), foursquare.search_words(tag)})
    return n / total > max_share / 100


def is_poor_sector(tag: str, rates: dict) -> bool:
    from . import foursquare

    for category in {tag.split("=", 1)[-1].replace("_", " "), foursquare.search_words(tag)}:
        scanned, rate = rates.get(category, (0, 1.0))
        if scanned >= POOR_SECTOR_MIN_SCANNED and rate < POOR_SECTOR_RATE:
            return True
    return False


def discover_next(config: AgentConfig | None = None, limit: int = 60) -> int:
    """Search the next country/city/sector in rotation, on Foursquare (when set up and within the
    monthly free allowance) and on OpenStreetMap."""
    from . import foursquare

    config = config or AgentConfig.get()
    plan = search_plan(config)
    if not plan:
        log("Enable at least one market (Markets page) and add sectors in Settings so the agent can find businesses.", "warning")
        return 0
    rates = sector_email_rates() if config.skip_poor_sectors else {}
    shares = sector_shares()
    # Pick the next search in turn, preferring (1) sectors that show emails and aren't already
    # over-represented, then (2) any sector that shows emails, then (3) simply the next one.
    order = [(config.discovery_cursor + i) % len(plan) for i in range(len(plan))]
    good = [i for i in order if not (rates and is_poor_sector(plan[i][2], rates))]
    balanced = [i for i in good if not is_crowded_sector(plan[i][2], shares, config.max_sector_share)]
    config.discovery_cursor = (balanced or good or order)[0]
    market, city, tag = plan[config.discovery_cursor]
    found = 0
    if foursquare.available(config):
        try:
            found += foursquare.discover(city, tag, market)
        except (foursquare.FoursquareError, requests.RequestException) as exc:
            log(f"Foursquare: {exc}", "warning")
    found += discover(city, tag, limit, market=market, contactable_only=config.only_contactable)  # raises OverpassBusy -> same search is retried next time
    config.discovery_cursor = (config.discovery_cursor + 1) % len(plan)
    config.save(update_fields=["discovery_cursor"])
    return found
