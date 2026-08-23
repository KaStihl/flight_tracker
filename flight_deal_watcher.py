"""
flight_deal_watcher.py

Scans cheapest cached flight prices FROM a set of departure airports (AT, DE, HU,
PL, CZ, SK, FR) TO anywhere, using the Travelpayouts Data API, builds a rolling
price baseline per route in Postgres (Supabase), and emails an alert — with a
direct booking-search link — when a fare drops well below its own historic
baseline (possible error fare / genuine deal).

Why "baseline vs anywhere" instead of a fixed price threshold:
A flat "alert under 100 EUR" misses good long-haul deals and floods you with
noise on cheap short-haul routes. Comparing each route's current price to its
OWN rolling median catches genuine anomalies regardless of distance.

Also pulls in a second, complementary layer: RSS feeds from a couple of known
manually-curated deal sites (The Flight Deal, Secret Flying). These are
community/editorial deal spotters, not price-anomaly algorithms — they catch
things a pure stats-based scan misses, but most of their volume is
US-departure content, so entries get filtered by keyword match against your
tracked origin cities/countries before making it into your email. RSS entries
already carry their own direct link to the source post.

NOTE on RSS URLs: https://www.theflightdeal.com/feed is confirmed current as
of their 2023 site update. The Secret Flying feed URL below was found via a
third-party feed directory, not fetched and verified directly — open it in a
browser once before relying on it.

Direct flight links: for the Travelpayouts price anomalies, the email
includes a generated Aviasales search-results link (route + dates prefilled)
built from Travelpayouts' documented URL scheme. It is NOT wrapped with your
affiliate marker by default — see build_aviasales_link() for how to add one
if you want affiliate credit on bookings made through it.

Architecture notes:
- Designed to run as a stateless scheduled job (e.g. GitHub Actions cron).
- All state (price history, sent alerts, seen RSS entries) lives in Postgres
  (Supabase free tier), never on local disk, so the GitHub Actions container
  can be thrown away between runs without losing the baseline.
- The baseline needs a few runs of history before it can flag anything, so
  don't expect alerts on day one — that's expected, not a bug.

Requirements: pip install requests feedparser psycopg2-binary
Env vars required:
  TRAVELPAYOUTS_TOKEN  - your Travelpayouts Data API token
  GMAIL_ADDRESS        - the Gmail account the alert is sent FROM
  GMAIL_APP_PASSWORD   - a Gmail App Password (NOT your normal password)
  DATABASE_URL         - Supabase Postgres connection string
Optional:
  ALERT_TO             - who receives the alert (defaults to GMAIL_ADDRESS)
  AVIASALES_MARKER     - your Travelpayouts partner ID, to earn affiliate
                          credit on links clicked from the email (optional)
"""

import os
import statistics
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timezone

import requests
import feedparser
import psycopg2

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

TRAVELPAYOUTS_TOKEN = os.environ["TRAVELPAYOUTS_TOKEN"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_TO = os.environ.get("ALERT_TO", GMAIL_ADDRESS)
DATABASE_URL = os.environ["DATABASE_URL"]
AVIASALES_MARKER = os.environ.get("AVIASALES_MARKER")  # optional

CURRENCY = "eur"

# Departure airports: SK, AT, DE, HU, PL, CZ, FR — extend/trim as you like.
ORIGINS = {
    "BTS": "Bratislava",
    "VIE": "Vienna",
    "FRA": "Frankfurt",
    "MUC": "Munich",
    "BER": "Berlin",
    "DUS": "Dusseldorf",
    "BUD": "Budapest",
    "WAW": "Warsaw",
    "KRK": "Krakow",
    "PRG": "Prague",
    "CDG": "Paris (CDG)",
    "ORY": "Paris (Orly)",
    "LYS": "Lyon",
}

# Anomaly-detection thresholds — tune these once you see real data.
MIN_SAMPLES_FOR_BASELINE = 5      # need this many prior observations per route
DEAL_RATIO_THRESHOLD = 0.55       # alert if price <= 55% of rolling median
MIN_ABSOLUTE_SAVING_EUR = 30      # ignore noise on already-cheap routes

API_BASE = "https://api.travelpayouts.com/v1/prices/cheap"

# RSS deal feeds — see NOTE in the module docstring about verifying these.
RSS_FEEDS = [
    "https://www.theflightdeal.com/feed",
    "https://www.secretflying.com/feed/",
]

# Keywords used to match RSS post titles/summaries against our tracked
# origins. Built from ORIGINS city names + country names, lowercased. Add
# synonyms here if a country/city keeps getting missed (e.g. "prague" vs
# "praha").
RSS_KEYWORDS = [name.lower() for name in ORIGINS.values()] + [
    "austria", "germany", "hungary", "poland", "czech", "slovakia", "france",
    "vienna", "warsaw", "budapest", "prague", "paris", "berlin", "frankfurt",
    "munich",
]

# ---------------------------------------------------------------------------
# DB LAYER (Postgres / Supabase)
# ---------------------------------------------------------------------------

def get_db_connection():
    """
    Connects to Supabase Postgres using the DATABASE_URL secret.
    Get this string from: Supabase project -> Settings -> Database ->
    Connection string -> URI (use the "Session pooler" one for scripts that
    run briefly and disconnect, which is exactly this use case).
    """
    conn = psycopg2.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id SERIAL PRIMARY KEY,
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                price REAL NOT NULL,
                trip_type TEXT NOT NULL,       -- 'one_way' or 'return'
                found_at TEXT NOT NULL,
                checked_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS alerts_sent (
                id SERIAL PRIMARY KEY,
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                price REAL NOT NULL,
                sent_at TEXT NOT NULL,
                UNIQUE(origin, destination, price)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rss_seen (
                id SERIAL PRIMARY KEY,
                entry_link TEXT NOT NULL UNIQUE,
                seen_at TEXT NOT NULL
            )
        """)
    conn.commit()
    return conn


def save_price(conn, origin, destination, price, trip_type, found_at):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO price_history (origin, destination, price, trip_type, found_at, checked_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (origin, destination, price, trip_type, found_at, datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()


def get_baseline(conn, origin, destination, trip_type):
    """Rolling median of historic prices for this route, BEFORE today's fetch."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT price FROM price_history WHERE origin=%s AND destination=%s AND trip_type=%s",
            (origin, destination, trip_type),
        )
        rows = cur.fetchall()
    prices = [r[0] for r in rows]
    if len(prices) < MIN_SAMPLES_FOR_BASELINE:
        return None, len(prices)
    return statistics.median(prices), len(prices)


def already_alerted(conn, origin, destination, price):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM alerts_sent WHERE origin=%s AND destination=%s AND price=%s",
            (origin, destination, price),
        )
        row = cur.fetchone()
    return row is not None


