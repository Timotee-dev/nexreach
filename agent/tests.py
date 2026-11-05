"""Tests run with Claude, Gmail and the web mocked out: python manage.py test agent"""
from datetime import timedelta
from email.message import EmailMessage
from unittest import mock

from django.contrib.auth.models import User
from django.db.models import Count
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Activity, AgentConfig, Lead, Message, Suppression
from .services import discovery, inbox, llm, loop, mailer, research, writer

GMAIL = dict(GMAIL_ADDRESS="me@gmail.com", GMAIL_APP_PASSWORD="abcd")


def fake_claude(**answers):
    """One fake for all Claude calls; picks an answer by what the prompt is for."""
    def respond(prompt, model=""):
        if "Classify this reply" in prompt:
            return answers.get("classify", {"category": "other", "summary": ""})
        if "You qualify sales leads" in prompt:
            return answers.get("qualify", {"fit_score": 80, "summary": "s", "hook": "h", "fit_reason": "r"})
        return answers.get("write", {"subject": "s", "body": "b"})
    return mock.patch("agent.services.llm.ask_json", side_effect=respond)


class FakeResp:
    def __init__(self, text, url="https://shop.example.ng/", status=200):
        self.text, self.url, self.status_code = text, url, status


SITE = """<html><head><title>Ada Fashion</title></head><body>
<h1>Ada Fashion House</h1><p>Ankara and bridal wear in Ikeja.</p>
<a href="mailto:hello@adafashion.ng">Email us</a><footer>© 2019 Ada Fashion</footer></body></html>"""


class LLMTests(TestCase):
    def test_extract_json_handles_fences_and_chatter(self):
        self.assertEqual(llm.extract_json('Sure!\n```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(llm.extract_json('text {"b": [1, 2]} trailing'), {"b": [1, 2]})
        with self.assertRaises(llm.LLMError):
            llm.extract_json("no json here")

    @mock.patch("agent.services.llm.subprocess.run")
    def test_claude_cli_parses_result(self, run):
        run.return_value = mock.Mock(returncode=0, stdout='{"type":"result","is_error":false,"result":"{\\"ok\\": true}"}', stderr="")
        self.assertEqual(llm.ask_json("hi"), {"ok": True})
        args = run.call_args[0][0]
        self.assertIn("-p", args)
        self.assertIn("--output-format", args)

    @mock.patch("agent.services.llm.subprocess.run")
    def test_claude_cli_error_raises(self, run):
        run.return_value = mock.Mock(returncode=1, stdout='{"is_error":true,"result":"not logged in"}', stderr="")
        with self.assertRaises(llm.LLMError):
            llm.ask("hi")


class DiscoveryTests(TestCase):
    def test_parse_element(self):
        el = {"type": "node", "id": 5, "tags": {"name": "Mama Put", "website": "mamaput.ng", "contact:phone": "08031234567;0809"}}
        data = discovery.parse_element(el, "Lagos", "amenity=restaurant")
        self.assertEqual(data["website"], "http://mamaput.ng")
        self.assertEqual(data["phone"], "08031234567")
        self.assertEqual(data["source_id"], "osm:node/5")
        self.assertIsNone(discovery.parse_element({"tags": {}}, "Lagos", "amenity=restaurant"))

    @mock.patch("agent.services.discovery.requests")
    def test_discover_creates_and_dedupes(self, req):
        nominatim = mock.Mock(**{"json.return_value": [
            {"osm_type": "node", "osm_id": 9, "category": "shop", "type": "clothes"},  # a shop named "Lagos": skipped
            {"osm_type": "relation", "osm_id": 7, "category": "boundary", "addresstype": "city", "importance": 0.2},
            {"osm_type": "relation", "osm_id": 123, "category": "boundary", "addresstype": "city", "importance": 0.8},
        ]})
        overpass = mock.Mock(**{"json.return_value": {"elements": [
            {"type": "node", "id": 1, "tags": {"name": "A Shop"}},
            {"type": "node", "id": 2, "tags": {"name": "B Shop", "email": "b@b.ng"}},
        ]}})
        overpass.status_code = 200
        req.get.return_value, req.post.return_value = nominatim, overpass
        self.assertEqual(discovery.discover("Lagos", "shop=clothes"), 2)
        self.assertEqual(discovery.discover("Lagos", "shop=clothes"), 0)  # no duplicates
        self.assertIn("3600000123", req.post.call_args.kwargs["data"]["data"])

    def test_rotation_across_markets(self):
        from .models import Market
        config = AgentConfig.get()
        config.target_categories = "office=lawyer\namenity=dentist"
        config.save()
        Market.objects.update(enabled=False)
        Market.objects.filter(name__in=["United States", "United Kingdom"]).update(enabled=True)
        plan = discovery.search_plan(config)
        first = [(m.name, city.split(",")[0], tag) for m, city, tag in plan[:4]]
        self.assertEqual(first, [  # back-to-back searches differ in country AND sector
            ("United States", "New York", "office=lawyer"), ("United Kingdom", "London", "amenity=dentist"),
            ("United States", "New York", "amenity=dentist"), ("United Kingdom", "London", "office=lawyer"),
        ])
        with mock.patch.object(discovery, "discover", return_value=0) as d:
            discovery.discover_next(config)
            discovery.discover_next(config)
        self.assertEqual([c.kwargs["market"].name for c in d.call_args_list], ["United States", "United Kingdom"])


class ResearchTests(TestCase):
    def test_find_emails_filters_junk(self):
        html = '<a href="mailto:Info@Shop.ng?subject=hi">x</a> sales@shop.ng icon@2x.png user@example.com'
        self.assertEqual(research.find_emails(html, "shop.ng"), ["info@shop.ng", "sales@shop.ng"])

    def test_signals(self):
        signals = research.site_signals(FakeResp(SITE, url="http://ada.ng/"), 6.0, SITE)
        text = " ".join(signals)
        self.assertIn("HTTPS", text)
        self.assertIn("mobile", text)
        self.assertIn("2019", text)
        self.assertIn("6s", text)

    @mock.patch("agent.services.research.llm.ask_json")
    @mock.patch("agent.services.research.fetch")
    def test_research_qualifies_and_finds_email(self, fetch, ask):
        fetch.return_value = (FakeResp(SITE), 1.0)
        ask.return_value = {"fit_score": 82, "fit_reason": "Old site", "summary": "Fashion house.", "hook": "Footer says 2019."}
        lead = Lead.objects.create(name="Ada Fashion", website="https://shop.example.ng")
        research.research_lead(lead)
        lead.refresh_from_db()
        self.assertEqual(lead.status, Lead.Status.QUALIFIED)
        self.assertEqual(lead.email, "hello@adafashion.ng")
        self.assertEqual(lead.fit_score, 82)
        self.assertIn("2019", ask.call_args[0][0])  # signals went into the prompt

    @mock.patch("agent.services.research.llm.ask_json", return_value={"fit_score": 90, "summary": "", "hook": "", "fit_reason": ""})
    def test_no_website_no_email_goes_to_call_list(self, _):
        lead = Lead.objects.create(name="Buka", phone="08031234567")
        research.research_lead(lead)
        self.assertEqual(lead.status, Lead.Status.NO_EMAIL)
        self.assertEqual(lead.whatsapp_link, "https://wa.me/2348031234567")

    @mock.patch("agent.services.research.llm.ask_json", return_value={"fit_score": 20})
    def test_low_score_disqualified(self, _):
        lead = Lead.objects.create(name="Bank", email="x@bank.ng")
        research.research_lead(lead)
        self.assertEqual(lead.status, Lead.Status.DISQUALIFIED)


class WriterTests(TestCase):
    @mock.patch("agent.services.writer.llm.ask_json", return_value={"subject": "your 2019 footer", "body": "Hi Ada team, ..."})
    def test_initial_draft_has_footer_and_is_draft(self, _):
        lead = Lead.objects.create(name="Ada", email="a@a.ng", status=Lead.Status.QUALIFIED)
        msg = writer.draft_initial(lead)
        config = AgentConfig.get()
        self.assertEqual(msg.status, Message.Status.DRAFT)
        self.assertIn(config.opt_out_line, msg.body)
        self.assertIn("Nexora from Nexlab", msg.body)
        self.assertEqual(lead.status, Lead.Status.DRAFTED)

    @mock.patch("agent.services.writer.llm.ask_json", return_value={"body": "Following up"})
    def test_followup_threads(self, _):
        lead = Lead.objects.create(name="Ada", email="a@a.ng", status=Lead.Status.CONTACTED)
        Message.objects.create(lead=lead, direction="out", kind="initial", status="sent", subject="quick idea", message_id="<1@x>")
        msg = writer.draft_followup(lead)
        self.assertEqual(msg.subject, "Re: quick idea")
        self.assertEqual(msg.in_reply_to, "<1@x>")


@override_settings(**GMAIL)
class MailerTests(TestCase):
    def setUp(self):
        self.lead = Lead.objects.create(name="Ada", email="a@a.ng", status=Lead.Status.DRAFTED)
        config = AgentConfig.get()
        config.send_hour_start, config.send_hour_end, config.send_weekdays_only = 0, 24, False  # always open
        config.save()

    def approved(self, kind="initial"):
        return Message.objects.create(lead=self.lead, direction="out", kind=kind, status="approved", subject="hi", body="b")

    @mock.patch("agent.services.mailer.smtplib.SMTP_SSL")
    def test_only_approved_are_sent(self, smtp_cls):
        Message.objects.create(lead=self.lead, direction="out", kind="initial", status="draft", subject="d", body="d")
        msg = self.approved()
        self.assertEqual(mailer.send_approved(), 1)
        msg.refresh_from_db(); self.lead.refresh_from_db()
        self.assertEqual(msg.status, Message.Status.SENT)
        self.assertTrue(msg.message_id)
        self.assertEqual(self.lead.status, Lead.Status.CONTACTED)
        smtp = smtp_cls.return_value.__enter__.return_value
        self.assertEqual(smtp.send_message.call_count, 1)

    @mock.patch("agent.services.mailer.smtplib.SMTP_SSL")
    def test_daily_limit(self, smtp_cls):
        config = AgentConfig.get(); config.daily_send_limit = 2; config.save()
        for i in range(4):
            lead = Lead.objects.create(name=f"L{i}", email=f"l{i}@x.ng")
            Message.objects.create(lead=lead, direction="out", kind="initial", status="approved", subject="s", body="b")
        self.assertEqual(mailer.send_approved(), 2)
        self.assertEqual(mailer.send_approved(), 0)  # limit reached for today
        self.assertEqual(Message.objects.filter(status="approved").count(), 2)

    @mock.patch("agent.services.mailer.smtplib.SMTP_SSL")
    def test_suppressed_never_sent(self, smtp_cls):
        Suppression.objects.create(email="A@a.ng")
        msg = self.approved()
        mailer.send_approved()
        msg.refresh_from_db()
        self.assertEqual(msg.status, Message.Status.REJECTED)
        self.assertFalse(smtp_cls.return_value.__enter__.return_value.send_message.called)


def raw_email(sender, body, in_reply_to="", msg_id="<reply1@mail.com>"):
    em = EmailMessage()
    em["From"], em["To"], em["Subject"], em["Message-ID"] = sender, "me@gmail.com", "Re: quick idea", msg_id
    if in_reply_to:
        em["In-Reply-To"] = in_reply_to
    em.set_content(body)
    return em.as_bytes()


class InboxTests(TestCase):
    def setUp(self):
        self.lead = Lead.objects.create(name="Ada", email="a@a.ng", status=Lead.Status.CONTACTED)
        Message.objects.create(lead=self.lead, direction="out", kind="initial", status="sent", subject="quick idea",
                               body="hello", message_id="<orig@gmail.com>", sent_at=timezone.now())

    @fake_claude(classify={"category": "interested", "summary": "Wants a price"}, write={"body": "Great — here's a time."})
    def test_interested_reply_drafts_answer(self, _):
        pending = Message.objects.create(lead=self.lead, direction="out", kind="followup", status="draft", subject="Re", body="x")
        result = inbox.process_raw(raw_email("Ada <a@a.ng>", "How much?\n\nOn Mon, You wrote:\n> hello", "<orig@gmail.com>"))
        self.assertEqual(result, "interested")
        self.lead.refresh_from_db(); pending.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.INTERESTED)
        self.assertEqual(pending.status, Message.Status.REJECTED)  # follow-up cancelled
        inbound = self.lead.messages.get(direction="in")
        self.assertEqual(inbound.body, "How much?")
        reply = self.lead.messages.get(kind="reply")
        self.assertEqual(reply.status, Message.Status.DRAFT)
        self.assertEqual(reply.in_reply_to, "<reply1@mail.com>")
        # Same email again is ignored
        self.assertIsNone(inbox.process_raw(raw_email("Ada <a@a.ng>", "How much?", "<orig@gmail.com>")))

    @mock.patch("agent.services.inbox.llm.ask_json", return_value={"category": "unsubscribe", "summary": "stop"})
    def test_unsubscribe(self, _):
        inbox.process_raw(raw_email("a@a.ng", "no thanks"))
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.UNSUBSCRIBED)
        self.assertTrue(Suppression.blocked("a@a.ng"))

    def test_bounce(self):
        inbox.process_raw(raw_email("mailer-daemon@googlemail.com", "Delivery to a@a.ng failed", msg_id="<b@g>"))
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.BOUNCED)

    def test_unrelated_mail_ignored(self):
        self.assertIsNone(inbox.process_raw(raw_email("friend@x.com", "hey")))


