# NexReach — Nexlab's outreach agent

Your own Harvey-style sales agent. It finds businesses, researches their
websites, decides who's worth contacting, drafts personalised cold emails,
watches your inbox for replies and drafts answers, and queues follow-ups.

**Nothing is ever sent without your approval.** Every email waits in the
dashboard until you click *Approve*.

It runs on your Claude Code login (`claude -p`), so there is no separate
API bill, and finds businesses for free on OpenStreetMap.

---

## How it works

Every 15 minutes the agent checks in and does, in order:

1. **Inbox** – reads new Gmail replies (without marking them read), matches
   them to leads, asks Claude whether they're interested / have a question /
   said no / want to stop, and drafts a reply when needed. Bounces and
   "no thanks" replies go on a do-not-contact list automatically.
2. **Send** – sends the emails *you approved*, up to your daily limit.
3. **Follow-ups** – drafts a follow-up for anyone who hasn't replied after
   *N* days (max 2 by default). A reply cancels pending follow-ups.
4. **Research** – opens each new lead's website, spots fixable problems
   (no HTTPS, not mobile friendly, outdated, slow, no website at all), finds
   a contact email, and has Claude score the fit 0–100.
5. **Draft** – writes a first email for each qualified lead, opening with a
   real observation about their business.
6. **Discover** – when the pipeline runs low, searches the next
   city + business type from your settings on OpenStreetMap.

Good-fit businesses that have a phone number but no email land on the
**Call / WhatsApp list** with a one-click WhatsApp link.

---

## Setup on Windows

You need Python, and Claude Code installed and logged in (`claude` should
open in your terminal).

