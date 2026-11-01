import imaplib
import smtplib

from django.conf import settings
from django.core.management.base import BaseCommand

from agent.services import llm


class Command(BaseCommand):
    help = "Check that Claude Code and Gmail are set up correctly. Sends nothing."

    def ok(self, text):
        self.stdout.write(self.style.SUCCESS(f"  OK   {text}"))

    def bad(self, text):
        self.stdout.write(self.style.ERROR(f"  FAIL {text}"))

    def handle(self, *args, **options):
        self.stdout.write("Claude:")
        try:
            reply = llm.ask_json('Return {"ok": true}')
            self.ok(f"Claude answered ({reply})") if reply.get("ok") else self.bad(f"Unexpected reply: {reply}")
        except Exception as exc:
            self.bad(str(exc))

        self.stdout.write("Gmail:")
        if not settings.GMAIL_ADDRESS or not settings.GMAIL_APP_PASSWORD:
            self.bad("GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing from .env")
            return
        try:
            with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as smtp:
                smtp.login(settings.GMAIL_ADDRESS, settings.GMAIL_APP_PASSWORD)
            self.ok("can log in to send (SMTP)")
        except Exception as exc:
            self.bad(f"SMTP login: {exc}")
        try:
            imap = imaplib.IMAP4_SSL(settings.IMAP_HOST)
            imap.login(settings.GMAIL_ADDRESS, settings.GMAIL_APP_PASSWORD)
            imap.logout()
            self.ok("can read inbox (IMAP)")
        except Exception as exc:
            self.bad(f"IMAP login: {exc}")