class LoopTests(TestCase):
    @mock.patch("agent.services.loop.discovery.discover_next", return_value=3)
    @mock.patch("agent.services.loop.inbox.check_inbox", side_effect=mailer.MailConfigError("no gmail"))
    @mock.patch("agent.services.loop.mailer.send_approved", return_value=0)
    @fake_claude()
    def test_tick_runs_pipeline_and_survives_errors(self, *_):
        Lead.objects.create(name="New", email="n@n.ng")
        results = loop.tick()
        self.assertEqual(results["researched"], 1)
        self.assertEqual(results["drafted"], 1)
        self.assertEqual(results["discovered"], 18)  # behind the daily email target: 6 searches
        self.assertTrue(Message.objects.filter(status="draft").exists())
        self.assertFalse(Message.objects.filter(status="sent").exists())  # nothing sent without approval

    @mock.patch("agent.services.writer.llm.ask_json", return_value={"body": "f"})
    def test_followups_due(self, _):
        config = AgentConfig.get()
        lead = Lead.objects.create(name="Old", email="o@o.ng", status=Lead.Status.CONTACTED,
                                   last_contacted_at=timezone.now() - timedelta(days=10))
        Message.objects.create(lead=lead, direction="out", kind="initial", status="sent", subject="hi", message_id="<m>")
        self.assertEqual(loop.queue_followups(config), 1)
        self.assertEqual(loop.queue_followups(config), 0)  # already has a pending draft

    def test_paused(self):
        config = AgentConfig.get(); config.agent_enabled = False; config.save()
        self.assertEqual(loop.tick(), {"paused": True})


class ViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("t", "t@t.com", "pw")
        self.client.force_login(self.user)
        self.lead = Lead.objects.create(name="Ada", email="a@a.ng", status=Lead.Status.DRAFTED, fit_score=80,
                                        site_signals=["No HTTPS"], researched_at=timezone.now())
        self.msg = Message.objects.create(lead=self.lead, direction="out", kind="initial", status="draft", subject="s", body="b")

    def test_pages_render(self):
        for name in ["dashboard", "outbox", "leads", "call_list", "settings", "activity", "lead_add"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertContains(self.client.get(reverse("lead_detail", args=[self.lead.pk])), "No HTTPS")

    def test_login_required(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)

    def test_edit_and_approve(self):
        self.client.post(reverse("message_action", args=[self.msg.pk]), {"action": "approve", "subject": "new", "body": "edited"})
        self.msg.refresh_from_db()
        self.assertEqual((self.msg.status, self.msg.subject, self.msg.body), ("approved", "new", "edited"))

    def test_reject(self):
        self.client.post(reverse("message_action", args=[self.msg.pk]), {"action": "reject"})
        self.msg.refresh_from_db(); self.lead.refresh_from_db()
        self.assertEqual(self.msg.status, "rejected")
        self.assertEqual(self.lead.status, Lead.Status.DISQUALIFIED)

    def test_add_lead_and_settings(self):
        r = self.client.post(reverse("lead_add"), {"name": "Foreign Co", "website": "https://foreign.co", "email": "x@foreign.co"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Lead.objects.filter(name="Foreign Co", status="new").exists())
        data = {f.name: getattr(AgentConfig.get(), f.name) for f in AgentConfig._meta.fields if f.name not in ("id", "updated_at", "discovery_cursor", "gigs_checked_at", "remotive_checked_at", "gig_source_pauses", "claude_paused_until")}
        data["daily_send_limit"] = 12
        data["agent_enabled"] = "on"
        self.assertEqual(self.client.post(reverse("settings"), data).status_code, 302)
        self.assertEqual(AgentConfig.get().daily_send_limit, 12)


class PhoneTests(TestCase):
    def test_find_phones(self):
        html = """<a href="https://wa.me/2348031234567">Chat</a> <a href="tel:+44 20 7946 0958">UK</a>
        Call 0803 123 4567 or 0909-876-5432. Ref 2024000123456789 order 12345."""
        phones = research.find_phones(html)
        self.assertEqual(phones[0], "+2348031234567")  # WhatsApp first, duplicate removed
        self.assertIn("+442079460958", phones)
        self.assertIn("+2349098765432", phones)
        self.assertEqual(len(phones), 3)

    @mock.patch("agent.services.research.llm.ask_json", return_value={"fit_score": 80})
    @mock.patch("agent.services.research.fetch")
    def test_research_fills_phone(self, fetch, _):
        fetch.return_value = (FakeResp("<p>Call us: +234 803 123 4567</p>"), 1.0)
        lead = Lead.objects.create(name="Shop", website="https://shop.example.ng")
        research.research_lead(lead)
        self.assertEqual(lead.phone, "+2348031234567")
        self.assertEqual(lead.whatsapp_link, "https://wa.me/2348031234567")
        self.assertEqual(lead.status, Lead.Status.NO_EMAIL)

    @mock.patch("agent.services.research.fetch")
    def test_refresh_contacts_command(self, fetch):
        from django.core.management import call_command
        fetch.return_value = (FakeResp('<a href="mailto:hi@shop.ng">x</a> 08031234567'), 1.0)
        lead = Lead.objects.create(name="Shop", website="https://shop.ng", status=Lead.Status.NO_EMAIL)
        call_command("refresh_contacts")
        lead.refresh_from_db()
        self.assertEqual((lead.email, lead.phone, lead.status), ("hi@shop.ng", "+2348031234567", Lead.Status.QUALIFIED))



@override_settings(**GMAIL)
class GlobalMarketTests(TestCase):
    def setUp(self):
        from .models import Market
        self.us = Market.objects.get(name="United States")
        self.uk = Market.objects.get(name="United Kingdom")
        self.ng = Market.objects.get(name="Nigeria")

    def test_markets_seeded(self):
        from .models import Market
        self.assertEqual(Market.objects.count(), 29)
        self.assertEqual(Market.objects.filter(enabled=True).count(), 21)
        self.assertFalse(Market.objects.get(name="Nigeria").enabled)
        self.assertTrue(Market.objects.get(name="France").enabled)
        self.assertTrue(self.us.enabled and self.uk.enabled)
        self.assertFalse(self.ng.enabled)
        self.assertTrue(Market.objects.get(name="Canada").enabled)
        self.assertFalse(Market.objects.get(name="Germany").enabled)
        self.assertEqual(Market.objects.get(name="Germany").language, "German")
        self.assertIn("San Francisco, United States", Market.objects.get(name="United States").city_list)
        self.assertIn("London, United Kingdom", self.uk.city_list)

    def test_phones_by_country(self):
        html = '<a href="tel:+1 (713) 555-0142">Call</a> Office: (281) 555-0199.'
        self.assertEqual(research.find_phones(html, "1")[:2], ["+17135550142", "+12815550199"])
        self.assertIn("+442079460958", research.find_phones("Call 020 7946 0958 today", "44"))
        self.assertEqual(research.normalise_phone("0803 123 4567", "234"), "+2348031234567")
        self.assertEqual(research.normalise_phone("+234 0803 123 4567", "234"), "+2348031234567")
        lead = Lead.objects.create(name="Firm", market=self.uk)
        self.assertEqual(lead.calling_code, "44")

    def test_send_window_uses_each_business_time_zone(self):
        import datetime
        config = AgentConfig.get()  # 9:00-16:00 weekdays by default
        utc = datetime.timezone.utc
        # Tuesday 15:00 UTC = 15:00 London (open) and 09:00 Chicago (open)
        # Tuesday 21:00 UTC = 21:00 London (closed) and 15:00 Chicago (open)
        us_lead = Lead.objects.create(name="US", market=self.us)
        uk_lead = Lead.objects.create(name="UK", market=self.uk)
        with mock.patch("django.utils.timezone.now", return_value=datetime.datetime(2026, 11, 3, 21, 0, tzinfo=utc)):
            self.assertTrue(config.within_send_window(config.lead_local_now(us_lead)))
            self.assertFalse(config.within_send_window(config.lead_local_now(uk_lead)))
        with mock.patch("django.utils.timezone.now", return_value=datetime.datetime(2026, 11, 7, 15, 0, tzinfo=utc)):
            self.assertFalse(config.within_send_window(config.lead_local_now(uk_lead)))  # Saturday

    @mock.patch("agent.services.mailer.smtplib.SMTP_SSL")
    def test_mixed_queue(self, smtp_cls):
        import datetime
        config = AgentConfig.get()
        config.postal_address = "12 Example Street, Akure, Nigeria"
        config.save()
        us = Lead.objects.create(name="US Firm", email="a@us.com", market=self.us)
        uk = Lead.objects.create(name="UK Firm", email="b@uk.co.uk", market=self.uk)
        replied = Lead.objects.create(name="UK replied", email="c@uk.co.uk", market=self.uk)
        m_us = Message.objects.create(lead=us, direction="out", kind="initial", status="approved", subject="s", body="b")
        m_uk = Message.objects.create(lead=uk, direction="out", kind="initial", status="approved", subject="s", body="b")
        m_reply = Message.objects.create(lead=replied, direction="out", kind="reply", status="approved", subject="Re", body="b")
        evening = datetime.datetime(2026, 11, 3, 21, 0, tzinfo=datetime.timezone.utc)  # open in US, closed in UK
        with mock.patch("django.utils.timezone.now", return_value=evening):
            self.assertEqual(mailer.send_approved(), 2)
        for m in (m_us, m_uk, m_reply):
            m.refresh_from_db()
        self.assertEqual((m_us.status, m_uk.status, m_reply.status), ("sent", "approved", "sent"))

    @mock.patch("agent.services.mailer.smtplib.SMTP_SSL")
    def test_postal_address_required_for_market(self, smtp_cls):
        config = AgentConfig.get()
        config.send_hour_start, config.send_hour_end, config.send_weekdays_only = 0, 24, False
        config.save()
        lead = Lead.objects.create(name="US Firm", email="a@us.com", market=self.us)
        Message.objects.create(lead=lead, direction="out", kind="initial", status="approved", subject="s", body="b")
        self.assertEqual(mailer.send_approved(), 0)
        mailer.send_approved()
        self.assertEqual(Activity.objects.filter(text__startswith="Cold emails to some countries are on hold").count(), 1)
        config.postal_address = "12 Example Street, Akure, Nigeria"
        config.save()
        self.assertEqual(mailer.send_approved(), 1)

    def test_footer_and_unsubscribe_header(self):
        config = AgentConfig.get()
        config.postal_address = "12 Example Street, Akure, Nigeria"
        lead = Lead.objects.create(name="Firm", email="c@firm.com", market=self.us)
        self.assertIn("12 Example Street", writer.footer(config))
        em = mailer.build(Message(lead=lead, subject="s", body="b"), config)
        self.assertEqual(em["List-Unsubscribe"], "<mailto:me@gmail.com?subject=unsubscribe>")

    def test_unsubscribe_subject_needs_no_claude(self):
        lead = Lead.objects.create(name="Firm", email="c@firm.com", status=Lead.Status.CONTACTED, market=self.us)
        em = EmailMessage()
        em["From"], em["Subject"], em["Message-ID"] = "c@firm.com", "unsubscribe", "<u1@x>"
        em.set_content("")
        with mock.patch("agent.services.llm.ask_json") as ask:
            inbox.process_raw(em.as_bytes())
            ask.assert_not_called()
        lead.refresh_from_db()
        self.assertEqual(lead.status, Lead.Status.UNSUBSCRIBED)

    def test_presets_switch_markets_and_focus(self):
        from django.core.management import call_command
        ng = Lead.objects.create(name="Lagos shop", source="osm", market=self.ng, email="x@x.ng", status="qualified")
        us = Lead.objects.create(name="NY Law", source="osm", market=self.us, email="s@d.com", status="qualified")
        manual = Lead.objects.create(name="Picked by hand", email="m@t.ca", status="qualified")
        call_command("apply_preset", "global", stdout=mock.Mock())
        config = AgentConfig.get()
        self.assertIn("office=lawyer", config.categories)
        self.assertIn("$", config.offer)
        focus = set(Lead.objects.filter(config.focus_filter()).values_list("name", flat=True))
        self.assertEqual(focus, {"NY Law", "Picked by hand"})
        call_command("apply_preset", "nigeria", stdout=mock.Mock())
        config = AgentConfig.get()
        focus = set(Lead.objects.filter(config.focus_filter()).values_list("name", flat=True))
        self.assertEqual(focus, {"Lagos shop", "Picked by hand"})
        self.assertIn("₦", config.offer)

    @mock.patch("agent.services.writer.llm.ask_json", return_value={"subject": "s", "body": "b"})
    def test_writer_gets_country(self, ask):
        lead = Lead.objects.create(name="Firm", email="c@firm.com", market=self.uk, category="lawyer", city="Leeds, United Kingdom")
        writer.draft_initial(lead)
        prompt = ask.call_args[0][0]
        self.assertIn("United Kingdom", prompt)
        self.assertIn("pick the ONE service", prompt)

    def test_pages(self):
        user = User.objects.create_superuser("t", "t@t.com", "pw")
        self.client.force_login(user)
        r = self.client.get(reverse("markets"))
        self.assertContains(r, "United Kingdom")
        import datetime
        sydney = self.us.__class__.objects.get(name="Australia").local_now().strftime("%a %H:%M")
        self.assertContains(r, f"{sydney} there")
        self.assertLess(r.content.find(b"United States"), r.content.find(b"Australia"))  # enabled first
        self.assertContains(r, "Add your postal address")
        self.client.post(reverse("markets"), {"market": self.ng.pk, "enabled": "1"})
        self.ng.refresh_from_db()
        self.assertTrue(self.ng.enabled)
        r = self.client.get(reverse("settings"))
        self.assertContains(r, "Sending &amp; compliance")
        self.assertContains(r, "Sectors to search")
        data = {f.name: getattr(AgentConfig.get(), f.name) for f in AgentConfig._meta.fields if f.name not in ("id", "updated_at", "discovery_cursor", "gigs_checked_at", "remotive_checked_at", "gig_source_pauses", "claude_paused_until")}
        for key in [k for k, v in data.items() if isinstance(v, bool)]:
            data[key] = "on" if data[key] else ""
        data["home_timezone"] = "Mars/Olympus"
        r = self.client.post(reverse("settings"), data)
        self.assertContains(r, "isn&#x27;t a time zone name")
        self.assertContains(self.client.get(reverse("outbox")), "Cold emails go out")
        self.assertEqual(self.client.get(reverse("lead_add")).status_code, 200)



class OverpassFallbackTests(TestCase):
    @mock.patch("agent.services.discovery.requests.post")
    def test_falls_back_to_next_server(self, post):
        busy = mock.Mock(status_code=502)
        ok = mock.Mock(status_code=200, **{"json.return_value": {"elements": []}})
        post.side_effect = [busy, ok]
        self.assertEqual(discovery.run_overpass("q"), {"elements": []})
        self.assertIn("kumi", post.call_args_list[1].args[0])

    @mock.patch("agent.services.discovery.requests.post", return_value=mock.Mock(status_code=504))
    @mock.patch("agent.services.discovery.city_area_id", return_value=1)
    def test_all_busy_moves_on_to_next_search(self, *_):
        config = AgentConfig.get()
        before = config.discovery_cursor
        with self.assertRaises(discovery.OverpassBusy):
            discovery.discover_next(config)
        config.refresh_from_db()
        self.assertNotEqual(config.discovery_cursor, before)  # moves on instead of repeating the same search
        result = loop._step("Finding businesses", lambda: discovery.discover_next(config))
        self.assertEqual(result, 0)
        self.assertTrue(Activity.objects.filter(level="warning", text__contains="busy").exists())


class WhatsAppTests(TestCase):
    def setUp(self):
        from .models import Market
        self.uk = Market.objects.get(name="United Kingdom")
        self.lead = Lead.objects.create(
            name="Leeds Law", phone="+44 7700 900123", market=self.uk, category="lawyer",
            city="Leeds, United Kingdom", status=Lead.Status.NO_EMAIL, fit_score=85, hook="Site isn't mobile friendly.",
        )

    @mock.patch("agent.services.writer.llm.ask_json", return_value={"message": "Hi Leeds Law, quick idea & question?"})
    def test_draft_and_link(self, ask):
        writer.draft_whatsapp(self.lead)
        prompt = ask.call_args[0][0]
        self.assertIn("United Kingdom", prompt)
        self.assertIn("Never mention prices", prompt)
        self.assertIn("sent on WhatsApp", prompt)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.whatsapp_number, "447700900123")
        self.assertEqual(
            self.lead.whatsapp_send_link,
            "https://wa.me/447700900123?text=Hi%20Leeds%20Law%2C%20quick%20idea%20%26%20question%3F",
        )

    @mock.patch("agent.services.writer.llm.ask_json", return_value={"message": "Hello"})
    def test_loop_writes_once_and_skips_sent(self, _):
        config = AgentConfig.get()
        config.research_dm_leads = True
        sent = Lead.objects.create(name="Already messaged", phone="+441130000001", market=self.uk,
                                   status=Lead.Status.NO_EMAIL, whatsapp_sent_at=timezone.now())
        no_phone = Lead.objects.create(name="No phone", market=self.uk, status=Lead.Status.NO_EMAIL)
        self.assertEqual(loop.whatsapp_batch(config), 1)
        self.assertEqual(loop.whatsapp_batch(config), 0)  # not rewritten every check-in
        sent.refresh_from_db(); no_phone.refresh_from_db()
        self.assertEqual((sent.whatsapp_message, no_phone.whatsapp_message), ("", ""))

    def test_views(self):
        user = User.objects.create_superuser("t", "t@t.com", "pw")
        self.client.force_login(user)
        url = reverse("lead_action", args=[self.lead.pk])
        r = self.client.post(url, {"action": "whatsapp_save", "whatsapp_message": "Edited text"}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(r.json(), {"ok": True})
        r = self.client.post(url, {"action": "whatsapp_sent", "next": "call_list"})
        self.assertTrue(r.url.endswith(f"#lead-{self.lead.pk}"))
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.whatsapp_message, "Edited text")
        self.assertIsNotNone(self.lead.whatsapp_sent_at)
        page = self.client.get(reverse("call_list"))
        self.assertContains(page, "Edited text")
        self.assertContains(page, 'data-number="447700900123"')
        self.assertContains(page, "Messaged")
        self.assertContains(self.client.get(reverse("lead_detail", args=[self.lead.pk])), "Direct message")

    def test_remove_leads_command(self):
        from django.core.management import call_command
        Lead.objects.create(name="Laos shop", phone="+856 20 5533 9992", source="osm")
        call_command("remove_leads", "--phone-prefix", "+856", "--yes", stdout=mock.Mock())
        self.assertFalse(Lead.objects.filter(name="Laos shop").exists())
        self.assertTrue(Lead.objects.filter(name="Leeds Law").exists())



class DirectMessageTests(TestCase):
    def setUp(self):
        from .models import Market
        self.uk = Market.objects.get(name="United Kingdom")
        self.us = Market.objects.get(name="United States")

    def test_find_socials(self):
        from .contacts import find_socials
        html = """
        <a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>
        <a href="https://www.instagram.com/p/AbC123/">a post</a>
        <a href="https://instagram.com/smile.dental/">IG</a>
        <a href="https://www.facebook.com/SmileDentalLeeds">FB</a>
        <a href="https://uk.linkedin.com/company/smile-dental-ltd">in</a>
        <a href="https://twitter.com/intent/tweet?text=hi">tweet this</a>
        <a href="https://x.com/smiledental">X</a>
        <a href="https://www.tiktok.com/@smile.dental">tt</a>"""
        self.assertEqual(find_socials(html), {
            "instagram": "https://www.instagram.com/smile.dental/",
            "facebook": "https://www.facebook.com/SmileDentalLeeds",
            "linkedin": "https://www.linkedin.com/company/smile-dental-ltd",
            "x": "https://x.com/smiledental",
            "tiktok": "https://www.tiktok.com/@smile.dental",
        })

    def test_dm_links_and_handles(self):
        from .contacts import dm_link, social_url
        self.assertEqual(dm_link("instagram", "https://www.instagram.com/smile.dental/"), "https://ig.me/m/smile.dental")
        self.assertEqual(dm_link("facebook", "https://www.facebook.com/SmileDentalLeeds"), "https://m.me/SmileDentalLeeds")
        self.assertEqual(dm_link("facebook", "https://www.facebook.com/profile.php?id=123"), "https://www.facebook.com/profile.php?id=123")
        self.assertEqual(social_url("instagram", "@smile.dental"), "https://www.instagram.com/smile.dental/")
        self.assertEqual(social_url("linkedin", "smile-dental"), "https://www.linkedin.com/company/smile-dental")
        self.assertEqual(social_url("instagram", "not a link at all ///"), "")

    def test_phone_kinds(self):
        from .contacts import phone_kind
        self.assertEqual(phone_kind("+44 7700 900123", "44"), "mobile")
        self.assertEqual(phone_kind("020 7946 0123", "44"), "landline")
        self.assertEqual(phone_kind("0803 123 4567", "234"), "mobile")
        self.assertEqual(phone_kind("(713) 555-0142", "1"), "unknown")
        self.assertEqual(phone_kind("020 7946 0123", "44", whatsapp_confirmed=True), "whatsapp")
        landline = Lead.objects.create(name="Office", phone="020 7946 0123", market=self.uk)
        self.assertEqual((landline.whatsapp_number, landline.whatsapp_link), ("", ""))

    def test_osm_tags(self):
        el = {"type": "node", "id": 7, "tags": {"name": "Smile", "contact:whatsapp": "+447700900123",
                                                "phone": "02079460123", "contact:instagram": "smile.dental",
                                                "contact:facebook": "https://facebook.com/smile"}}
        data = discovery.parse_element(el, "London, United Kingdom", "amenity=dentist")
        self.assertEqual(data["phone"], "+447700900123")
        self.assertTrue(data["whatsapp_confirmed"])
        self.assertEqual(data["socials"]["instagram"], "https://www.instagram.com/smile.dental/")
        self.assertEqual(data["socials"]["facebook"], "https://www.facebook.com/smile")

    @mock.patch("agent.services.research.llm.ask_json", return_value={"fit_score": 80})
    @mock.patch("agent.services.research.fetch")
    def test_research_prefers_whatsapp_number_and_collects_socials(self, fetch, _):
        html = """<p>Office: 020 7946 0123</p><a href="https://wa.me/447700900123">Chat on WhatsApp</a>
                  <a href="https://instagram.com/leedsfirm">IG</a>"""
        fetch.return_value = (FakeResp(html), 1.0)
        lead = Lead.objects.create(name="Firm", website="https://firm.co.uk", market=self.uk, phone="020 7946 0999")
        research.research_lead(lead)
        lead.refresh_from_db()
        self.assertEqual((lead.phone, lead.whatsapp_confirmed, lead.phone_kind), ("+447700900123", True, "whatsapp"))
        self.assertEqual(lead.socials, {"instagram": "https://www.instagram.com/leedsfirm/"})
        self.assertEqual(lead.status, Lead.Status.NO_EMAIL)

    @mock.patch("agent.services.writer.llm.ask_json", return_value={"message": "Hi"})
    def test_social_only_leads_get_messages(self, ask):
        lead = Lead.objects.create(name="IG only", market=self.us, status=Lead.Status.NO_EMAIL,
                                   socials={"instagram": "https://www.instagram.com/igonly/"})
        Lead.objects.create(name="Nothing", market=self.us, status=Lead.Status.NO_EMAIL)
        config = AgentConfig.get()
        self.assertEqual(loop.whatsapp_batch(config), 0)  # emails first: no Claude spent on DMs by default
        config.research_dm_leads = True
        self.assertEqual(loop.whatsapp_batch(config), 1)
        self.assertIn("sent on Instagram", ask.call_args[0][0])
        lead.refresh_from_db()
        self.assertEqual(lead.whatsapp_message, "Hi")

    def test_page_order_buttons_and_saving(self):
        user = User.objects.create_superuser("t", "t@t.com", "pw")
        self.client.force_login(user)
        base = dict(status=Lead.Status.NO_EMAIL, market=self.uk, whatsapp_message="Hello there")
        Lead.objects.create(name="Landline Ltd", phone="020 7946 0123", fit_score=95, **base)
        Lead.objects.create(name="Insta Co", socials={"instagram": "https://www.instagram.com/instaco/"}, fit_score=70, **base)
        Lead.objects.create(name="WA Co", phone="+447700900123", whatsapp_confirmed=True, fit_score=60, **base)
        Lead.objects.create(name="Done Co", phone="+447700900124", whatsapp_confirmed=True, fit_score=99,
                            whatsapp_sent_at=timezone.now(), dm_channel="instagram", **base)
        page = self.client.get(reverse("call_list")).content.decode()
        order = [page.find(n) for n in ["WA Co", "Insta Co", "Landline Ltd", "Done Co"]]
        self.assertEqual(order, sorted(order))  # best channel first, already-messaged last
        self.assertIn('data-url="https://ig.me/m/instaco"', page)
        self.assertIn("Landline — call", page)
        self.assertIn("Messaged on Instagram", page)
        lead = Lead.objects.get(name="Landline Ltd")
        self.client.post(reverse("lead_action", args=[lead.pk]), {
            "action": "socials_save", "social_instagram": "@landline.ltd", "social_facebook": "",
        })
        lead.refresh_from_db()
        self.assertEqual(lead.socials, {"instagram": "https://www.instagram.com/landline.ltd/"})
        self.client.post(reverse("lead_action", args=[lead.pk]), {"action": "whatsapp_sent", "channel": "instagram"})
        lead.refresh_from_db()
        self.assertEqual(lead.dm_channel, "instagram")
        self.assertContains(self.client.get(reverse("lead_detail", args=[lead.pk])), 'id="socials"')

    @mock.patch("agent.services.research.fetch")
    def test_refresh_contacts_finds_socials(self, fetch):
        from django.core.management import call_command
        fetch.return_value = (FakeResp('<a href="https://www.facebook.com/oldfirm">fb</a>'), 1.0)
        lead = Lead.objects.create(name="Old", website="https://old.com", phone="+17135550142", market=self.us,
                                   status=Lead.Status.NO_EMAIL)
        call_command("refresh_contacts", stdout=mock.Mock())
        lead.refresh_from_db()
        self.assertEqual(lead.socials, {"facebook": "https://www.facebook.com/oldfirm"})


class FoursquareTests(TestCase):
    PLACE = {"fsq_place_id": "abc123", "name": "Smile Dental", "location": {"formatted_address": "1 Main St, Houston"},
             "tel": "(713) 555-0142", "website": "smiledental.com", "email": "hi@smiledental.com",
             "social_media": {"instagram": "smile.dental", "facebook_id": "123456789"}}

    def setUp(self):
        from .models import Market
        self.us = Market.objects.get(name="United States")

    def test_to_lead(self):
        from .services import foursquare
        data = foursquare.to_lead(self.PLACE, "Houston, United States", "amenity=dentist")
        self.assertEqual(data["website"], "http://smiledental.com")
        self.assertEqual(data["email"], "hi@smiledental.com")
        self.assertEqual(data["socials"]["instagram"], "https://www.instagram.com/smile.dental/")
        self.assertEqual(data["socials"]["facebook"], "https://www.facebook.com/123456789")
        self.assertEqual(data["category"], "dentist")

    @override_settings(FOURSQUARE_API_KEY="k")
    @mock.patch("agent.services.foursquare.requests.get")
    def test_discover_counts_usage_dedupes_and_respects_limit(self, get):
        from .models import ApiUsage
        from .services import foursquare
        get.return_value = mock.Mock(status_code=200, **{"json.return_value": {"results": [self.PLACE]}})
        Lead.objects.create(name="SMILE DENTAL", city="Dallas, United States")  # other city: not a duplicate
        self.assertEqual(foursquare.discover("Houston, United States", "amenity=dentist", self.us), 1)
        self.assertEqual(foursquare.discover("Houston, United States", "amenity=dentist", self.us), 0)
        self.assertEqual(ApiUsage.used("foursquare"), 2)
        headers = get.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer k")
        self.assertIn("X-Places-Api-Version", headers)
        config = AgentConfig.get()
        config.foursquare_monthly_limit = 2
        self.assertFalse(foursquare.available(config))

    @override_settings(FOURSQUARE_API_KEY="k")
    @mock.patch("agent.services.discovery.discover", return_value=3)
    @mock.patch("agent.services.foursquare.discover", side_effect=__import__("agent.services.foursquare", fromlist=["x"]).FoursquareError("bad key"))
    def test_rotation_uses_both_and_survives_foursquare_errors(self, fsq, osm):
        config = AgentConfig.get()
        self.assertEqual(discovery.discover_next(config), 3)
        self.assertTrue(fsq.called and osm.called)
        self.assertTrue(Activity.objects.filter(text__contains="bad key").exists())

    @mock.patch("agent.services.discovery.discover", return_value=1)
    @mock.patch("agent.services.foursquare.discover")
    def test_no_key_skips_foursquare(self, fsq, osm):
        discovery.discover_next(AgentConfig.get())
        fsq.assert_not_called()


REDDIT_JSON = {"data": {"children": [
    {"data": {"id": "a1", "title": "[Hiring] Need a Django developer to build a booking site", "selftext": "Budget $1500. Email me at owner@shop.com",
              "author": "shopowner", "created_utc": 0, "permalink": "/r/forhire/comments/a1/x/", "over_18": False}},
    {"data": {"id": "a2", "title": "[For Hire] I build websites", "selftext": "", "author": "dev", "created_utc": 0,
              "permalink": "/r/forhire/comments/a2/x/", "over_18": False}},
    {"data": {"id": "a3", "title": "[Hiring] Logo designer", "selftext": "Need a logo", "author": "x", "created_utc": 0,
              "permalink": "/r/forhire/comments/a3/x/", "over_18": False}},
]}}


class GigTests(TestCase):
    def setUp(self):
        import time
        for child in REDDIT_JSON["data"]["children"]:
            child["data"]["created_utc"] = time.time() - 3600

    @mock.patch("agent.services.gigs.requests.get")
    def test_reddit_filters_hiring_and_keywords(self, get):
        from .services import gigs
        config = AgentConfig.get()
        config.remotive_enabled = config.mastodon_enabled = False
        config.hn_gigs_enabled = False
        config.gig_subreddits = "forhire"
        get.return_value = mock.Mock(status_code=200, **{"json.return_value": REDDIT_JSON})
        self.assertEqual(gigs.find_gigs(config, force=True), 1)
        gig = Lead.objects.get(kind="gig")
        self.assertEqual((gig.source_id, gig.post_author, gig.email), ("reddit:a1", "shopowner", "owner@shop.com"))
        self.assertEqual(gig.post_url, "https://www.reddit.com/r/forhire/comments/a1/x/")
        self.assertEqual(gigs.find_gigs(config), 0)  # not due again yet
        self.assertEqual(gigs.find_gigs(config, force=True), 0)  # no duplicates

    @mock.patch("agent.services.gigs.requests.get")
    def test_hn_seeking_freelancer(self, get):
        from .services import gigs
        now = timezone.now().isoformat()
        responses = {
            gigs.HN_SEARCH: {"hits": [{"objectID": "999", "title": "Ask HN: Freelancer? Seeking freelancer? (November 2026)"}]},
            gigs.HN_ITEM.format(id="999"): {"children": [
                {"id": 1, "author": "acme", "created_at": now, "text": "SEEKING FREELANCER | Remote | <p>Need a React dashboard. contact@acme.io"},
                {"id": 2, "author": "me", "created_at": now, "text": "SEEKING WORK | Remote | Python developer"},
            ]},
        }
        get.side_effect = lambda url, **kw: mock.Mock(status_code=200, raise_for_status=lambda: None,
                                                    **{"json.return_value": responses[url]})
        config = AgentConfig.get()
        config.remotive_enabled = config.mastodon_enabled = False
        config.gig_subreddits = ""
        self.assertEqual(gigs.find_gigs(config, force=True), 1)
        gig = Lead.objects.get(kind="gig")
        self.assertEqual((gig.source, gig.email), ("hn", "contact@acme.io"))

    @mock.patch("agent.services.gigs.requests.get", return_value=mock.Mock(status_code=403))
    def test_reddit_blocked_is_logged_not_fatal(self, _):
        from .services import gigs
        config = AgentConfig.get()
        config.remotive_enabled = config.mastodon_enabled = False
        config.hn_gigs_enabled = False
        self.assertEqual(gigs.find_gigs(config, force=True), 0)
        self.assertTrue(Activity.objects.filter(text__contains="HTTP 403").exists())

    @fake_claude()
    def test_gig_research_and_isolation_from_cold_email(self, _):
        with mock.patch("agent.services.llm.ask_json", return_value={
            "fit_score": 88, "fit_reason": "Web build", "summary": "A booking site", "budget": "$1500", "reply": "Hi! Happy to help."
        }):
            gig = Lead.objects.create(kind="gig", source="reddit", name="g", post_title="[Hiring] booking site",
                                      post_text="Budget $1500", email="owner@shop.com", post_author="shopowner")
            loop.research_batch(AgentConfig.get())
        gig.refresh_from_db()
        self.assertEqual((gig.status, gig.budget, gig.whatsapp_message), (Lead.Status.QUALIFIED, "$1500", "Hi! Happy to help."))
        self.assertEqual(loop.draft_batch(AgentConfig.get()), 0)  # no cold email for gigs
        self.assertEqual(loop.whatsapp_batch(AgentConfig.get()), 0)

    def test_gigs_page_and_email_action(self):
        user = User.objects.create_superuser("t", "t@t.com", "pw")
        self.client.force_login(user)
        config = AgentConfig.get()
        config.postal_address = "12 Example Street, Akure"
        config.save()
        gig = Lead.objects.create(kind="gig", source="reddit", name="g", post_title="[Hiring] booking site", status="qualified",
                                  fit_score=88, post_author="shopowner", email="owner@shop.com", whatsapp_message="Hi there",
                                  post_url="https://www.reddit.com/r/forhire/comments/a1/x/", budget="$1500")
        Lead.objects.create(kind="gig", source="hn", name="poor", post_title="Logo", status="disqualified", fit_score=10)
        page = self.client.get(reverse("gigs"))
        self.assertContains(page, "Reddit message")
        self.assertContains(page, 'data-author="shopowner"')
        self.assertContains(page, "budget: $1500")
        self.assertNotContains(page, ">Logo<")
        self.assertContains(self.client.get(reverse("gigs") + "?all=1"), "Logo")
        self.assertContains(self.client.get(reverse("dashboard")), 'href="/gigs/"')
        self.client.post(reverse("lead_action", args=[gig.pk]), {"action": "gig_email", "next": "gigs"})
        msg = Message.objects.get(lead=gig)
        self.assertEqual((msg.kind, msg.status, msg.subject), ("reply", "draft", "Re: [Hiring] booking site"))
        self.assertIn("12 Example Street", msg.body)
        self.client.post(reverse("lead_action", args=[gig.pk]), {"action": "whatsapp_sent", "channel": "reddit", "next": "gigs"})
        gig.refresh_from_db()
        self.assertEqual(gig.dm_channel, "reddit")
        self.assertContains(self.client.get(reverse("settings")), "Gig postings")



class MoreSourceTests(TestCase):
    def setUp(self):
        self.config = AgentConfig.get()
        self.config.gig_subreddits = ""
        self.config.hn_gigs_enabled = False

    @mock.patch("agent.services.gigs.requests.get")
    def test_remotive_contract_only_four_times_a_day(self, get):
        from .services import gigs
        today = timezone.now().strftime("%Y-%m-%dT%H:%M:%S")
        get.return_value = mock.Mock(status_code=200, **{"json.return_value": {"jobs": [
            {"id": 1, "url": "https://remotive.com/remote-jobs/software-dev/django-dev-1", "title": "Django developer",
             "company_name": "Acme", "job_type": "contract", "publication_date": today,
             "candidate_required_location": "Worldwide", "salary": "$60/hr", "description": "<p>Build our Django API</p>"},
            {"id": 2, "url": "https://remotive.com/x/2", "title": "Senior Django engineer", "company_name": "Big",
             "job_type": "full_time", "publication_date": today, "description": "Django"},
        ]}})
        self.config.mastodon_enabled = False
        self.assertEqual(gigs.find_gigs(self.config, force=True), 1)
        gig = Lead.objects.get(kind="gig")
        self.assertEqual((gig.source, gig.post_author), ("remotive", "Acme"))
        self.assertIn("$60/hr", gig.post_text)
        self.assertEqual(gigs.remotive_gigs(self.config), 0)  # not again within 6 hours
        self.assertEqual(get.call_count, 1)

    @mock.patch("agent.services.gigs.requests.get")
    def test_mastodon_needs_clear_request(self, get):
        from .services import gigs
        now = timezone.now().isoformat()
        posts = [
            {"id": "1", "uri": "u1", "url": "https://mastodon.social/@bakery/1", "created_at": now,
             "account": {"acct": "bakery@example.org"}, "content": "<p>We're looking for a web developer to rebuild our bakery website #hiring</p>"},
            {"id": "2", "uri": "u2", "url": "https://mastodon.social/@dev/2", "created_at": now,
             "account": {"acct": "dev"}, "content": "<p>I'm a freelance developer, available for projects! Need a website? #hiring</p>"},
            {"id": "3", "uri": "u3", "url": "https://mastodon.social/@co/3", "created_at": now,
             "account": {"acct": "co"}, "content": "<p>We're hiring a barista #hiring</p>"},
        ]
        get.return_value = mock.Mock(status_code=200, **{"json.return_value": posts})
        self.config.remotive_enabled = False
        self.config.mastodon_tags = "hiring"
        self.assertEqual(gigs.find_gigs(self.config, force=True), 1)
        gig = Lead.objects.get(kind="gig")
        self.assertEqual((gig.source, gig.post_author), ("mastodon", "@bakery@example.org"))

    @mock.patch("agent.services.gigs.requests.get", return_value=mock.Mock(status_code=503))
    def test_sources_down_are_logged(self, _):
        from .services import gigs
        self.config.mastodon_tags = "hiring"
        self.assertEqual(gigs.find_gigs(self.config, force=True), 0)
        self.assertTrue(Activity.objects.filter(text__contains="Remotive unavailable").exists())
        self.assertTrue(Activity.objects.filter(text__contains="Mastodon #hiring unavailable").exists())

    @mock.patch("agent.services.writer.llm.ask_json", return_value={"subject": "s", "body": "b"})
    def test_writes_in_country_language(self, ask):
        from .models import Market
        de = Market.objects.get(name="Germany")
        lead = Lead.objects.create(name="Kanzlei", email="a@k.de", market=de)
        writer.draft_initial(lead)
        self.assertIn("Write in German.", ask.call_args[0][0])
        lead2 = Lead.objects.create(name="Firm", email="a@f.com", market=Market.objects.get(name="United States"))
        writer.draft_initial(lead2)
        self.assertIn("Write in English.", ask.call_args[0][0])

    def test_enable_all_and_sectors(self):
        from .models import Market
        user = User.objects.create_superuser("t", "t@t.com", "pw")
        self.client.force_login(user)
        self.client.post(reverse("markets"), {"all": "on"})
        self.assertFalse(Market.objects.filter(enabled=False).exists())
        self.assertContains(self.client.get(reverse("markets")), "29 of 29 countries on")
        self.client.post(reverse("markets"), {"all": "off"})
        self.assertFalse(Market.objects.filter(enabled=True).exists())
        self.assertIn("healthcare=physiotherapist", AgentConfig.get().categories)
        self.assertIn("jobbit", AgentConfig.get().subreddits)
        self.assertContains(self.client.get(reverse("settings")), "Mastodon")


class EmailFinderTests(TestCase):
    def setUp(self):
        from .models import Market
        self.us = Market.objects.get(name="United States")

    def test_extraction_tricks(self):
        key = 0x42
        cf = format(key, "02x") + "".join(format(ord(c) ^ key, "02x") for c in "hello@acmelaw.com")
        html = f"""<html><body>
          <span class="__cf_email__" data-cfemail="{cf}">[email&#160;protected]</span>
          <p>Write to office [at] acmelaw [dot] com</p>
          <p>noreply@acmelaw.com</p><p>privacy@acmelaw.com</p>
          <script type="application/ld+json">{{"@type":"LegalService","email":"intake@acmelaw.com"}}</script>
          <a href="mailto:partner@gmail.com">x</a>
        </body></html>"""
        emails = research.find_emails(html, "acmelaw.com")
        self.assertIn("hello@acmelaw.com", emails)
        self.assertIn("office@acmelaw.com", emails)
        self.assertIn("intake@acmelaw.com", emails)
        self.assertNotIn("noreply@acmelaw.com", emails)
        self.assertEqual(emails[0], "hello@acmelaw.com")          # own domain + role address first
        self.assertEqual(emails[-1], "partner@gmail.com")         # other domains last
        self.assertLess(emails.index("intake@acmelaw.com"), emails.index("privacy@acmelaw.com"))

    def test_contact_links(self):
        html = '<a href="/about-us">About</a><a href="https://other.com/contact">x</a><a href="/services">Get in touch</a><a href="mailto:a@b.com">contact</a>'
        self.assertEqual(research.contact_links(html, "https://firm.com/"),
                         ["https://firm.com/about-us", "https://firm.com/services"])

    @override_settings(EMAIL_MX_CHECK=True)
    def test_mx_check(self):
        research.has_mx.cache_clear()
        with mock.patch("agent.services.research.has_mx", side_effect=lambda d: d != "dead-domain.com"):
            self.assertTrue(research.usable_email("info@firm.com"))
            self.assertFalse(research.usable_email("info@dead-domain.com"))
            self.assertFalse(research.usable_email("noreply@firm.com"))
            data = {"name": "Dead Co", "email": "info@dead-domain.com", "source": "osm", "source_id": "osm:node/1"}
            lead = discovery._create_lead(data, self.us)
            self.assertEqual((lead.email, lead.email_found_at), ("", None))

    def test_contactable_query(self):
        q = discovery.overpass_query(1, "office=lawyer", 60, contactable_only=True)  # default: proven union format
        for key in ("website", "contact:website", "url", "email", "contact:email"):
            self.assertIn(f'nwr["office"="lawyer"]["name"]["{key}"](area.a);', q)
        self.assertIn("[timeout:90]", q)
        self.assertIn("out center tags 60", q)
        p = discovery.overpass_query(1, "office=lawyer", 60, contactable_only=True, style="pattern")
        self.assertIn('[~"^(website|contact:website|url|email|contact:email)$"~"."]', p)
        self.assertNotIn("website", discovery.overpass_query(1, "office=lawyer", 60))
        self.assertEqual(discovery.QUERY_STYLES[0], "union")

    @mock.patch("agent.services.discovery.run_overpass")
    def test_rejected_format_falls_back(self, run):
        discovery._working_style["style"] = None
        run.side_effect = [discovery.OverpassBadQuery("syntax"), {"elements": []}]
        self.assertEqual(discovery.run_search(1, "office=lawyer", 60, True), {"elements": []})
        self.assertIn('[~"^(', run.call_args_list[1][0][0])  # second try used the pattern format
        discovery._working_style["style"] = None

    def test_scan_finds_emails_sets_aside_dead_ends_and_saves_claude(self):
        a = Lead.objects.create(name="A", website="https://a.com", market=self.us)
        b = Lead.objects.create(name="B", website="https://b.com", market=self.us)
        c = Lead.objects.create(name="C", market=self.us)  # no website, nothing to contact
        facts = {
            "A": {"emails": ["info@a.com"], "phones": [], "whatsapp": [], "socials": {}, "signals": ["No HTTPS"], "text": "law", "title": "A"},
            "B": {"emails": [], "phones": ["+17135550142"], "whatsapp": [], "socials": {}, "signals": [], "text": "", "title": "B"},
            "C": {"emails": [], "phones": [], "whatsapp": [], "socials": {}, "signals": ["No website listed"], "text": "", "title": ""},
        }
        with mock.patch("agent.services.research.gather", side_effect=lambda l, *a, **k: facts[l.name]):
            self.assertEqual(research.scan_contacts(AgentConfig.get()), 1)
        a.refresh_from_db(); b.refresh_from_db(); c.refresh_from_db()
        self.assertEqual((a.email, a.status), ("info@a.com", "new"))
        self.assertIsNotNone(a.email_found_at)
        self.assertEqual(b.phone, "+17135550142")
        self.assertEqual(c.status, Lead.Status.DISQUALIFIED)
        self.assertEqual(research.emails_found_today(), 1)
        # Emailable leads are researched first, and research reuses the scan (no second website visit).
        config = AgentConfig.get()
        self.assertEqual([l.name for l in research.research_queue(config)], ["A"])  # emails only by default
        config.research_dm_leads = True
        self.assertEqual([l.name for l in research.research_queue(config)], ["A", "B"])
        with mock.patch("agent.services.research.gather") as gather, \
             mock.patch("agent.services.research.llm.ask_json", return_value={"fit_score": 80, "summary": "s", "hook": "h", "fit_reason": "r"}):
            research.research_lead(a)
            gather.assert_not_called()
        a.refresh_from_db()
        self.assertEqual((a.status, a.site_signals, a.scan_cache), (Lead.Status.QUALIFIED, ["No HTTPS"], {}))

    @mock.patch("agent.services.loop.discovery.discover_next", return_value=50)
    def test_discovery_keeps_going_until_daily_target(self, discover_next):
        config = AgentConfig.get()
        config.daily_email_target, config.scan_per_tick, config.min_new_leads, config.searches_when_behind = 2, 3, 1, 3
        Lead.objects.create(name="waiting", market=self.us, email="w@w.com", contacts_checked_at=timezone.now())
        self.assertEqual(loop.maybe_discover(config), 150)                      # behind target: 3 searches
        for i in range(2):
            Lead.objects.create(name=f"f{i}", market=self.us, email=f"f{i}@f.com", email_found_at=timezone.now(),
                                status="qualified")
        self.assertEqual(loop.maybe_discover(config), 0)                        # target met, queue not empty
        for i in range(6):
            Lead.objects.create(name=f"u{i}", market=self.us)                   # unscanned queue full (2 x scan size)
        config.daily_email_target = 99
        self.assertEqual(loop.maybe_discover(config), 0)

    def test_dashboard_counter(self):
        user = User.objects.create_superuser("t", "t@t.com", "pw")
        self.client.force_login(user)
        Lead.objects.create(name="x", market=self.us, email="x@x.com", email_found_at=timezone.now())
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, "New emails found today")
        self.assertContains(page, "1<span")
        self.assertContains(self.client.get(reverse("settings")), "Daily email target")