```powershell
cd C:\Users\HPBV\Desktop
# unzip nexreach.zip here, then:
cd nexreach
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

### 1. Make an outreach Gmail + App Password

1. Create a new Gmail just for outreach (don't use your personal one).
2. Turn on **2-Step Verification** for it.
3. Go to **Google Account → Security → App passwords**, create one called
   "NexReach" and copy the 16-letter password.
4. Put the address and password in `.env` (`GMAIL_ADDRESS`,
   `GMAIL_APP_PASSWORD`).

### 2. Create the database and your login

```powershell
python manage.py migrate
python manage.py createsuperuser
```

### 3. Check everything is connected (sends nothing)

```powershell
python manage.py check_setup
```

If Claude fails with "couldn't find the claude command", run
`where claude` and put that full path in `CLAUDE_BIN` in `.env`.

### 4. Run it — two terminals

Terminal 1, the dashboard:

```powershell
venv\Scripts\activate
python manage.py runserver
```

Open http://127.0.0.1:8000, log in, and go to **Settings** to check your
offer, target cities and business types.

Terminal 2, the agent:

```powershell
venv\Scripts\activate
python manage.py run_agent
```

It keeps going every 15 minutes while the window is open and your laptop is
awake. Useful variations:

```powershell
python manage.py run_agent --once          # one check-in, then stop
python manage.py run_agent --minutes 30    # slower pace
python manage.py find_leads "Akure" "shop=clothes"   # search right now
```

---

## Daily routine

1. Open **Approve emails**. Read each draft, edit anything, then *Approve*,
   *Rewrite with Claude* or *Reject*.
2. Check **Overview → Hot leads** for replies; their answer drafts are in
   the approval queue too.
3. Work the **Call / WhatsApp list** for businesses without email.
4. Add businesses you find yourself (including foreign ones) with
   **Leads → Add lead**.

## Settings worth knowing

| Setting | Default | Notes |
|---|---|---|
| Daily send limit | 20 | Keep it 10–20 for a new Gmail; raise slowly. |
| Min fit score | 60 | Leads below this are marked "Not a fit". |
| Follow-up days / max | 4 / 2 | Stops at the first reply. |
| Target categories | clothes, restaurants, beauty, schools, hotels | Any OpenStreetMap tag works. |

## Staying out of trouble

- Start slow: 5–10 emails a day for the first week or two, then increase.
- Read every draft. Claude is told never to invent facts, but you are the
  final check.
- The opt-out line at the bottom of each email is there on purpose; the
  agent honours "no thanks" automatically.
- Nigeria's data protection law (NDPA) and, for European prospects, GDPR
  apply to cold outreach — only email business addresses and stop when asked.
- OpenStreetMap's free search services ask for light use; the defaults keep
  well within that.

## Selling worldwide

NexReach works across many countries and sectors at once.

- **Markets page:** each country (US, UK, Canada, Australia, UAE, Ireland, New Zealand,
  Nigeria) has its own cities, time zone, phone format and currency. Switch each on or off;
  "Edit cities" adds or removes cities, and "+ Add a country" adds a new one.
- **Sectors** (Settings → Sectors to search): law firms, accountants, dentists, clinics,
  doctors, estate agents, financial advisers, insurance, architects, vets, HVAC, plumbers,
  roofers, electricians, car dealers, hotels and gyms by default. The agent rotates through
  every country × city × sector combination.
- **Offer:** list all your services with starting prices in US dollars. For each business,
  Claude picks the ONE service that fits it best. Prices are left out of first emails; they
  only come up when someone asks.
- **Sending hours:** cold emails go out 9:00–16:00 on weekdays **in each business's own
  time zone**. The agent has to be running then: US hours are roughly 15:00–22:00 Lagos
  time, UK hours 10:00–17:00. Australia and New Zealand fall in the Nigerian night.
- **Postal address:** required by law in most countries. Cold emails to those countries stay
  on hold until it's set in Settings.

Two quick switches:

```powershell
python manage.py apply_preset global    # US + UK on, high-value sectors, USD offer
python manage.py apply_preset nigeria   # Nigeria only, naira offer
```

US and UK are on by default. Each other country's card shows its cold-email rules; read them
before switching it on. This isn't legal advice. Mainland EU countries such as Germany are
deliberately left out because their rules are much stricter.

## More lead sources

**Foursquare (businesses).** A second business directory alongside OpenStreetMap, with
better phone, website, email and social data. The first 500 searches a month are free.
Create a free account at foursquare.com/developers, make a project, generate a Service API
key and add it to `.env` as `FOURSQUARE_API_KEY=...`. NexReach stops at the monthly limit
in Settings (450 by default) and keeps using OpenStreetMap.

**Countries.** 27 countries are set up; 8 are on by default (US, UK, Canada, Australia,
Ireland, New Zealand, UAE, Singapore). Turn others on from the Markets page ("Enable all"
turns everything on). Each country writes in its own language (German for Germany, and so
on) and shows its cold-email rules; many EU countries need consent for cold email, so lean
on direct messages and gigs there.

**Gigs (people asking for a developer).** The **Gigs** page collects `[Hiring]` posts from
the subreddits in Settings (r/forhire and r/freelance_forhire by default) and "SEEKING
FREELANCER" comments in Hacker News' monthly thread. Only posts that mention your keywords
are kept. Claude scores each one and writes a reply. You send it yourself: **Reddit
message** opens Reddit with the reply filled in, **Reply on Hacker News** copies it and
opens the reply box, and **Email them** (when the post lists an email) puts it in Approve
emails. Gigs are checked every 60 minutes, or run `python manage.py find_gigs`.
Also included: contract and freelance software listings from Remotive (checked 4 times a
day, always linking back to Remotive) and Mastodon posts on hiring hashtags that clearly ask
for a developer. Subreddits checked by default: forhire, freelance_forhire, hiring, jobbit
and WebDeveloperJobs.

## Finding emails

Each check-in, a free step (no Claude) scans up to 25 new businesses' websites in parallel for
emails, phone/WhatsApp numbers and social accounts. It checks the homepage, the contact/about
pages the site links to, and common paths; it decodes Cloudflare-protected and "name [at]
domain [dot] com" addresses, prefers the business's own role addresses (info@, hello@...),
and drops addresses whose domain can't receive mail. Map searches only collect businesses
that list a website or email. The agent keeps searching until the **daily email target**
(30 by default, on the dashboard) is reached. Claude only researches businesses you can
actually contact, emailable ones first.

Finding 30 emails a day is separate from sending: the daily send limit still decides how
many cold emails go out, so a new Gmail can warm up safely.

## Switching to your own domain later

Buy the domain, set up the mailbox (Zoho or Google Workspace), then change
`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `SMTP_HOST` and `IMAP_HOST` in
`.env` (e.g. `smtp.zoho.com` / `imap.zoho.com`). Nothing else changes.

## Tests

```powershell
python manage.py test agent
```

Claude, Gmail and the web are mocked, so the tests cost nothing and send
nothing.

## Project layout

```
agent/services/llm.py        Claude via `claude -p` (or the API)
agent/services/discovery.py  OpenStreetMap business search
agent/services/research.py   website checks + fit scoring
agent/services/writer.py     first emails, follow-ups, replies (always drafts)
agent/services/mailer.py     sends approved emails via Gmail SMTP
agent/services/inbox.py      reads replies via IMAP, classifies, drafts answers
agent/services/loop.py       the 15-minute check-in
agent/views.py + templates/  the dashboard
```
