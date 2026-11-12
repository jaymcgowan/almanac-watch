#!/usr/bin/env python3
"""Almanac watcher.

Runs every few minutes on GitHub Actions. Checks the sources that matter for
the Anthropic (ANT) IPO, writes anything new to feed/feed.json (which the
Almanac app reads) and pushes an alert through ntfy.

Standard library only, so the workflow needs no installs.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "state.json"
FEED_FILE = ROOT / "feed" / "feed.json"
CONFIG_FILE = ROOT / "config.json"

UK = ZoneInfo("Europe/London")
NY = ZoneInfo("America/New_York")

BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
# The SEC asks automated clients to identify themselves with a contact.
# Set the SEC_USER_AGENT secret to "Your Name you@yourdomain" to use your own.
SEC_UA = os.environ.get("SEC_USER_AGENT") or "AlmanacResearch research@example.com"

FEED_LIMIT = 200
SEEN_LIMIT = 400


# ---------------------------------------------------------------- plumbing

def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def get(url: str, *, ua: str = BROWSER_UA, timeout: int = 20, accept: str = "*/*") -> bytes:
    # curl, not urllib: the SEC and Nasdaq turn away Python's default client.
    out = subprocess.run(
        ["curl", "-sS", "-f", "--compressed", "-m", str(timeout),
         "-A", ua, "-H", f"Accept: {accept}", "-H", "Accept-Language: en-GB,en;q=0.9", url],
        capture_output=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode(errors="ignore").strip() or f"curl exit {out.returncode}")
    return out.stdout


def get_json(url: str, **kw):
    return json.loads(get(url, **kw))


def load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


class Run:
    """Collects events from every check, then writes and sends them once."""

    def __init__(self, state: dict, config: dict, seeding: bool):
        self.state = state
        self.config = config
        self.seeding = seeding
        self.events: list[dict] = []
        self.errors: list[str] = []

    def event(self, *, kind: str, badge: str, title: str, body: str,
              source: str, url: str = "", priority: int = 3, key: str = "") -> None:
        stamp = now_utc()
        self.events.append({
            "id": key or f"{kind}-{stamp.strftime('%Y%m%d%H%M%S')}-{len(self.events)}",
            "time": stamp.isoformat(timespec="seconds"),
            "kind": kind,
            "badge": badge,          # Filed · Exchange · Headline · Odds · Market
            "title": title,
            "body": body,
            "source": source,
            "url": url,
            "priority": priority,    # ntfy scale: 1 min … 5 urgent
        })

    def guard(self, name: str, fn) -> None:
        try:
            fn(self)
        except Exception as exc:  # one source failing must never stop the others
            self.errors.append(f"{name}: {exc.__class__.__name__}: {exc}")


# ---------------------------------------------------------------- SEC EDGAR

KEY_FORMS = {
    "S-1": "Public registration statement (the IPO paperwork is now public)",
    "S-1/A": "Amended registration statement",
    "424B4": "Final prospectus (the priced deal)",
    "424B1": "Final prospectus",
    "424B3": "Prospectus supplement",
    "FWP": "Free writing prospectus",
    "8-A12B": "Exchange registration (listing is close)",
    "RW": "Registration withdrawal",
    "EFFECT": "Registration declared effective",
    "S-8": "Employee share registration",
}


def check_edgar(run: Run) -> None:
    cik = run.config["sec_cik"]
    data = get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", ua=SEC_UA, accept="application/json")
    recent = data["filings"]["recent"]
    first = run.seeding or "edgar_seen" not in run.state  # never alert on history
    seen = set(run.state.setdefault("edgar_seen", []))
    for i, acc in enumerate(recent["accessionNumber"]):
        if acc in seen:
            continue
        seen.add(acc)
        if first:
            continue
        form = recent["form"][i]
        doc = recent["primaryDocument"][i]
        folder = acc.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{doc}"
        label = KEY_FORMS.get(form, "New filing")
        high = form in {"424B4", "424B1", "RW", "EFFECT", "S-1/A", "8-A12B"}
        run.event(kind="filing", badge="Filed", key=f"edgar-{acc}",
                  title=f"Anthropic filed {form}",
                  body=f"{label}. Filed {recent['filingDate'][i]}. Open it to see what changed.",
                  source="SEC EDGAR", url=url, priority=5 if high else 3)
    run.state["edgar_seen"] = sorted(seen)[-SEEN_LIMIT:]


# ---------------------------------------------------------------- Nasdaq IPO calendar

def check_nasdaq(run: Run) -> None:
    ticker = run.config["ticker"]
    month = now_utc().astimezone(NY).strftime("%Y-%m")
    data = get_json(f"https://api.nasdaq.com/api/ipo/calendar?date={month}", accept="application/json, text/plain, */*")["data"] or {}
    found = None
    for section, rows in (("priced", (data.get("priced") or {}).get("rows")),
                          ("upcoming", ((data.get("upcoming") or {}).get("upcomingTable") or {}).get("rows")),
                          ("filed", (data.get("filed") or {}).get("rows"))):
        for row in rows or []:
            if (row.get("proposedTickerSymbol") or "").upper() == ticker:
                found = (section, row)
                break
        if found:
            break
    if not found:
        return
    section, row = found
    snap = {
        "section": section,
        "price": row.get("proposedSharePrice"),
        "shares": row.get("sharesOffered"),
        "date": row.get("expectedPriceDate") or row.get("pricedDate"),
    }
    last = run.state.get("nasdaq")
    run.state["nasdaq"] = snap
    if run.seeding or last == snap:
        return
    changes = [f"{k}: {last.get(k) if last else '–'} → {v}" for k, v in snap.items() if not last or last.get(k) != v]
    if section == "priced":
        title, prio = f"{ticker} has priced at ${snap['price']}", 5
        run.state.setdefault("manual", {})["ipo_price"] = _num(snap["price"])
    else:
        title, prio = f"Nasdaq updated the {ticker} listing", 4
    run.event(kind="exchange", badge="Exchange", key=f"nasdaq-{section}-{snap['price']}-{snap['date']}",
              title=title, body="; ".join(changes), source="Nasdaq IPO calendar",
              url="https://www.nasdaq.com/market-activity/ipos", priority=prio)


# ---------------------------------------------------------------- OpenAI IPO watch
# OpenAI filed confidentially on 8 Jun 2026, so nothing is public yet. The first public
# S-1, an exchange registration (8-A12B), a final prospectus (424B4) or a Nasdaq calendar
# row is the moment it becomes real. Same machinery as the ANT watch.

def check_openai(run: Run) -> None:
    cfg = run.config.get("openai_watch")
    if not cfg:
        return
    ignore = re.compile(cfg.get("ignore_names", "$^"), re.I)
    first = run.seeding or "openai_seen" not in run.state
    seen = set(run.state.setdefault("openai_seen", []))
    for form in cfg["edgar_forms"]:
        url = ("https://www.sec.gov/cgi-bin/browse-edgar?" + urllib.parse.urlencode(
            {"company": cfg["edgar_company"], "type": form, "owner": "include", "count": 40,
             "action": "getcompany", "output": "atom"}))
        xml = get(url, ua=SEC_UA, accept="application/atom+xml").decode("utf-8", "ignore")
        for entry in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
            title = html.unescape(_tag(entry, "title"))
            link = re.search(r'<link[^>]*href="([^"]+)"', entry)
            eid = _tag(entry, "id") or (link.group(1) if link else title)
            if not title or eid in seen:
                continue
            seen.add(eid)
            if first or ignore.search(title):
                continue
            run.event(kind="filing", badge="Filed", key="openai-" + hashlib.sha1(eid.encode()).hexdigest()[:16],
                      title="OpenAI IPO: " + re.sub(r"\s*\(\d{10}\).*$", "", title),
                      body=f"{KEY_FORMS.get(form, 'New filing')}. The OpenAI listing is now public on EDGAR.",
                      source="SEC EDGAR", url=link.group(1) if link else url, priority=5)
    run.state["openai_seen"] = sorted(seen)[-SEEN_LIMIT:]

    month = now_utc().astimezone(NY).strftime("%Y-%m")
    data = get_json(f"https://api.nasdaq.com/api/ipo/calendar?date={month}", accept="application/json, text/plain, */*")["data"] or {}
    for section, rows in (("priced", (data.get("priced") or {}).get("rows")),
                          ("upcoming", ((data.get("upcoming") or {}).get("upcomingTable") or {}).get("rows")),
                          ("filed", (data.get("filed") or {}).get("rows"))):
        for row in rows or []:
            name = row.get("companyName") or ""
            if cfg["calendar_name"].lower() not in name.lower() or ignore.search(name):
                continue
            key = f"openai-cal-{section}-{row.get('proposedTickerSymbol')}-{row.get('proposedSharePrice')}"
            if key in seen:
                continue
            seen.add(key)
            run.state["openai_seen"] = sorted(seen)[-SEEN_LIMIT:]
            if first:
                continue
            run.event(kind="exchange", badge="Exchange", key=key,
                      title=f"OpenAI is on the IPO calendar ({section})",
                      body=f"{name} · ticker {row.get('proposedTickerSymbol') or 'TBC'} · price {row.get('proposedSharePrice') or 'TBC'}",
                      source="Nasdaq IPO calendar", url="https://www.nasdaq.com/market-activity/ipos", priority=5)


def _num(value) -> float | None:
    try:
        return float(re.sub(r"[^0-9.]", "", str(value).split("-")[0]))
    except ValueError:
        return None


# ---------------------------------------------------------------- Polymarket

def check_polymarket(run: Run) -> None:
    step = run.config["odds_alert_points"] / 100
    store = run.state.setdefault("odds", {})
    for slug in run.config["polymarket_slugs"]:
        events = get_json(f"https://gamma-api.polymarket.com/events?slug={urllib.parse.quote(slug)}")
        if not events:
            continue
        ev = events[0]
        board = {}
        for m in ev.get("markets", []):
            if m.get("closed"):
                continue
            prices = json.loads(m.get("outcomePrices") or "[]")
            if prices:
                board[m.get("groupItemTitle") or m.get("question", "")[:60]] = round(float(prices[0]), 4)
        if not board:
            continue
        last = store.get(slug)
        leader = max(board, key=board.get)
        if last is None or run.seeding:
            store[slug] = {"board": board, "leader": leader}
            continue
        moves = []
        for name, p in board.items():
            before = last["board"].get(name)
            if before is not None and abs(p - before) >= step:
                moves.append(f"{name}: {before:.0%} → {p:.0%}")
        if moves or leader != last.get("leader"):
            head = f"New favourite: {leader} ({board[leader]:.0%})" if leader != last.get("leader") else f"Favourite: {leader} ({board[leader]:.0%})"
            run.event(kind="odds", badge="Odds", key=f"poly-{slug}-{now_utc():%Y%m%d%H%M}",
                      title=f"Odds moved · {ev.get('title', slug)}",
                      body=head + (". " + "; ".join(moves) if moves else ""),
                      source="Polymarket", url=f"https://polymarket.com/event/{slug}", priority=3)
            store[slug] = {"board": board, "leader": leader}  # only re-baseline after an alert


# ---------------------------------------------------------------- headlines

def check_news(run: Run) -> None:
    outlets = [o.lower() for o in run.config["news_outlets"]]
    urgent = re.compile(run.config["news_urgent_pattern"], re.I)
    first = run.seeding or "news_seen" not in run.state
    seen = run.state.setdefault("news_seen", [])
    seen_set = set(seen)
    fresh = []
    for query in run.config["news_queries"]:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": query, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"})
        xml = get(url).decode("utf-8", "ignore")
        for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
            title = html.unescape(_tag(item, "title"))
            link = _tag(item, "link")
            outlet = html.unescape(_tag(item, "source"))
            published = _when(_tag(item, "pubDate"))
            if not title or link in seen_set:
                continue
            seen_set.add(link)
            seen.append(link)
            if first:
                continue
            if not any(o in outlet.lower() for o in outlets):
                continue
            if published and now_utc() - published > dt.timedelta(hours=run.config.get("news_max_age_hours", 6)):
                continue
            fresh.append((title, outlet, link))
    run.state["news_seen"] = seen[-SEEN_LIMIT:]
    for title, outlet, link in fresh:
        clean = re.sub(r"\s+-\s+[^-]+$", "", title)
        run.event(kind="headline", badge="Headline", key="news-" + hashlib.sha1(link.encode()).hexdigest()[:16],
                  title=clean, body=f"{outlet}. Single report until confirmed by a filing or a second outlet.",
                  source=outlet, url=link, priority=4 if urgent.search(clean) else 2)


def _when(text: str) -> dt.datetime | None:
    try:
        return email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None


def _tag(block: str, name: str) -> str:
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
    return (m.group(1).strip() if m else "").replace("<![CDATA[", "").replace("]]>", "")


# ---------------------------------------------------------------- prices

def yahoo(symbol: str) -> dict | None:
    for host in ("query1", "query2"):
        try:
            data = get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=5d&interval=1d")
            result = (data.get("chart") or {}).get("result")
            if result:
                return result[0]
        except Exception:
            continue
    return None


def check_markets(run: Run) -> None:
    today = now_utc().astimezone(NY).date().isoformat()
    fired = run.state.setdefault("price_alerts", {})
    if fired.get("day") != today:
        fired.clear()
        fired["day"] = today
    for rule in run.config["price_rules"]:
        chart = yahoo(rule["symbol"])
        if not chart:
            continue
        meta = chart["meta"]
        price, prev = meta.get("regularMarketPrice"), meta.get("chartPreviousClose") or meta.get("previousClose")
        traded = meta.get("regularMarketTime")
        if not price or not prev or not traded:
            continue
        if dt.datetime.fromtimestamp(traded, NY).date().isoformat() != today:
            continue  # no trading yet today
        if rule.get("kind") == "level":
            for level in rule["above"]:
                tag = f"{rule['symbol']}>{level}"
                if price >= level and tag not in fired:
                    fired[tag] = True
                    if not run.seeding:
                        run.event(kind="market", badge="Market", title=f"{rule['name']} above {level}",
                                  body=f"{rule['name']} at {price:.2f}. {rule.get('why', '')}".strip(),
                                  source="Yahoo Finance", priority=4)
            continue
        change = (price - prev) / prev * 100
        for level in rule.get("down", []):
            tag = f"{rule['symbol']}<-{level}"
            if change <= -level and tag not in fired:
                fired[tag] = True
                if not run.seeding:
                    run.event(kind="market", badge="Market", title=f"{rule['name']} down {change:.1f}% today",
                              body=f"{rule['name']} at {price:,.2f}. {rule.get('why', '')}".strip(),
                              source="Yahoo Finance", priority=4 if level >= rule["down"][-1] else 3)
        for level in rule.get("up", []):
            tag = f"{rule['symbol']}>+{level}"
            if change >= level and tag not in fired:
                fired[tag] = True
                if not run.seeding:
                    run.event(kind="market", badge="Market", title=f"{rule['name']} up {change:.1f}% today",
                              body=f"{rule['name']} at {price:,.2f}. {rule.get('why', '')}".strip(),
                              source="Yahoo Finance", priority=3)


def check_ant(run: Run) -> None:
    """Listing-day watch: first trade, then moves against the IPO price."""
    chart = yahoo(run.config["ticker"])
    if not chart:
        return
    meta = chart["meta"]
    price, traded = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if not price or not traded:
        return
    ant = run.state.setdefault("ant", {})
    ipo = (run.state.get("manual") or {}).get("ipo_price") or run.config.get("ipo_price")
    if not ant.get("first_trade"):
        ant["first_trade"] = {"price": price, "time": traded}
        if not run.seeding:
            vs = f" ({(price - ipo) / ipo:+.1%} vs ${ipo:.2f} offer)" if ipo else ""
            run.event(kind="listing", badge="Market", key="ant-first-trade",
                      title=f"ANT is trading: ${price:.2f}{vs}",
                      body="First trade is in. Offer price, first trade and live price are three different numbers; Almanac shows all three.",
                      source="Yahoo Finance", priority=5)
        return
    if not ipo:
        return
    move = (price - ipo) / ipo * 100
    done = set(ant.setdefault("levels", []))
    for level in run.config["ant_levels"]:
        tag = f"{level:+d}"
        hit = move >= level if level > 0 else move <= level
        if hit and tag not in done:
            done.add(tag)
            if not run.seeding:
                word = "above" if level > 0 else "below"
                run.event(kind="listing", badge="Market", key=f"ant-{tag}",
                          title=f"ANT {abs(move):.0f}% {word} its offer price",
                          body=f"${price:.2f} against the ${ipo:.2f} offer.",
                          source="Yahoo Finance", priority=4)
    ant["levels"] = sorted(done)


# ---------------------------------------------------------------- output

def notify(events: list[dict], topic: str) -> None:
    for ev in events:
        payload = {
            "topic": topic,
            "title": ev["title"],
            "message": f"{ev['body']}\n[{ev['badge']} · {ev['source']}]",
            "priority": ev["priority"],
            "tags": [{"Filed": "page_facing_up", "Exchange": "bell", "Headline": "newspaper",
                      "Odds": "bar_chart", "Market": "chart_with_upwards_trend"}.get(ev["badge"], "bell")],
        }
        if ev.get("url"):
            payload["click"] = ev["url"]
        req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()


def main() -> int:
    config = load(CONFIG_FILE, None)
    if config is None:
        print("config.json missing", file=sys.stderr)
        return 1
    state = load(STATE_FILE, {})
    seeding = not state.get("seeded")
    run = Run(state, config, seeding)

    run.guard("edgar", check_edgar)
    run.guard("nasdaq", check_nasdaq)
    run.guard("openai", check_openai)
    run.guard("polymarket", check_polymarket)
    run.guard("news", check_news)
    run.guard("markets", check_markets)
    if now_utc().astimezone(UK).date() >= dt.date.fromisoformat(config["listing_date"]):
        run.guard("ant", check_ant)

    if seeding:
        state["seeded"] = now_utc().isoformat(timespec="seconds")
        run.event(kind="system", badge="Exchange", key="watcher-online",
                  title="Almanac watcher is live",
                  body="Watching SEC filings (Anthropic and OpenAI), the Nasdaq IPO calendar, Polymarket odds, headlines and prices every few minutes.",
                  source="Almanac", priority=3)

    feed = load(FEED_FILE, {"updated": None, "items": []})
    have = {i["id"] for i in feed["items"]}
    new = [e for e in run.events if e["id"] not in have]
    if new:
        feed["items"] = (new[::-1] + feed["items"])[:FEED_LIMIT]
        feed["updated"] = now_utc().isoformat(timespec="seconds")
        save(FEED_FILE, feed)
    state["last_run"] = {"at": now_utc().isoformat(timespec="seconds"), "errors": run.errors}
    save(STATE_FILE, state)

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if new and topic:
        notify(new, topic)
    for e in new:
        print(f"[{e['badge']}] {e['title']} | {e['body']}")
    for err in run.errors:
        print("error:", err, file=sys.stderr)
    if not topic:
        print("NTFY_TOPIC not set: alerts written to the feed only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