class EmailsFirstTests(TestCase):
    def setUp(self):
        from .models import Market
        self.us = Market.objects.get(name="United States")

    def _scanned(self, category, n, with_email):
        for i in range(n):
            Lead.objects.create(name=f"{category}{i}", category=category, market=self.us,
                                contacts_checked_at=timezone.now(), email=f"a{i}@{category}.com" if i < with_email else "")

    def test_poor_sectors_are_skipped(self):
        config = AgentConfig.get()
        config.target_categories = "craft=plumber\noffice=lawyer"
        from .models import Market
        Market.objects.exclude(pk=self.us.pk).update(enabled=False)
        self.us.cities = "Houston, United States"
        self.us.save()
        self._scanned("plumber", 40, 1)    # 2.5% -> poor
        self._scanned("lawyer", 40, 20)    # 50%
        with mock.patch.object(discovery, "discover", return_value=0) as d:
            discovery.discover_next(config)
            discovery.discover_next(config)
        self.assertEqual([c.args[1] for c in d.call_args_list], ["office=lawyer", "office=lawyer"])
        config.skip_poor_sectors = False
        with mock.patch.object(discovery, "discover", return_value=0) as d:
            discovery.discover_next(config)
            discovery.discover_next(config)
        self.assertEqual(sorted(c.args[1] for c in d.call_args_list), ["craft=plumber", "office=lawyer"])

    @mock.patch("agent.services.gigs.requests.get", return_value=mock.Mock(status_code=403))
    def test_blocked_subreddit_paused_for_a_day(self, get):
        from .services import gigs
        config = AgentConfig.get()
        config.gig_subreddits = "jobbit"
        config.hn_gigs_enabled = config.remotive_enabled = config.mastodon_enabled = False
        gigs.find_gigs(config, force=True)
        calls = get.call_count
        gigs.find_gigs(config, force=True)
        self.assertEqual(get.call_count, calls)  # not retried for 24 hours
        self.assertIn("reddit:jobbit", AgentConfig.get().gig_source_pauses)
        self.assertEqual(Activity.objects.filter(text__contains="r/jobbit").count(), 1)

    @mock.patch("agent.services.research.scan_many")
    def test_refresh_contacts_revives_leads(self, scan_many):
        from django.core.management import call_command
        dm = Lead.objects.create(name="DM", website="https://dm.com", market=self.us, status="no_email", fit_score=80)
        dead = Lead.objects.create(name="Dead", website="https://dead.com", market=self.us, status="disqualified",
                                   fit_reason="No email, phone or social account found.")
        poor = Lead.objects.create(name="Poor", website="https://poor.com", market=self.us, status="disqualified",
                                   fit_score=20, fit_reason="Too big")
        facts = {"emails": ["x@y.com"], "phones": [], "whatsapp": [], "socials": {}}
        scan_many.side_effect = lambda leads, **k: [dict(facts) for _ in leads]
        with mock.patch("agent.management.commands.refresh_contacts.scan_many", scan_many):
            call_command("refresh_contacts", stdout=mock.Mock())
        for lead in (dm, dead, poor):
            lead.refresh_from_db()
        self.assertEqual((dm.status, dead.status, poor.status), ("qualified", "new", "disqualified"))
        self.assertTrue(all(l.email == "x@y.com" for l in (dm, dead, poor)))