def mark_alerted(conn, origin, destination, price):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO alerts_sent (origin, destination, price, sent_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (origin, destination, price, datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()


def is_rss_entry_seen(conn, link):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM rss_seen WHERE entry_link=%s", (link,))
        row = cur.fetchone()
    return row is not None


def mark_rss_entry_seen(conn, link):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rss_seen (entry_link, seen_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (link, datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()

# ---------------------------------------------------------------------------
# AVIASALES DEEP LINK
# ---------------------------------------------------------------------------

def build_aviasales_link(origin, destination, depart_at, return_at=None, adults=1):
    """
    Builds a direct link to the Aviasales search-results page, prefilled with
    route + dates, using Travelpayouts' documented URL scheme:
      https://www.aviasales.com/search/{ORIGIN}{DDMM}{DEST}{DDMM?}{passengers}

    depart_at / return_at are ISO datetime strings as returned by the
    Travelpayouts API (e.g. "2026-11-03T14:20:00Z").

    This link works and is bookable as-is with no affiliate tracking. If you
    want affiliate credit for bookings made through it, set the
    AVIASALES_MARKER env var to your Travelpayouts partner ID — appended
    below as a query param, which is the general Travelpayouts tracking
    convention. Verify it actually attributes correctly against your
    Travelpayouts stats before relying on it for real income tracking.
    """
    try:
        depart_dt = datetime.fromisoformat(depart_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None  # can't build a reliable link without a real date

    code = f"{origin}{depart_dt.strftime('%d%m')}{destination}"

    if return_at:
        try:
            return_dt = datetime.fromisoformat(return_at.replace("Z", "+00:00"))
            code += return_dt.strftime("%d%m")
        except (ValueError, AttributeError):
            pass  # fall back to a one-way-shaped link

    code += str(adults)

    url = f"https://www.aviasales.com/search/{code}"
    if AVIASALES_MARKER:
        url += f"?marker={AVIASALES_MARKER}"
    return url

# ---------------------------------------------------------------------------
# TRAVELPAYOUTS FETCH
# ---------------------------------------------------------------------------

def fetch_cheapest_from_origin(origin, trip_type="one_way"):
    """
    Pulls cached cheapest fares FROM `origin` TO ANY destination.
    destination='-' is the Travelpayouts wildcard meaning 'all routes' — this
    is what gives 'anywhere' coverage without looping over destinations.

    Leaving depart_date/return_date empty returns the cheapest cached fare
    across whatever dates Travelpayouts has cached per destination, which
    covers 'any date' for a first pass.

    NOTE on one-way vs return: the Data API's round-trip vs one-way behavior
    depends on whether a return_date is present. The exact response shape
    for a wildcard destination with a real return_date isn't nailed down
    here — treat the 'return' pass as a TODO to verify against a couple of
    real responses (print(payload) once) before trusting it, and adjust the
    params below accordingly.
    """
    params = {
        "origin": origin,
        "destination": "-",
        "currency": CURRENCY,
        "token": TRAVELPAYOUTS_TOKEN,
    }
    if trip_type == "return":
        # TODO: verify this actually forces round-trip pricing for a wildcard
        # destination — Travelpayouts docs are written around fixed routes.
        # A rolling month a few weeks out is a reasonable first guess:
        next_month = (datetime.now(timezone.utc).month % 12) + 1
        params["return_date"] = f"{datetime.now(timezone.utc).year}-{next_month:02d}"

    resp = requests.get(API_BASE, params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    results = []
    if not payload.get("success"):
        return results

    for destination, entries in payload.get("data", {}).items():
        for _, entry in entries.items():
            depart_at = entry.get("departure_at", datetime.now(timezone.utc).isoformat())
            return_at = entry.get("return_at")  # present for round-trip entries
            results.append({
                "destination": destination,
                "price": entry["price"],
                "found_at": depart_at,
                "link": build_aviasales_link(origin, destination, depart_at, return_at),
            })
    return results

# ---------------------------------------------------------------------------
# RSS FETCH (community/editorial deal sites)
# ---------------------------------------------------------------------------

def fetch_rss_deals(conn):
    """
    Pulls entries from RSS_FEEDS, keeps only ones whose title or summary
    mentions one of our tracked cities/countries, and skips entries already
    seen on a previous run (tracked in rss_seen). Each entry's own link
    (entry.link) is the direct link to the deal post.

    This is intentionally a simple substring match, not NLP — it will miss
    posts that mention only an airport code (e.g. "VIE") without the city
    name, and can false-positive on unrelated mentions. Good enough as a
    first pass; tighten later if it's too noisy.
    """
    matches = []
    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"[WARN] RSS fetch failed for {feed_url}: {e}")
            continue

        if parsed.bozo and not parsed.entries:
            print(f"[WARN] RSS feed looks broken or unreachable: {feed_url}")
            continue

        for entry in parsed.entries:
            link = entry.get("link", "")
            if not link or is_rss_entry_seen(conn, link):
                continue

            title = entry.get("title", "")
            summary = entry.get("summary", "")
            haystack = f"{title} {summary}".lower()

            if any(keyword in haystack for keyword in RSS_KEYWORDS):
                matches.append({
                    "title": title,
                    "link": link,
                    "source": feed_url,
                })

            # Mark as seen regardless of match, so we don't re-scan it forever.
            mark_rss_entry_seen(conn, link)

    return matches

# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------

def send_alert_email(deals, rss_matches):
    if not deals and not rss_matches:
        return

    lines = []

    if deals:
        lines.append("Cenove anomalie (Travelpayouts, algoritmicky nalezene):\n")
        for d in deals:
            line = (
                f"  {d['origin']} -> {d['destination']}: {d['price']:.0f} {CURRENCY.upper()} "
                f"(baseline {d['baseline']:.0f}, -{d['discount_pct']:.0f}%) [{d['trip_type']}]"
            )
            lines.append(line)
            if d.get("link"):
                lines.append(f"    {d['link']}")
        lines.append("")

    if rss_matches:
        lines.append("Relevantne prispevky z deal-stranok (RSS):\n")
        for m in rss_matches:
            lines.append(f"  {m['title']}\n  {m['link']}")
        lines.append("")

    body = "\n".join(lines)

    total = len(deals) + len(rss_matches)
    msg = MIMEText(body)
    msg["Subject"] = f"Flight deal alert - {total} novych polozka/iek"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ALERT_TO

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    conn = get_db_connection()
    found_deals = []

    for origin in ORIGINS:
        for trip_type in ("one_way", "return"):
            try:
                fares = fetch_cheapest_from_origin(origin, trip_type)
            except requests.RequestException as e:
                print(f"[WARN] fetch failed for {origin} ({trip_type}): {e}")
                continue

            for fare in fares:
                destination = fare["destination"]
                price = fare["price"]
                found_at = fare["found_at"]

                baseline, sample_count = get_baseline(conn, origin, destination, trip_type)

                # Always store the observation — this is what builds the
                # baseline over time, so keep it even on the first (silent) runs.
                save_price(conn, origin, destination, price, trip_type, found_at)

                if baseline is None:
                    continue  # not enough history yet for this route

                discount_pct = (1 - price / baseline) * 100
                saving_eur = baseline - price

                is_deal = (
                    price <= baseline * DEAL_RATIO_THRESHOLD
                    and saving_eur >= MIN_ABSOLUTE_SAVING_EUR
                )

                if is_deal and not already_alerted(conn, origin, destination, price):
                    found_deals.append({
                        "origin": origin,
                        "destination": destination,
                        "price": price,
                        "baseline": baseline,
                        "discount_pct": discount_pct,
                        "trip_type": trip_type,
                        "link": fare.get("link"),
                    })
                    mark_alerted(conn, origin, destination, price)

    rss_matches = fetch_rss_deals(conn)

    if found_deals or rss_matches:
        send_alert_email(found_deals, rss_matches)
        print(f"Sent alert with {len(found_deals)} price deal(s) and {len(rss_matches)} RSS match(es).")
    else:
        print("Nothing above threshold this run.")

    conn.close()


if __name__ == "__main__":
    main()
