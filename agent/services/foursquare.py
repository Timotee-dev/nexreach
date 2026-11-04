"""Foursquare Places as a second business source.

Free tier: the first 500 "Pro" calls each month (search with default Pro fields: name,
location, categories, phone, website, email, social media). Each search returns up to 50
places. Set FOURSQUARE_API_KEY in .env; NexReach caps itself at the monthly limit in Settings.
"""
import requests
from django.conf import settings

from ..contacts import social_url
from ..models import ApiUsage, AgentConfig, Market, log

SEARCH_URL = "https://places-api.foursquare.com/places/search"
FIELDS = "fsq_place_id,name,location,categories,tel,website,email,social_media"
USAGE_KEY = "foursquare"

# OpenStreetMap sector tags -> plain search words Foursquare understands.
TAG_WORDS = {
    "office=lawyer": "law firm",
    "office=accountant": "accountant",
    "amenity=dentist": "dentist",
    "healthcare=dentist": "dentist",
    "amenity=clinic": "medical clinic",
    "amenity=doctors": "doctor",
    "office=estate_agent": "real estate agency",
    "office=financial_advisor": "financial advisor",
    "office=insurance": "insurance agency",
    "office=architect": "architect",
    "amenity=veterinary": "veterinarian",
    "craft=hvac": "HVAC contractor",
    "craft=plumber": "plumber",
    "craft=roofer": "roofing contractor",
    "craft=electrician": "electrician",
    "shop=car": "car dealership",
    "tourism=hotel": "hotel",
    "leisure=fitness_centre": "gym",
    "healthcare=physiotherapist": "physiotherapist",
    "healthcare=optometrist": "optometrist",
    "office=property_management": "property management",
    "office=consulting": "consulting firm",
    "office=employment_agency": "recruitment agency",
    "office=travel_agent": "travel agency",
    "amenity=school": "private school",
    "amenity=college": "college",
    "craft=carpenter": "carpenter",
    "shop=jewelry": "jewelry store",
    "shop=furniture": "furniture store",
    "tourism=guest_house": "guest house",
    "office=advertising_agency": "marketing agency",
    "office=web_design": "web design agency",
}


class FoursquareError(Exception):
    pass


def available(config: AgentConfig) -> bool:
    return bool(settings.FOURSQUARE_API_KEY) and ApiUsage.used(USAGE_KEY) < config.foursquare_monthly_limit


def search_words(tag: str) -> str:
    return TAG_WORDS.get(tag, tag.split("=", 1)[-1].replace("_", " "))


def search(city: str, tag: str, limit: int = 50) -> list[dict]:
    resp = requests.get(
        SEARCH_URL,
        params={"query": search_words(tag), "near": city, "limit": min(limit, 50), "fields": FIELDS},
        headers={
            "Authorization": f"Bearer {settings.FOURSQUARE_API_KEY}",
            "X-Places-Api-Version": settings.FOURSQUARE_API_VERSION,
            "Accept": "application/json",
        },
        timeout=30,
    )
    ApiUsage.add(USAGE_KEY)
    if resp.status_code in (401, 403):
        raise FoursquareError("Foursquare rejected the API key. Check FOURSQUARE_API_KEY in .env.")
    if resp.status_code == 429:
        raise FoursquareError("Foursquare limit reached for now; using OpenStreetMap instead.")
    resp.raise_for_status()
    return resp.json().get("results", [])


def to_lead(place: dict, city: str, tag: str) -> dict | None:
    name = (place.get("name") or "").strip()
    place_id = place.get("fsq_place_id") or place.get("fsq_id")
    if not name or not place_id:
        return None
    location = place.get("location") or {}
    social = place.get("social_media") or {}
    socials = {}
    for network, raw in (("instagram", social.get("instagram")), ("x", social.get("twitter")),
                         ("facebook", social.get("facebook_id") or social.get("facebook"))):
        url = social_url(network, str(raw)) if raw else ""
        if url:
            socials[network] = url
    website = (place.get("website") or "").strip()
    if website and not website.startswith(("http://", "https://")):
        website = "http://" + website
    return {
        "name": name[:255],
        "category": search_words(tag),
        "city": city,
        "address": (location.get("formatted_address") or location.get("address") or "")[:255],
        "phone": (place.get("tel") or "")[:60],
        "website": website[:500],
        "email": (place.get("email") or "")[:254],
        "socials": socials,
        "source": "foursquare",
        "source_id": f"fsq:{place_id}",
    }


def discover(city: str, tag: str, market: Market | None, limit: int = 50, run=None) -> int:
    from ..models import Lead

    created = 0
    for place in search(city, tag, limit):
        data = to_lead(place, city, tag)
        if not data or Lead.objects.filter(source_id=data["source_id"]).exists():
            continue
        if Lead.objects.filter(name__iexact=data["name"], city__iexact=city).exists():
            continue  # already found on OpenStreetMap
        from .discovery import _create_lead

        _create_lead(data, market, run)
        created += 1
    log(f"Found {created} new businesses on Foursquare for {search_words(tag)} in {city}.", "success" if created else "info")
    return created