class SectorBalanceTests(TestCase):
    def setUp(self):
        from .models import Market
        Market.objects.exclude(name="United States").update(enabled=False)
        us = Market.objects.get(name="United States")
        us.cities = "Houston, United States"
        us.save()
        self.us = us

    def test_crowded_sector_waits_its_turn(self):
        config = AgentConfig.get()
        config.target_categories = "amenity=dentist\noffice=lawyer\noffice=accountant"
        for i in range(60):
            Lead.objects.create(name=f"d{i}", category="dentist", market=self.us)
        for i in range(5):
            Lead.objects.create(name=f"l{i}", category="lawyer", market=self.us)
        with mock.patch.object(discovery, "discover", return_value=0) as d:
            for _ in range(4):
                discovery.discover_next(config)
        tags = [c.args[1] for c in d.call_args_list]
        self.assertNotIn("amenity=dentist", tags)
        self.assertEqual(set(tags), {"office=lawyer", "office=accountant"})

    def test_everything_crowded_still_searches(self):
        config = AgentConfig.get()
        config.target_categories = "amenity=dentist"
        for i in range(60):
            Lead.objects.create(name=f"d{i}", category="dentist", market=self.us)
        with mock.patch.object(discovery, "discover", return_value=0) as d:
            discovery.discover_next(config)
        self.assertEqual(d.call_args.args[1], "amenity=dentist")



