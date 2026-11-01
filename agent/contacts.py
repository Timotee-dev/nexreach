"""Phone numbers and social accounts: cleaning, classifying and turning them into direct-message links.
No Django imports here, so both models and services can use it."""
import re
from urllib.parse import quote, urlparse

# ---------------------------------------------------------------- phones

def normalise_phone(raw: str, country_code: str = "234") -> str:
    """Return a clean international number like +2348031234567 or +17135551234, or ''."""
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    has_plus = raw.startswith("+") or raw.startswith("00")
    if raw.startswith("00"):
        digits = digits[2:]
    if not has_plus:
        if digits.startswith("0") and country_code != "1":
            digits = country_code + digits[1:]          # local format, e.g. 0803... -> 234803...
        elif country_code == "1" and len(digits) == 10:
            digits = "1" + digits                      # US/Canada 10-digit number
    if digits.startswith(country_code + "0") and country_code != "1":
        digits = country_code + digits[len(country_code) + 1:]  # +234 0803... -> +234803...
    if not 8 <= len(digits) <= 15:
        return ""
    return "+" + digits


# Mobile number prefixes (after the country code). Landlines in these countries rarely have WhatsApp.
MOBILE_PREFIXES = {
    "234": ("7", "8", "9"),        # Nigeria
    "44": ("7",),                  # UK
    "61": ("4",),                  # Australia
    "64": ("2",),                  # New Zealand
    "353": ("8",),                 # Ireland
    "971": ("5",),                 # UAE
}


def phone_kind(number: str, country_code: str, whatsapp_confirmed: bool = False) -> str:
    """'whatsapp' (seen on a WhatsApp link/listing), 'mobile', 'landline' or 'unknown' (US/Canada can't be told apart)."""
    if not number:
        return ""
    if whatsapp_confirmed:
        return "whatsapp"
    digits = normalise_phone(number, country_code).lstrip("+")
    if not digits:
        return "unknown"
    for code, prefixes in MOBILE_PREFIXES.items():
        if digits.startswith(code):
            return "mobile" if digits[len(code):].startswith(prefixes) else "landline"
    return "unknown"


def whatsapp_digits(number: str, country_code: str) -> str:
    return normalise_phone(number, country_code).lstrip("+")


# ---------------------------------------------------------------- social accounts

SOCIAL_PATTERNS = {
    "instagram": re.compile(r"(?:https?:)?//(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})/?(?:[?#]|$)", re.I),
    "facebook": re.compile(r"(?:https?:)?//(?:www\.|m\.|web\.)?(?:facebook|fb)\.com/((?:profile\.php\?id=\d+)|(?:pages/[^?#]+)|[A-Za-z0-9.\-]{3,80})/?(?:[?#]|$)", re.I),
    "linkedin": re.compile(r"(?:https?:)?//(?:[a-z]{2,3}\.)?linkedin\.com/((?:company|in|school)/[A-Za-z0-9\-_%.]+)/?(?:[?#]|$)", re.I),
    "x": re.compile(r"(?:https?:)?//(?:www\.|mobile\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})/?(?:[?#]|$)", re.I),
    "tiktok": re.compile(r"(?:https?:)?//(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]{2,30})/?(?:[?#]|$)", re.I),
}
# Path words that are site features, not a business's account.
NOT_ACCOUNTS = {
    "instagram": {"p", "reel", "reels", "explore", "accounts", "stories", "tv", "about", "developer", "direct"},
    "facebook": {"sharer", "sharer.php", "share", "share.php", "dialog", "plugins", "tr", "login", "events", "groups",
                 "help", "policies", "privacy", "watch", "marketplace", "home.php", "photo.php", "permalink.php"},
    "x": {"intent", "share", "home", "hashtag", "i", "search", "login", "signup", "privacy", "tos", "explore", "messages"},
    "tiktok": set(),
    "linkedin": set(),
}
CANONICAL = {
    "instagram": "https://www.instagram.com/{}/",
    "facebook": "https://www.facebook.com/{}",
    "linkedin": "https://www.linkedin.com/{}",
    "x": "https://x.com/{}",
    "tiktok": "https://www.tiktok.com/@{}",
}
LABELS = {"instagram": "Instagram", "facebook": "Facebook", "linkedin": "LinkedIn", "x": "X", "tiktok": "TikTok"}
# Channels in the order they're worth trying for a first message.
CHANNEL_ORDER = ["instagram", "facebook", "linkedin", "x", "tiktok"]


def social_url(network: str, value: str) -> str:
    """Accepts a handle, a full URL or an OSM value like '@shop' and returns the canonical profile URL, or ''."""
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value and not value.startswith("//"):
        handle = value.lstrip("@").strip("/")
        if network == "linkedin" and not handle.startswith(("company/", "in/", "school/")):
            handle = "company/" + handle
        value = CANONICAL[network].format(handle) if handle else ""
    found = find_socials(f'<a href="{value}">', only=network)
    return found.get(network, "")


def find_socials(html: str, only: str | None = None) -> dict:
    """First real account per network found in a page's links."""
    hrefs = re.findall(r"""href\s*=\s*["']([^"']+)["']""", html or "", re.I)
    result = {}
    for network, pattern in SOCIAL_PATTERNS.items():
        if only and network != only:
            continue
        for href in hrefs:
            match = pattern.search(href.strip())
            if not match:
                continue
            handle = match.group(1)
            first = handle.split("/")[0].lower()
            if first in NOT_ACCOUNTS[network]:
                continue
            result[network] = CANONICAL[network].format(handle.rstrip("/"))
            break
    return result


def dm_link(network: str, url: str) -> str:
    """Link that opens a direct-message thread where the platform allows it; otherwise the profile."""
    path = urlparse(url).path.strip("/")
    if network == "instagram" and path:
        return f"https://ig.me/m/{path}"
    if network == "facebook" and path and "/" not in path and not path.startswith("profile.php"):
        return f"https://m.me/{path}"
    return url


def whatsapp_url(digits: str, text: str = "") -> str:
    if not digits:
        return ""
    return f"https://wa.me/{digits}" + (f"?text={quote(text)}" if text else "")
