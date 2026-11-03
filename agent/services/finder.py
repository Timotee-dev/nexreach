"""The fast email finder: searching maps and scanning websites, with no Claude involved.

It runs in its own loop (every minute by default) next to the slower Claude check-ins, so finding
emails never waits behind research and drafting. Polite to the free map servers: at most one map
search per cycle, and it rests once today's email target is reached."""
import threading
import time

from django.db import close_old_connections, connection

from ..models import AgentConfig, Lead, log
from . import discovery, research
from .discovery import OverpassBusy

SCAN_BATCH = 40      # websites per cycle
SCAN_WORKERS = 16    # websites scanned at the same time
IDLE_SECONDS = 600   # how long to rest once today's target is met


def cycle(config: AgentConfig | None = None) -> dict:
    """One round: top up the scan queue with a map search if it's low, then scan a batch."""
    config = config or AgentConfig.get()
    if not config.agent_enabled:
        return {"paused": True}
    today = research.emails_found_today()
    if today >= config.daily_email_target:
        return {"idle": True, "emails_today": today}
    unscanned = (
        Lead.objects.filter(status=Lead.Status.NEW, kind="business", contacts_checked_at__isnull=True)
        .filter(config.focus_filter())
        .count()
    )
    searched = 0
    if unscanned < SCAN_BATCH:
        try:
            searched = discovery.discover_next(config)
        except OverpassBusy as exc:
            log(f"Email finder: {exc}", "warning")
        except Exception as exc:  # network trouble shouldn't stop the finder
            log(f"Email finder search failed: {exc.__class__.__name__}: {exc}", "warning")
    found = research.scan_contacts(config, limit=SCAN_BATCH, workers=SCAN_WORKERS)
    research.mark_chains(config)  # free: keep chain branches away from Claude
    return {"searched": searched, "emails_found": found, "emails_today": today + found}


def run_forever(stop: threading.Event, every_seconds: int = 60, out=None):
    """Loop until `stop` is set. Meant for a background thread inside run_agent."""
    while not stop.is_set():
        close_old_connections()
        try:
            result = cycle()
        except Exception as exc:
            log(f"Email finder error: {exc.__class__.__name__}: {exc}", "error")
            result = {}
        finally:
            connection.close()
        if out and result.get("emails_found"):
            out(f"[{time.strftime('%H:%M')}] Email finder: +{result['emails_found']} emails "
                f"({result['emails_today']} today)")
        wait = IDLE_SECONDS if result.get("idle") else every_seconds
        stop.wait(wait)