class SpreadTests(TestCase):
    def test_burst_covers_many_countries_and_sectors(self):
        from .models import Market
        config = AgentConfig.get()
        plan = discovery.search_plan(config)
        first = plan[:21]
        self.assertEqual(len({m.name for m, _, _ in first}), 21)   # 21 different countries in a row
        self.assertGreater(len({t for _, _, t in first}), 15)      # and many different sectors
        self.assertEqual(config.searches_when_behind, 6)

    @mock.patch("agent.services.discovery.run_overpass", return_value={"elements": []})
    @mock.patch("agent.services.discovery.city_area_id", return_value=3600000123)
    def test_city_lookup_is_cached(self, lookup, _):
        from .models import Market
        us = Market.objects.get(name="United States")
        discovery.discover("Houston, United States", "office=lawyer", market=us, contactable_only=True)
        discovery.discover("Houston, United States", "amenity=dentist", market=us, contactable_only=True)
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(Market.objects.get(pk=us.pk).area_ids["Houston, United States"], 3600000123)


class FinderTests(TestCase):
    def setUp(self):
        from .models import Market
        self.us = Market.objects.get(name="United States")

    @mock.patch("agent.services.finder.research.scan_contacts", return_value=7)
    @mock.patch("agent.services.finder.discovery.discover_next", return_value=40)
    def test_cycle_searches_when_queue_low_then_scans(self, discover, scan):
        from .services import finder
        result = finder.cycle()
        self.assertEqual(result, {"searched": 40, "emails_found": 7, "emails_today": 7})
        scan.assert_called_once_with(mock.ANY, limit=finder.SCAN_BATCH, workers=finder.SCAN_WORKERS)
        for i in range(finder.SCAN_BATCH):
            Lead.objects.create(name=f"q{i}", market=self.us)  # queue full: no map search this round
        discover.reset_mock()
        finder.cycle()
        discover.assert_not_called()

    @mock.patch("agent.services.finder.research.scan_contacts")
    def test_idle_when_target_met_or_paused(self, scan):
        from .services import finder
        config = AgentConfig.get()
        config.daily_email_target = 1
        config.save()
        Lead.objects.create(name="x", market=self.us, email="x@x.com", email_found_at=timezone.now())
        self.assertTrue(finder.cycle()["idle"])
        config.agent_enabled = False
        config.save()
        self.assertEqual(finder.cycle(), {"paused": True})
        scan.assert_not_called()

    @mock.patch("agent.services.finder.research.scan_contacts", return_value=0)
    @mock.patch("agent.services.finder.discovery.discover_next", side_effect=discovery.OverpassBusy("busy"))
    def test_busy_map_servers_dont_stop_scanning(self, _, scan):
        from .services import finder
        self.assertEqual(finder.cycle()["searched"], 0)
        scan.assert_called_once()
        self.assertTrue(Activity.objects.filter(text__contains="Email finder: busy").exists())

    @fake_claude()
    @mock.patch("agent.services.loop.research.scan_contacts")
    @mock.patch("agent.services.loop.maybe_discover")
    def test_claude_tick_leaves_finding_to_the_finder(self, discover, scan, _):
        with mock.patch("agent.services.loop.inbox.check_inbox", return_value=0), \
             mock.patch("agent.services.loop.mailer.send_approved", return_value=0), \
             mock.patch("agent.services.loop.gigs.find_gigs", return_value=0):
            results = loop.tick(finding=False)
        discover.assert_not_called()
        scan.assert_not_called()
        self.assertNotIn("discovered", results)

    @mock.patch("agent.services.finder.cycle", return_value={"emails_found": 3, "emails_today": 3})
    def test_background_loop_stops_cleanly(self, cycle):
        import threading
        from .services import finder
        stop = threading.Event()
        lines = []
        t = threading.Thread(target=finder.run_forever, args=(stop, 0, lines.append))
        t.start()
        time_limit = __import__("time").time() + 2
        while cycle.call_count < 2 and __import__("time").time() < time_limit:
            pass
        stop.set()
        t.join(timeout=2)
        self.assertFalse(t.is_alive())
        self.assertTrue(any("+3 emails" in l for l in lines))


class PerSearchTests(TestCase):
    def setUp(self):
        from .models import Market
        Market.objects.exclude(name="United States").update(enabled=False)
        self.us = Market.objects.get(name="United States")
        self.us.cities = "Chicago, United States\nTinyville, United States"
        self.us.save()

    def _finished_run(self, city, tag, emails, businesses=20):
        from .models import SearchRun
        run = SearchRun.objects.create(market=self.us, city=city, tag=tag, businesses=businesses, reported=True)
        for i in range(businesses):
            Lead.objects.create(name=f"{city}{tag}{run.pk}-{i}", market=self.us, search_run=run, category=tag,
                                contacts_checked_at=timezone.now(), email=f"e{i}@x{run.pk}.com" if i < emails else "")
        return run

    @mock.patch("agent.services.discovery.run_overpass")
    @mock.patch("agent.services.discovery.cached_area_id", return_value=1)
    def test_search_is_recorded_sized_and_reported(self, _, overpass):
        from .models import SearchRun
        overpass.return_value = {"elements": [
            {"type": "node", "id": i, "tags": {"name": f"Firm {i}", "website": f"https://f{i}.com"}} for i in range(12)
        ]}
        config = AgentConfig.get()
        config.target_categories = "office=lawyer"
        self.assertEqual(discovery.discover_next(config), 12)
        self.assertIn("out center tags 60", overpass.call_args[0][0])  # reliable size, no timeouts
        run = SearchRun.objects.get()
        self.assertEqual((run.businesses, run.leads.count()), (12, 12))
        facts = lambda l, *a, **k: {"emails": [f"info@{l.name.split()[1]}.com"] if int(l.name.split()[1]) < 11 else [],
                                     "phones": [], "whatsapp": [], "socials": {}, "signals": [], "text": "", "title": ""}
        with mock.patch("agent.services.research.gather", side_effect=facts):
            research.scan_contacts(config, limit=50)
        run.refresh_from_db()
        self.assertTrue(run.reported)
        self.assertTrue(Activity.objects.filter(text__contains="12 businesses → 11 emails").exists())
        self.assertEqual(research.average_emails_per_search(), 11.0)

    def test_cities_and_sectors_that_fall_short_are_skipped(self):
        for _ in range(3):
            self._finished_run("Tinyville, United States", "office=lawyer", emails=2)
            self._finished_run("Chicago, United States", "office=lawyer", emails=25, businesses=30)  # sector avg 13.5
        config = AgentConfig.get()
        config.target_categories = "office=lawyer"
        with mock.patch.object(discovery, "discover", return_value=0) as d:
            for _ in range(3):
                discovery.discover_next(config)
        self.assertEqual({c.args[0] for c in d.call_args_list}, {"Chicago, United States"})
        config.min_emails_per_search = 0
        with mock.patch.object(discovery, "discover", return_value=0) as d:
            discovery.discover_next(config)
            discovery.discover_next(config)
        self.assertIn("Tinyville, United States", {c.args[0] for c in d.call_args_list})

    def test_dashboard_shows_average(self):
        self._finished_run("Chicago, United States", "office=lawyer", emails=12)
        self._finished_run("Chicago, United States", "amenity=dentist", emails=8)
        user = User.objects.create_superuser("t", "t@t.com", "pw")
        self.client.force_login(user)
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, "Avg emails per search")
        self.assertContains(page, "10.0<span")
        self.assertContains(self.client.get(reverse("settings")), "Min emails per search")



class RestoreTests(TestCase):
    def test_migration_restores_reliable_settings(self):
        import importlib
        from django.apps import apps as global_apps
        mig = importlib.import_module("agent.migrations.0013_back_to_reliable_search")
        config = AgentConfig.get()
        config.search_size = 200
        config.target_categories = "\n".join(f"shop=x{i}" for i in range(200))
        config.save()
        mig.forwards(global_apps, None)
        config.refresh_from_db()
        self.assertEqual(config.search_size, 60)
        self.assertEqual(len(config.categories), 29)
        self.assertEqual(config.categories[0], "office=lawyer")
        config.target_categories = "office=lawyer\namenity=dentist"  # a short custom list is left alone
        config.save()
        mig.forwards(global_apps, None)
        config.refresh_from_db()
        self.assertEqual(config.categories, ["office=lawyer", "amenity=dentist"])


class EmailsFoundPageTests(TestCase):
    def setUp(self):
        from .models import Market
        self.us = Market.objects.get(name="United States")
        user = User.objects.create_superuser("t", "t@t.com", "pw")
        self.client.force_login(user)
        now = timezone.now()
        self.waiting = Lead.objects.create(name="Waiting Law", email="a@w.com", email_found_at=now, market=self.us,
                                           contacts_checked_at=now)
        self.drafted = Lead.objects.create(name="Drafted Dental", email="b@d.com", email_found_at=now, market=self.us,
                                           status="drafted", fit_score=80)
        self.not_fit = Lead.objects.create(name="Big Chain", email="c@c.com", email_found_at=now, market=self.us,
                                           status="disqualified", fit_score=30, fit_reason="Large chain with a modern site")
        Lead.objects.create(name="Old", email="d@o.com", email_found_at=now - timezone.timedelta(days=3), market=self.us)
        Lead.objects.create(name="No email", market=self.us)

    def test_lists_found_emails_with_their_step(self):
        page = self.client.get(reverse("emails_found"))
        for text in ("Waiting Law", "a@w.com", "Waiting for Claude", "Drafted: approve it", "Big Chain",
                     "Large chain with a modern site", "Research &amp; draft now", "All stages · 3"):
            self.assertContains(page, text)
        self.assertNotContains(page, "No email")
        self.assertNotContains(page, ">Old<")
        self.assertContains(self.client.get(reverse("emails_found") + "?day=all"), "All stages · 4")
        only = self.client.get(reverse("emails_found") + "?day=today&stage=not_fit")
        self.assertContains(only, "Big Chain")
        self.assertNotContains(only, "Waiting Law")
        self.assertContains(self.client.get(reverse("dashboard")), "/emails/?day=today")

    def test_research_and_draft_now(self):
        claude = {"fit_score": 82, "fit_reason": "Small firm, old site", "summary": "s", "hook": "h",
                  "subject": "quick idea", "body": "Hi"}
        with mock.patch("agent.services.llm.ask_json", return_value=claude):
            r = self.client.post(reverse("lead_action", args=[self.waiting.pk]),
                                 {"action": "research_and_draft", "next": "/emails/?day=today"})
        self.assertRedirects(r, "/emails/?day=today", fetch_redirect_response=False)
        self.waiting.refresh_from_db()
        self.assertEqual(self.waiting.status, Lead.Status.DRAFTED)
        self.assertTrue(Message.objects.filter(lead=self.waiting, status="draft").exists())

    def test_process_next_ten(self):
        claude = {"fit_score": 75, "fit_reason": "r", "summary": "s", "hook": "h", "subject": "s", "body": "b"}
        with mock.patch("agent.services.llm.ask_json", return_value=claude):
            self.client.post(reverse("emails_found"), {"action": "process_next"})
        self.waiting.refresh_from_db()
        self.assertEqual(self.waiting.status, Lead.Status.DRAFTED)

    def test_claude_limit_is_shown(self):
        from .services.llm import LLMError
        with mock.patch("agent.services.llm.ask_json", side_effect=LLMError("usage limit reached")):
            r = self.client.post(reverse("emails_found"), {"action": "process_next"}, follow=True)
        self.assertContains(r, "usage limit reached")


class BacklogTests(TestCase):
    def setUp(self):
        from .models import Market
        self.us = Market.objects.get(name="United States")

    def _waiting(self, n, domain="firm{i}.com"):
        now = timezone.now()
        return [Lead.objects.create(name=f"Firm {i}", email=f"info@{domain.format(i=i)}", market=self.us,
                                    contacts_checked_at=now, email_found_at=now,
                                    scan_cache={"signals": ["No HTTPS"], "text": "law", "title": "t"}) for i in range(n)]

    def test_eight_businesses_per_claude_call(self):
        leads = self._waiting(10)
        def answer(prompt, model=""):
            n = prompt.count("--- BUSINESS ")
            return {"results": [{"i": i, "fit_score": 80 if i % 2 else 40, "fit_reason": "r", "summary": "s",
                                 "hook": "h"} for i in range(1, n + 1)]}
        with mock.patch("agent.services.llm.ask_json", side_effect=answer) as ask:
            self.assertEqual(research.research_many(leads), 10)
        self.assertEqual(ask.call_count, 2)  # 8 + 2, not 10 calls
        statuses = [Lead.objects.get(pk=l.pk).status for l in leads]
        self.assertEqual(statuses.count("qualified"), 5)
        self.assertEqual(statuses.count("disqualified"), 5)

    def test_missing_answers_fall_back_to_single_calls(self):
        leads = self._waiting(3)
        replies = [{"results": [{"i": 1, "fit_score": 90, "fit_reason": "r", "summary": "s", "hook": "h"}]},
                   {"fit_score": 70, "fit_reason": "r2", "summary": "s", "hook": "h"},
                   {"fit_score": 30, "fit_reason": "r3", "summary": "s", "hook": "h"}]
        with mock.patch("agent.services.llm.ask_json", side_effect=replies):
            research.research_many(leads)
        self.assertEqual([Lead.objects.get(pk=l.pk).fit_score for l in leads], [90, 70, 30])

    def test_chains_set_aside_without_claude(self):
        for i in range(4):
            Lead.objects.create(name=f"SATS {i}", email=f"branch{i}@sats.no", market=self.us,
                                contacts_checked_at=timezone.now())
        for i in range(4):
            Lead.objects.create(name=f"Solo {i}", email=f"owner{i}@gmail.com", market=self.us,
                                contacts_checked_at=timezone.now())
        indie = Lead.objects.create(name="Indie", email="hi@indie.com", market=self.us, contacts_checked_at=timezone.now())
        self.assertEqual(research.mark_chains(), 4)
        self.assertEqual(Lead.objects.filter(email__endswith="@sats.no", status="disqualified").count(), 4)
        self.assertIn("4 locations share sats.no", Lead.objects.filter(email__endswith="@sats.no").first().fit_reason)
        self.assertFalse(Lead.objects.filter(email__endswith="@gmail.com", status="disqualified").exists())
        indie.refresh_from_db()
        self.assertEqual(indie.status, "new")

    def test_placeholder_emails_ignored(self):
        html = "<p>first.last@metosin.fi</p><p>fornavn.etternavn@firma.no</p><p>hello@metosin.fi</p>"
        self.assertEqual(research.find_emails(html, "metosin.fi"), ["hello@metosin.fi"])

    def test_tick_uses_batches(self):
        self._waiting(5)
        answer = lambda p, model="": {"results": [{"i": i, "fit_score": 75, "fit_reason": "r", "summary": "s", "hook": "h"}
                                        for i in range(1, p.count("--- BUSINESS ") + 1)]} if "--- BUSINESS" in p \
            else {"subject": "s", "body": "b"}
        with mock.patch("agent.services.llm.ask_json", side_effect=answer) as ask:
            self.assertEqual(loop.research_batch(AgentConfig.get()), 5)
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(AgentConfig.get().research_per_tick, 16)


class ClaudeLimitTests(TestCase):
    def test_reset_time_parses_midnight_lagos(self):
        from zoneinfo import ZoneInfo
        from agent.services import llm
        when = llm.reset_time("You've hit your weekly limit · resets 12am (Africa/Lagos)")
        local = when.astimezone(ZoneInfo("Africa/Lagos"))
        self.assertEqual((local.hour, local.minute), (0, 2))
        self.assertGreater(when, timezone.now())

    def test_limit_error_pauses_claude_work_but_not_finding(self):
        from unittest import mock
        from agent.services import llm, loop
        AgentConfig.get()
        err = llm.LLMLimit("limit", timezone.now() + timedelta(hours=3))
        with mock.patch.object(loop, "research_batch", side_effect=err) as rb, \
             mock.patch.object(loop, "draft_batch", return_value=0) as db, \
             mock.patch.object(loop.inbox, "check_inbox", return_value=0), \
             mock.patch.object(loop.gigs, "find_gigs", return_value=0):
            loop.tick(finding=False)
            self.assertTrue(rb.called)
            self.assertFalse(db.called)  # stopped straight after the limit
            self.assertIsNotNone(AgentConfig.get().claude_paused_until)
            rb.reset_mock()
            loop.tick(finding=False)
            self.assertFalse(rb.called)  # next check-in doesn't retry Claude

    def test_cli_limit_message_raises_llmlimit(self):
        from unittest import mock
        from agent.services import llm
        fake = mock.Mock(returncode=1, stdout='{"is_error": true, "result": "You\'ve hit your weekly limit · resets 12am (Africa/Lagos)"}', stderr="")
        with mock.patch.object(llm.subprocess, "run", return_value=fake):
            with self.assertRaises(llm.LLMLimit):
                llm._claude_cli("hi")


class LenientJsonTests(TestCase):
    def test_unescaped_quotes_and_newlines_are_rescued(self):
        from agent.services import llm
        raw = '{"subject": "uw website laadt erg traag", "body": "Goedendag,\n\nDe "afspraken" pagina is traag.\n\nGroet"}'
        data = llm.extract_json(raw)
        self.assertEqual(data["subject"], "uw website laadt erg traag")
        self.assertIn('"afspraken"', data["body"])

    def test_cut_off_reply_still_gives_subject_and_body(self):
        from agent.services import llm
        data = llm.extract_json('{"subject": "Snelle vraag", "body": "Goedendag,\\n\\nBij een controle')
        self.assertEqual(data["subject"], "Snelle vraag")
        self.assertTrue(data["body"].startswith("Goedendag"))

    def test_one_bad_draft_does_not_stop_the_rest(self):
        from unittest import mock
        from agent.services import loop, writer
        Lead.objects.create(name="A", email="a@a.nl", status="qualified", fit_score=80)
        Lead.objects.create(name="B", email="b@b.nl", status="qualified", fit_score=70)
        calls = []
        def fake(lead, config):
            calls.append(lead.name)
            if lead.name == "A":
                raise llm.LLMError("bad json")
        with mock.patch.object(writer, "draft_initial", side_effect=fake):
            self.assertEqual(loop.draft_batch(AgentConfig.get()), 1)
        self.assertEqual(calls, ["A", "B"])


class DuplicateTests(TestCase):
    def test_same_email_gets_one_draft(self):
        a = Lead.objects.create(name="Clinic", email="info@clinic.se", status="drafted")
        Message.objects.create(lead=a, direction="out", kind="initial", status="draft", subject="s", body="b")
        b = Lead.objects.create(name="Clinic", email="INFO@clinic.se", status="drafted")
        Message.objects.create(lead=b, direction="out", kind="initial", status="draft", subject="s", body="b")
        c = Lead.objects.create(name="Clinic", email="info@clinic.se", status="new")
        self.assertEqual(research.mark_duplicates(), 2)
        self.assertEqual(Message.objects.filter(status="draft").count(), 1)
        self.assertEqual(Lead.objects.get(pk=a.pk).status, "drafted")
        for lead in (b, c):
            self.assertTrue(Lead.objects.get(pk=lead.pk).fit_reason.startswith("Duplicate"))

    def test_duplicates_are_not_counted_as_a_chain(self):
        for _ in range(3):
            Lead.objects.create(name="Clinic", email="info@clinic.se")
        research.mark_chains()
        reasons = list(Lead.objects.values_list("fit_reason", flat=True))
        self.assertFalse(any(r.startswith("Chain") for r in reasons))
        self.assertEqual(Lead.objects.filter(status="new").count(), 1)


class AgencyTests(TestCase):
    def test_agency_sectors_added_alongside_others(self):
        tags = AgentConfig.get().categories
        self.assertIn("office=advertising_agency", tags)
        self.assertIn("amenity=dentist", tags)
        self.assertLess(sum(t in ("office=advertising_agency", "office=web_design") for t in tags) / len(tags), 0.1)

    def test_agency_gets_white_label_pitch(self):
        from agent.models import is_agency
        self.assertTrue(is_agency("advertising agency"))
        self.assertTrue(is_agency("web design agency"))
        self.assertFalse(is_agency("real estate agency"))
        self.assertFalse(is_agency("travel agency"))
        lead = Lead.objects.create(name="Pixel Co", email="hi@pixel.co", category="marketing agency", status="qualified")
        seen = {}
        def fake(prompt, model=""):
            seen["p"] = prompt
            return {"subject": "s", "body": "b"}
        with mock.patch.object(writer.llm, "ask_json", side_effect=fake):
            writer.draft_initial(lead)
        self.assertIn("white-label", seen["p"])
        other = Lead.objects.create(name="Dent", email="d@d.se", category="dentist", status="qualified")
        with mock.patch.object(writer.llm, "ask_json", side_effect=fake):
            writer.draft_initial(other)
        self.assertNotIn("white-label", seen["p"])


@override_settings(**GMAIL, EXTRA_GMAIL_ACCOUNTS=[("two@gmail.com", "pw2"), ("three@gmail.com", "pw3")],
                   WARMUP_START=10, WARMUP_STEP=3)
class MultiInboxTests(TestCase):
    def _approved(self, n, **lead_kw):
        msgs = []
        for i in range(n):
            lead = Lead.objects.create(name=f"B{i}{lead_kw.get('tag','')}", email=f"b{i}{lead_kw.get('tag','')}@x.com", status="drafted")
            msgs.append(Message.objects.create(lead=lead, direction="out", kind="initial", status="approved", subject="s", body="b"))
        return msgs

    @mock.patch("agent.models.AgentConfig.within_send_window", return_value=True)
    @mock.patch("agent.services.mailer.smtplib.SMTP_SSL")
    def test_spreads_across_inboxes_with_warmup(self, smtp, _):
        config = AgentConfig.get(); config.daily_send_limit = 25; config.sends_per_tick = 50; config.save()
        self._approved(60)
        self.assertEqual(mailer.capacity(config), 25 + 10 + 10)
        self.assertEqual(mailer.send_approved(config), 45)
        senders = dict(Message.objects.filter(status="sent").values_list("sender").annotate(n=Count("id")))
        self.assertEqual(senders, {"me@gmail.com": 25, "two@gmail.com": 10, "three@gmail.com": 10})

    @mock.patch("agent.models.AgentConfig.within_send_window", return_value=True)
    @mock.patch("agent.services.mailer.smtplib.SMTP_SSL")
    def test_followup_goes_from_same_inbox(self, smtp, _):
        lead = Lead.objects.create(name="T", email="t@x.com", status="contacted")
        Message.objects.create(lead=lead, direction="out", kind="initial", status="sent", sender="three@gmail.com",
                               sent_at=timezone.now() - timedelta(days=5), subject="s", body="b")
        follow = Message.objects.create(lead=lead, direction="out", kind="followup", status="approved", subject="Re: s", body="f")
        mailer.send_approved(AgentConfig.get())
        follow.refresh_from_db()
        self.assertEqual(follow.sender, "three@gmail.com")

    def test_warmup_grows_daily(self):
        lead = Lead.objects.create(name="W", email="w@x.com")
        Message.objects.create(lead=lead, direction="out", kind="initial", status="sent", sender="two@gmail.com",
                               sent_at=timezone.now() - timedelta(days=4), subject="s", body="b")
        config = AgentConfig.get(); config.daily_send_limit = 40
        self.assertEqual(mailer.limit_for("two@gmail.com", config), 22)
        self.assertEqual(mailer.limit_for("me@gmail.com", config), 40)


class LeanClaudeTests(TestCase):
    def _run(self, **kw):
        fake = mock.Mock(returncode=0, stdout='{"result": "{}"}', stderr="")
        with mock.patch.object(llm.subprocess, "run", return_value=fake) as run:
            llm._claude_cli("hi")
        return run.call_args[0][0]

    def test_lean_flags_used_by_default(self):
        llm._LEAN["on"] = True
        cmd = self._run()
        self.assertIn("--system-prompt", cmd)
        self.assertIn("--tools", cmd)

    @override_settings(CLAUDE_LEAN=False)
    def test_can_switch_off(self):
        self.assertNotIn("--system-prompt", self._run())

    def test_falls_back_if_cli_rejects_flags(self):
        llm._LEAN["on"] = True
        bad = mock.Mock(returncode=1, stdout="", stderr="error: unknown option '--tools'")
        good = mock.Mock(returncode=0, stdout='{"result": "ok"}', stderr="")
        with mock.patch.object(llm.subprocess, "run", side_effect=[bad, good]) as run:
            self.assertEqual(llm._claude_cli("hi"), "ok")
        self.assertNotIn("--system-prompt", run.call_args_list[1][0][0])
        llm._LEAN["on"] = True


class NullCharacterTests(TestCase):
    def test_control_characters_are_removed_before_calling_claude(self):
        fake = mock.Mock(returncode=0, stdout='{"result": "ok"}', stderr="")
        with mock.patch.object(llm.subprocess, "run", return_value=fake) as run:
            llm._claude_cli("Website text: hello\x00 world\x07!")
        sent = run.call_args[0][0][2]
        self.assertNotIn("\x00", sent)
        self.assertNotIn("\x07", sent)
        self.assertIn("hello", sent)
        self.assertIn("\n", llm.clean("a\nb"))  # normal line breaks are kept


@override_settings(**GMAIL, EXTRA_GMAIL_ACCOUNTS=[("bad@gmail.com", "wrong"), ("good@gmail.com", "ok")], WARMUP_START=10, WARMUP_STEP=3)
class BrokenInboxTests(TestCase):
    @mock.patch("agent.models.AgentConfig.within_send_window", return_value=True)
    @mock.patch("agent.services.mailer.smtplib.SMTP_SSL")
    def test_broken_inbox_does_not_hold_back_the_others(self, smtp_cls, _):
        import smtplib
        def make(*a, **k):
            conn = mock.MagicMock()
            conn.__enter__.return_value = conn
            def login(user, pw):
                if user == "bad@gmail.com":
                    raise smtplib.SMTPAuthenticationError(535, b"bad")
            conn.login.side_effect = login
            return conn
        smtp_cls.side_effect = make
        config = AgentConfig.get(); config.daily_send_limit = 25; config.sends_per_tick = 50; config.save()
        for i in range(30):
            lead = Lead.objects.create(name=f"B{i}", email=f"b{i}@x.com", status="drafted")
            Message.objects.create(lead=lead, direction="out", kind="initial", status="approved", subject="s", body="b")
        self.assertEqual(mailer.send_approved(config), 30)
        senders = set(Message.objects.filter(status="sent").values_list("sender", flat=True))
        self.assertEqual(senders, {"me@gmail.com", "good@gmail.com"})
