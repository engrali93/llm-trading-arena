#!/usr/bin/env python3
"""
LLM Trading Arena v2
An autonomous LLM trader (via Ollama) that may pick any coin from the Binance top-40 USDT pairs,
with free news + sentiment as hints. Fake money, SQLite ledger, live dashboard.
No exchange account, no API keys. Python standard library only.

    python arena.py --model deepseek-r1:14b     -> http://localhost:8001
"""
import argparse
import collections
import csv
import difflib
import email.utils
import html
import io
import json
import math
import os
import random
import re
import signal
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STOP = threading.Event()
UA = {"User-Agent": "Mozilla/5.0 (llm-trading-arena)"}
RESTART_LOCK = threading.Lock()
RESTART_REQ = {"req": None}         # pending {"start_cash", "fee_pct"} from POST /api/restart
WAKE = threading.Event()            # interrupts the between-turns nap when a restart is queued
DB_WRITE_LOCK = threading.RLock()   # held while the loop writes the ledger, so a shutdown never cuts a write in half
RUNTIME = {"db": None}              # ledger path, for the final WAL checkpoint on shutdown
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
# What the agent loop is doing right now, for the dashboard: trading / reflecting / waiting (for the next life) / dead.
AGENT = {"phase": "trading", "next_life_at": None, "reflecting_life": None}
LESSONS_SCHEMA = {
    "type": "object",
    "properties": {"lessons": {"type": "array", "minItems": 5, "maxItems": 10,
                               "items": {"type": "string", "maxLength": 220}}},
    "required": ["lessons"],
}
MERGE_SCHEMA = {
    "type": "object",
    "properties": {"lessons": {"type": "array", "minItems": 1, "maxItems": 10,
                               "items": {"type": "string", "maxLength": 220}}},
    "required": ["lessons"],
}
CORE_MAX = 10                       # size of the merged "core lessons" list
WISDOM_MAX_TOKENS = 480             # budget for the wisdom block in the system prompt (rough 4 chars per token)

BINANCE = "https://api.binance.com/api/v3"
FEEDS = [("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
         ("Cointelegraph", "https://cointelegraph.com/rss"),
         ("Decrypt", "https://decrypt.co/feed")]
STABLES = {"USDC", "FDUSD", "TUSD", "DAI", "USDP", "EUR", "USDE", "BUSD", "AEUR", "PYUSD", "XUSD", "USD1",
           "EURI", "UST", "USDS", "BFUSD", "RLUSD", "GBP", "TRY", "BRL", "USDT"}
TOKENIZED_GROUP = "TRD_GRP_261"      # exchangeInfo permission group carried by tokenized stocks/ETFs (e.g. MSTRB)
EXCLUDE_FILE = os.path.join(HERE, "exclude.txt")
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "orders": {"type": "array", "maxItems": 3, "items": {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["buy", "sell"]},
                           "symbol": {"type": "string"}, "usd": {"type": "number"}, "sell_pct": {"type": "number"}},
            "required": ["action", "symbol"]}},
        "watch": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
        "reason": {"type": "string", "maxLength": 200},
        "note": {"type": "string", "maxLength": 200},
    },
    "required": ["orders", "reason"],
}
COIN_NAMES = {"bitcoin": "BTC", "ethereum": "ETH", "ether": "ETH", "solana": "SOL", "ripple": "XRP",
              "dogecoin": "DOGE", "cardano": "ADA", "binance coin": "BNB", "tron": "TRX",
              "avalanche": "AVAX", "chainlink": "LINK", "polkadot": "DOT", "litecoin": "LTC",
              "shiba inu": "SHIB", "pepe": "PEPE", "toncoin": "TON", "stellar": "XLM",
              "near protocol": "NEAR", "uniswap": "UNI", "aptos": "APT", "arbitrum": "ARB",
              "hedera": "HBAR", "cosmos": "ATOM", "bitcoin cash": "BCH", "filecoin": "FIL",
              "polygon": "POL", "injective": "INJ", "aave": "AAVE", "ethereum classic": "ETC",
              "internet computer": "ICP", "bittensor": "TAO", "worldcoin": "WLD", "celestia": "TIA",
              "ethena": "ENA", "ondo": "ONDO", "render": "RENDER", "optimism": "OP", "sui": "SUI"}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_price(p):
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 100:
        return f"{p:.1f}"
    if p >= 1:
        return f"{p:.3f}"
    return f"{p:.6g}"


def fmt_vol(v):
    for div, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"${v / div:.1f}{s}"
    return f"${v:.0f}"


def base_of(sym):
    return sym[:-4] if sym.endswith("USDT") else sym


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url, timeout=10):
    return json.loads(http_get(url, timeout))


def load_excludes():
    """exclude.txt: one symbol or base asset per line (# comments allowed). Re-read on every universe rebuild."""
    try:
        with open(EXCLUDE_FILE, encoding="utf-8") as f:
            return {ln.split("#")[0].strip().upper() for ln in f if ln.split("#")[0].strip()}
    except OSError:
        return set()


# ───────────────────────── database (the ledger) ─────────────────────────
# One row per LIFE in `bots`: restarting archives the old row (ended/final_value) and inserts a new one,
# so every past life stays queryable through the bot_id on trades/equity/decisions/positions.
BOTS_DDL = """CREATE TABLE bots(
    id INTEGER PRIMARY KEY, name TEXT, model TEXT, cash REAL, alive INTEGER, started TEXT, died TEXT,
    ticks INTEGER DEFAULT 0, start_cash REAL, fee_rate REAL, ended TEXT, final_value REAL,
    learning INTEGER, summary TEXT, end_reason TEXT, reflected INTEGER DEFAULT 0, wisdom TEXT)"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS lessons(id INTEGER PRIMARY KEY, life_id INTEGER, lesson TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS core_history(id INTEGER PRIMARY KEY, life_id INTEGER, created TEXT, core TEXT);
CREATE TABLE IF NOT EXISTS positions(
    bot_id INTEGER, symbol TEXT, qty REAL, avg_cost REAL, PRIMARY KEY(bot_id, symbol));
CREATE TABLE IF NOT EXISTS trades(
    id INTEGER PRIMARY KEY, bot_id INTEGER, ts TEXT, turn INTEGER, action TEXT, symbol TEXT,
    qty REAL, price REAL, usd REAL, fee REAL, cash_after REAL, qty_after REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS equity(
    id INTEGER PRIMARY KEY, bot_id INTEGER, ts TEXT, btc_price REAL, cash REAL,
    positions_value REAL, equity REAL);
CREATE TABLE IF NOT EXISTS decisions(
    id INTEGER PRIMARY KEY, bot_id INTEGER, ts TEXT, turn INTEGER, action TEXT, orders TEXT,
    watch TEXT, reason TEXT, note TEXT, error TEXT, prompt_tokens INTEGER, eval_tokens INTEGER);
CREATE TABLE IF NOT EXISTS news(
    id INTEGER PRIMARY KEY, link TEXT UNIQUE, title TEXT, source TEXT, published TEXT,
    tags TEXT, fetched TEXT);
"""


def connect(path):
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db(path):
    con = connect(path)
    con.executescript(SCHEMA)
    have = {r["name"] for r in con.execute("PRAGMA table_info(decisions)")}
    for col, typ in (("thinking", "TEXT"), ("duration", "REAL"), ("stage2", "INTEGER")):   # migrate older ledgers in place
        if col not in have:
            con.execute(f"ALTER TABLE decisions ADD COLUMN {col} {typ}")
    if "last_price" not in {r["name"] for r in con.execute("PRAGMA table_info(positions)")}:
        con.execute("ALTER TABLE positions ADD COLUMN last_price REAL")     # lets exports value a finished life's holdings
    cols = {r["name"] for r in con.execute("PRAGMA table_info(bots)")}
    if not cols:                                                       # brand-new ledger
        con.execute(BOTS_DDL)
    elif "start_cash" not in cols:
        # Older single-life ledger: rebuild `bots` (drops the UNIQUE name, adds per-life settings).
        # The existing row becomes life #1; its start cash is read back from its first equity row.
        con.execute("ALTER TABLE bots RENAME TO bots_old")
        con.execute(BOTS_DDL)
        con.execute("""INSERT INTO bots(id, name, model, cash, alive, started, died, ticks, start_cash, fee_rate, ended, final_value)
                       SELECT id, name, model, cash, alive, started, died, ticks,
                              COALESCE((SELECT equity FROM equity WHERE bot_id=bots_old.id ORDER BY id LIMIT 1), 50.0), 0.001, NULL,
                              (SELECT equity FROM equity WHERE bot_id=bots_old.id ORDER BY id DESC LIMIT 1)
                       FROM bots_old""")
        con.execute("DROP TABLE bots_old")
    have_bots = {r["name"] for r in con.execute("PRAGMA table_info(bots)")}
    for col, typ in (("learning", "INTEGER"), ("summary", "TEXT"), ("end_reason", "TEXT"),
                     ("reflected", "INTEGER DEFAULT 0"), ("wisdom", "TEXT")):      # per-life memory columns
        if col not in have_bots:
            con.execute(f"ALTER TABLE bots ADD COLUMN {col} {typ}")
    # Lives that predate cross-life learning had no memory: mark them OFF so comparisons stay honest.
    con.execute("UPDATE bots SET learning=0 WHERE learning IS NULL")
    con.commit()
    return con


def get_setting(con, key):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(con, key, value):
    con.execute("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))


def current_bot(con):
    """The active life: settings.current_bot_id, else the newest life that hasn't been archived."""
    bid = get_setting(con, "current_bot_id")
    row = con.execute("SELECT * FROM bots WHERE id=?", (bid,)).fetchone() if bid else None
    if not row:
        row = con.execute("SELECT * FROM bots WHERE ended IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def sync_settings(con, cfg):
    """--cash/--fee only seed a brand-new ledger. After that the settings saved in SQLite win."""
    start_cash, fee_pct = get_setting(con, "start_cash"), get_setting(con, "fee_pct")
    if start_cash is None or fee_pct is None:
        cur = current_bot(con)
        start_cash, fee_pct = (cur["start_cash"], cur["fee_rate"] * 100) if cur else (cfg.start_cash, cfg.fee_pct)
        set_setting(con, "start_cash", start_cash)
        set_setting(con, "fee_pct", fee_pct)
        con.commit()
    cfg.start_cash, cfg.fee_pct = float(start_cash), float(fee_pct)
    cfg.fee_rate = cfg.fee_pct / 100


def get_toggle(con, name, default=True):
    value = get_setting(con, name)
    return default if value is None else value == "on"


def set_toggle(con, name, on):
    set_setting(con, name, "on" if on else "off")


def sync_toggles(con, cfg):
    """--learning / --auto-restart only change the saved toggle when passed explicitly; otherwise the saved value
    (default ON), which the dashboard can flip, wins."""
    for name, cli in (("learning", cfg.learning), ("auto_restart", cfg.auto_restart)):
        if cli is not None:
            set_toggle(con, name, cli == "on")
    con.commit()


def new_life(con, cfg, price):
    """Start a fresh life with the current cfg.start_cash / cfg.fee_rate and make it the active one.
    Whether it uses memory (learning) is decided now and stays fixed for the whole life."""
    model = "mock (random)" if cfg.mock else cfg.model
    learning = 1 if get_toggle(con, "learning") else 0
    cur = con.execute("INSERT INTO bots(name, model, cash, alive, started, ticks, start_cash, fee_rate, learning, reflected) "
                      "VALUES(?,?,?,1,?,0,?,?,?,0)",
                      (cfg.bot_name, model, cfg.start_cash, now_iso(), cfg.start_cash, cfg.fee_rate, learning))
    bid = cur.lastrowid
    con.execute("UPDATE bots SET wisdom=? WHERE id=?", (wisdom_section(con, {"id": bid, "learning": learning}), bid))
    set_setting(con, "current_bot_id", bid)
    con.execute("INSERT INTO equity(bot_id, ts, btc_price, cash, positions_value, equity) VALUES(?,?,?,?,?,?)",
                (bid, now_iso(), price, cfg.start_cash, 0.0, cfg.start_cash))
    con.commit()
    return dict(con.execute("SELECT * FROM bots WHERE id=?", (bid,)).fetchone())


def apply_pending_restart(con, cfg, market, bot):
    """Archive the current life and start a new one if POST /api/restart queued a request. Returns the new bot or None."""
    with RESTART_LOCK:
        req, RESTART_REQ["req"] = RESTART_REQ["req"], None
    if not req:
        return None
    pos = load_positions(con, bot["id"])
    final = bot["final_value"] if (not bot["alive"] and bot["final_value"] is not None) else total_value(bot["cash"], pos, market.prices)
    reason = "died" if not bot["alive"] else ("model change" if req.get("model_change") else "restarted")
    with DB_WRITE_LOCK:
        con.execute("UPDATE bots SET ended=?, final_value=?, end_reason=COALESCE(end_reason, ?) WHERE id=?",
                    (now_iso(), final, reason, bot["id"]))
        for sym in pos:                                 # remember the prices the final holdings were valued at
            con.execute("UPDATE positions SET last_price=? WHERE bot_id=? AND symbol=?",
                        (market.prices.get(sym), bot["id"], sym))
        con.commit()
    finish_life(con, cfg, market, bot["id"])            # summary + reflection (if memory was on) before the next life
    cfg.start_cash, cfg.fee_pct = req["start_cash"], req["fee_pct"]
    cfg.fee_rate = cfg.fee_pct / 100
    with DB_WRITE_LOCK:
        set_setting(con, "start_cash", cfg.start_cash)
        set_setting(con, "fee_pct", cfg.fee_pct)
        new = new_life(con, cfg, market.btc)
    print(f"\n>> Restart: life #{bot['id']} archived at ${final:.2f} after {bot['ticks']} turns. "
          f"New life #{new['id']}: start ${cfg.start_cash:.2f}, fee {cfg.fee_pct:g}%\n", flush=True)
    return new


def nap(seconds):
    """Sleep between turns; a queued restart (or shutdown) wakes it immediately."""
    WAKE.wait(max(0.0, seconds))
    WAKE.clear()


def graceful_exit(why):
    """Stop the whole process cleanly and quickly. It waits for a ledger write that is in progress (the loop holds
    DB_WRITE_LOCK while writing) but not for a slow model call, which has nothing half-written. Then it folds the WAL
    file into the database and exits. Runs in its own thread so the main thread can finish its write first."""
    STOP.set()
    WAKE.set()
    print(f"\n{why}: finishing any ledger write in progress, then stopping...", flush=True)
    with DB_WRITE_LOCK:
        try:
            if RUNTIME["db"]:
                c = sqlite3.connect(RUNTIME["db"], timeout=10)
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                c.close()
        except Exception as e:
            print(f"(checkpoint skipped: {e})", flush=True)
        print("Stopped. The ledger is saved; start again to continue this life.", flush=True)
        os._exit(0)


def load_positions(con, bot_id):
    return {r["symbol"]: {"qty": r["qty"], "avg": r["avg_cost"]} for r in con.execute(
        "SELECT symbol, qty, avg_cost FROM positions WHERE bot_id=? AND qty>0", (bot_id,))}


def save_positions(con, bot_id, positions, prices):
    con.execute("DELETE FROM positions WHERE bot_id=?", (bot_id,))
    con.executemany("INSERT INTO positions(bot_id, symbol, qty, avg_cost, last_price) VALUES(?,?,?,?,?)",
                    [(bot_id, s, p["qty"], p["avg"], prices.get(s)) for s, p in positions.items() if p["qty"] > 0])


# ───────────────────────── markets ─────────────────────────
class Market:
    """Binance public API: hourly top-40 USDT universe, all prices in one call per turn."""
    kind = "binance"
    label = "Binance top 40"

    def __init__(self, size=40):
        self.size = size
        self.prices = {}            # every symbol we've seen -> last price
        self.universe = []          # symbols, sorted by 24h quote volume
        self.uni_set = set()
        self.stats = {}             # symbol -> (24h change %, quote volume)
        self.uni_ts = 0.0
        self.stats_ts = 0.0
        self._klines = {}
        self.tokenized = set()      # tokenized stocks/ETFs, flagged by exchangeInfo

    def _load_tokenized(self):
        try:
            info = get_json(f"{BINANCE}/exchangeInfo?symbolStatus=TRADING", timeout=40)
            self.tokenized = {s["symbol"] for s in info["symbols"]
                              if any(TOKENIZED_GROUP in ps for ps in s.get("permissionSets", []))}
        except Exception as e:
            print(f"[market] exchangeInfo failed ({e}); relying on exclude.txt for tokenized stocks")

    def _rebuild(self, rows):
        out = []
        excl = load_excludes()
        for r in rows:
            s = r["symbol"]
            if not s.endswith("USDT"):
                continue
            b = s[:-4]
            if s in self.tokenized or s in excl or b in excl:
                continue
            if not re.fullmatch(r"[A-Z0-9]{2,12}", b) or b in STABLES:
                continue
            if b.endswith("USD") or b.startswith("USD") or "BULL" in b or "BEAR" in b:
                continue
            if re.fullmatch(r"[A-Z0-9]{3,}(UP|DOWN)", b):       # leveraged tokens (BTCUP, ETHDOWN)
                continue
            if float(r["count"]) <= 0:
                continue
            out.append(r)
        out.sort(key=lambda r: float(r["quoteVolume"]), reverse=True)
        return out[: self.size]

    def refresh(self, rebuild):
        rows = get_json(f"{BINANCE}/ticker/24hr", timeout=20)
        for r in rows:
            try:
                self.prices[r["symbol"]] = float(r["lastPrice"])
            except (KeyError, ValueError):
                pass
        if rebuild or not self.universe:
            self._load_tokenized()
            top = self._rebuild(rows)
            self.universe = [r["symbol"] for r in top]
            self.uni_set = set(self.universe)
            self.uni_ts = time.time()
        by = {r["symbol"]: r for r in rows}
        self.stats = {s: (float(by[s]["priceChangePercent"]), float(by[s]["quoteVolume"]))
                      for s in self.universe if s in by}
        self.stats_ts = time.time()

    def tick(self):
        """Refresh universe hourly (stats every 15 min) and all prices in one call. Never raises."""
        try:
            if time.time() - self.uni_ts > 3600 or not self.universe:
                self.refresh(True)
            elif time.time() - self.stats_ts > 900:
                self.refresh(False)
        except Exception as e:
            print(f"[market] universe refresh failed ({e}); keeping the old one")
            self.uni_ts = self.stats_ts = time.time() - 3000 if self.universe else 0.0
        try:
            for d in get_json(f"{BINANCE}/ticker/price", timeout=15):
                self.prices[d["symbol"]] = float(d["price"])
            return True
        except Exception as e:
            print(f"[market] price fetch failed ({e}); reusing last prices")
            return False

    def klines(self, sym):
        c = self._klines.get(sym)
        if c and time.time() - c[0] < 600:
            return c[1]
        k = get_json(f"{BINANCE}/klines?symbol={sym}&interval=1h&limit=24")
        closes = [float(x[4]) for x in k]
        self._klines[sym] = (time.time(), closes)
        return closes

    @property
    def btc(self):
        return self.prices.get("BTCUSDT")


class FakeMarket:
    """Offline fallback: 40 random-walk coins (--market random, or Binance unreachable)."""
    kind = "random"
    label = "SIM top 40"
    BASES = ("BTC ETH BNB SOL XRP DOGE ADA TRX AVAX LINK DOT LTC SHIB PEPE SUI TON XLM NEAR UNI APT ARB "
             "HBAR ATOM BCH FIL INJ OP AAVE ETC ICP RENDER FET TAO WLD SEI TIA JUP ENA ONDO POL").split()

    def __init__(self):
        rnd = random.Random()
        self.universe = [b + "USDT" for b in self.BASES]
        self.uni_set = set(self.universe)
        self.prices = {s: math.exp(rnd.uniform(-3, 11)) for s in self.universe}
        self.prices["BTCUSDT"] = 80000.0
        for s in ("KORUBUSDT", "MSTRBUSDT"):                       # excluded coins: priced but never buyable
            self.prices[s] = 100.0
        self.open = dict(self.prices)
        self.hist = {s: [p] for s, p in self.prices.items()}
        self.vol = {s: 2e9 / (i + 1) ** 1.6 for i, s in enumerate(self.universe)}   # tail falls below $50M
        self.stats = {}

    def tick(self):
        for s in self.universe:
            r = random.gauss(0, 0.006)
            if random.random() < 0.02:
                r += random.choice([-1, 1]) * random.uniform(0.03, 0.08)
            self.prices[s] = max(1e-6, self.prices[s] * math.exp(r))
            self.hist[s] = (self.hist[s] + [self.prices[s]])[-24:]
        self.stats = {s: ((self.prices[s] / self.open[s] - 1) * 100, self.vol[s]) for s in self.universe}
        return True

    def klines(self, sym):
        return self.hist[sym]

    @property
    def btc(self):
        return self.prices.get("BTCUSDT")


def make_market(cfg):
    if cfg.market == "binance":
        m = Market()
        try:
            m.refresh(True)
            m.tick()
            return m
        except Exception as e:
            print(f"[market] Binance unreachable ({e}); using the random-walk market instead.")
    m = FakeMarket()
    m.tick()
    return m


# ───────────────────────── news + mood ─────────────────────────
def build_tag_matchers(universe):
    bases = {base_of(s) for s in universe}
    sym_re = {b: re.compile(r"(?<![A-Za-z0-9])\$?" + re.escape(b) + r"(?![A-Za-z0-9])") for b in bases if len(b) >= 2}
    name_re = {re.compile(r"\b" + re.escape(n) + r"\b", re.I): b for n, b in COIN_NAMES.items() if b in bases}
    return sym_re, name_re


def tag_headline(title, universe):
    sym_re, name_re = build_tag_matchers(universe)
    tags = {b for b, rx in sym_re.items() if rx.search(title)}
    tags |= {b for rx, b in name_re.items() if rx.search(title)}
    return ",".join(sorted(tags))


def parse_rss(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate") or ""
        try:
            dt = email.utils.parsedate_to_datetime(pub)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            pub_iso = dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        except Exception:
            pub_iso = now_iso()
        if title and link:
            items.append((title, link, pub_iso))
    return items


def fetch_news(con, universe):
    added = 0
    for source, url in FEEDS:
        try:
            for title, link, pub in parse_rss(http_get(url, timeout=12)):
                if con.execute("SELECT 1 FROM news WHERE link=? OR title=?", (link, title)).fetchone():
                    continue
                con.execute("INSERT INTO news(link, title, source, published, tags, fetched) VALUES(?,?,?,?,?,?)",
                            (link, title, source, pub, tag_headline(title, universe), now_iso()))
                added += 1
        except Exception as e:
            print(f"[news] {source} failed: {type(e).__name__}: {str(e)[:80]}")
    con.commit()
    return added


def age_str(iso):
    try:
        s = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except Exception:
        return "?"
    if s < 3600:
        return f"{max(1, int(s // 60))}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


class Mood:
    """Fear & Greed (cached 1 h) and CoinGecko trending (cached 30 min). Failures leave None."""

    def __init__(self):
        self.fng = None
        self.trending = None
        self._fng_ts = 0.0
        self._tr_ts = 0.0

    def update(self):
        if time.time() - self._fng_ts > 3600:
            self._fng_ts = time.time()
            try:
                d = get_json("https://api.alternative.me/fng/")["data"][0]
                self.fng = {"value": int(d["value"]), "label": d["value_classification"]}
            except Exception as e:
                print(f"[mood] fear&greed failed: {type(e).__name__}")
        if time.time() - self._tr_ts > 1800:
            self._tr_ts = time.time()
            try:
                coins = get_json("https://api.coingecko.com/api/v3/search/trending")["coins"]
                self.trending = [{"symbol": c["item"]["symbol"].upper(), "name": c["item"]["name"]}
                                 for c in coins[:10]]
            except Exception as e:
                print(f"[mood] trending failed: {type(e).__name__}")


# ───────────────────────── the agent ─────────────────────────
def system_prompt(cfg, wisdom=""):
    if cfg.living_cost > 0:
        living = (f"Being alive costs ${cfg.living_cost:.2f} every turn, taken from your cash "
                  f"(or from your holdings if you have no cash).")
    else:
        living = "Staying alive is free; only fees and bad trades can hurt you."
    wisdom_block = f"\n{wisdom}\n" if wisdom else ""
    return f"""You are {cfg.bot_name}, an autonomous trader living inside a simulation.
Nobody will give you instructions after this message. Every decision is yours.

THE RULES OF YOUR WORLD
- You started with ${cfg.start_cash:.2f} in cash. You may trade ANY coin in the market list you are shown each turn (symbols like SOLUSDT).
- Coins with 24h volume below ${cfg.min_volume / 1e6:g}M are marked "(low volume, can't buy)": you can still sell them if you own them, but buys are rejected.
- Every trade costs a {cfg.fee_rate * 100:.2f}% fee.
- {living}
- If your total value (cash + all holdings) drops below ${cfg.death_line:.2f}, you die. Permanently.
- Your goal: stay alive and grow your money for as long as possible.
- Each turn you see the market, news and your own portfolio, then place up to 3 orders (sells run before buys). An empty list means hold.
- You can buy only coins in the current list, at least $1 each. A buy larger than your cash is shrunk to what you can afford. You can sell any coin you own.
- Orders the referee rejects are reported back to you next turn.
- "watch" lists up to 5 coins whose last 24 hourly prices you want to see next turn.
{wisdom_block}
Reply with ONLY a JSON object, nothing else:
{{"orders": [{{"action": "buy", "symbol": "<SYMBOL>USDT", "usd": <dollars>}}, {{"action": "sell", "symbol": "<SYMBOL>USDT", "sell_pct": <1-100>}}], "watch": ["<SYMBOL>USDT"], "reason": "<one short sentence>", "note": "<optional memo to your future self, or empty string>"}}"""


def total_value(cash, positions, prices):
    return cash + sum(p["qty"] * prices.get(s, p["avg"]) for s, p in positions.items())


def turn_prompt(con, cfg, market, mood, bot, turn, positions, watch):
    prices = market.prices
    parts = [f"Turn {turn} ({now_iso()} UTC)"]

    try:
        lines = []
        for s in market.universe:
            chg, vol = market.stats.get(s, (0.0, 0.0))
            lines.append(f"{base_of(s)} {fmt_price(prices.get(s, 0))} {chg:+.1f}% vol {fmt_vol(vol)}"
                         + (" (low volume, can't buy)" if vol < cfg.min_volume else ""))
        ranked = sorted(market.stats.items(), key=lambda kv: kv[1][0], reverse=True)
        gain = ", ".join(f"{base_of(s)} {v[0]:+.1f}%" for s, v in ranked[:5])
        lose = ", ".join(f"{base_of(s)} {v[0]:+.1f}%" for s, v in ranked[-5:][::-1])
        parts.append("MARKET OVERVIEW (coin, price in USDT, 24h change, 24h volume; trade as COINUSDT)\n"
                     + "\n".join(lines) + f"\nTop gainers 24h: {gain}\nTop losers 24h: {lose}")
    except Exception:
        parts.append("MARKET OVERVIEW\n(unavailable)")

    mood_lines = []
    mood_lines.append("Fear & Greed index: " + (f"{mood.fng['value']}/100 ({mood.fng['label']})"
                                                if mood.fng else "(unavailable)"))
    mood_lines.append("CoinGecko trending: " + (", ".join(t["symbol"] for t in mood.trending)
                                                if mood.trending else "(unavailable)"))
    parts.append("MARKET MOOD\n" + "\n".join(mood_lines))

    try:
        rows = con.execute("SELECT title, source, published, tags FROM news ORDER BY published DESC LIMIT 10").fetchall()
        parts.append("NEWS (newest first)\n" + ("\n".join(
            f"- [{age_str(r['published'])}, {r['source']}] {r['title'][:140]}" + (f" (mentions: {r['tags']})" if r["tags"] else "")
            for r in rows) if rows else "(unavailable)"))
    except Exception:
        parts.append("NEWS\n(unavailable)")

    if watch:
        wl = []
        for s in watch:
            try:
                wl.append(f"{s} last 24 hourly closes: " + ", ".join(f"{c:.4g}" for c in market.klines(s)))
            except Exception:
                wl.append(f"{s}: (unavailable)")
        parts.append("YOUR WATCHLIST (oldest to newest)\n" + "\n".join(wl))

    eq = total_value(bot["cash"], positions, prices)
    pos_lines = []
    for s, p in sorted(positions.items(), key=lambda kv: -kv[1]["qty"] * prices.get(kv[0], kv[1]["avg"])):
        now = prices.get(s, p["avg"])
        pl = (now / p["avg"] - 1) * 100 if p["avg"] else 0.0
        extra = "" if s in market.uni_set else " [no longer in the buy list: sell only]"
        pos_lines.append(f"- {s}: qty {p['qty']:.6g}, avg cost {fmt_price(p['avg'])}, now {fmt_price(now)}, "
                         f"value ${p['qty'] * now:.2f}, P/L {pl:+.1f}%{extra}")
    parts.append(f"YOUR PORTFOLIO\n- Cash: ${bot['cash']:.2f}\n"
                 + ("\n".join(pos_lines) if pos_lines else "- No coins held") +
                 f"\n- Total value: ${eq:.2f} ({(eq / cfg.start_cash - 1) * 100:+.2f}% vs start)"
                 f"\n- Distance to death: ${eq - cfg.death_line:.2f}")

    trades = con.execute("SELECT * FROM trades WHERE bot_id=? ORDER BY id DESC LIMIT 5", (bot["id"],)).fetchall()
    parts.append("YOUR LAST TRADES\n" + ("\n".join(
        f"- turn {t['turn']}: {t['action']} {t['symbol']} ${t['usd']:.2f} @ {fmt_price(t['price'])} (fee ${t['fee']:.3f}) because: {t['reason']}"
        for t in reversed(trades)) or "- none yet"))

    last = con.execute("SELECT turn, error FROM decisions WHERE bot_id=? ORDER BY id DESC LIMIT 1", (bot["id"],)).fetchone()
    if last and last["error"]:
        parts.append(f"PROBLEMS WITH YOUR LAST TURN (turn {last['turn']})\n{last['error']}")

    notes = con.execute("SELECT turn, note FROM decisions WHERE bot_id=? AND note<>'' ORDER BY id DESC LIMIT 8",
                        (bot["id"],)).fetchall()
    parts.append("YOUR NOTES TO YOURSELF\n" + ("\n".join(f"- turn {n['turn']}: {n['note']}" for n in reversed(notes))
                                               or "- none yet"))
    parts.append("Decide now. Keep your thinking brief (a few sentences), then answer with JSON only.")
    return "\n\n".join(parts)


def _post(host, path, body):
    req = urllib.request.Request(f"{host}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read())


def trim_thinking(th, limit=1500):
    th = (th or "").strip()
    return th if len(th) <= limit else th[:600] + "\n[...]\n" + th[-(limit - 610):]


def ask_ollama(cfg, system, user, schema=REPLY_SCHEMA, final_predict=600):
    """Returns (reply_text, thinking, prompt_tokens, eval_tokens, used_stage2).
    Stage 1: /api/chat with a JSON-schema `format` and think=true; R1's reasoning arrives in message.thinking.
    R1 distills often ramble past any sane token budget before answering, so if stage 1 ends without content,
    stage 2 feeds the reasoning so far back (raw prompt, think block closed) with the same schema `format`,
    which forces a valid JSON answer."""
    ptok = etok = 0
    thinking, content = "", ""
    if cfg.think_budget > 0:
        d = _post(cfg.host, "/api/chat", {
            "model": cfg.model, "stream": False, "think": True, "format": schema,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0.7, "num_predict": cfg.think_budget, "num_ctx": cfg.num_ctx}})
        msg = d["message"]
        content, thinking = msg.get("content") or "", msg.get("thinking") or ""
        ptok, etok = d.get("prompt_eval_count") or 0, d.get("eval_count") or 0
        if content.strip():
            return content, trim_thinking(thinking), ptok, etok, False
    closing = "\n\nI have thought enough. Time to decide and answer.\n" if thinking.strip() else ""
    prompt = (f"<｜begin▁of▁sentence｜>{system}<｜User｜>{user}"
              f"<｜Assistant｜><think>\n{thinking.strip()}{closing}</think>\n\n")
    d = _post(cfg.host, "/api/generate", {
        "model": cfg.model, "stream": False, "raw": True, "prompt": prompt, "format": schema,
        "options": {"temperature": 0.5, "num_predict": final_predict, "num_ctx": cfg.num_ctx}})
    return (d["response"], trim_thinking(thinking), ptok + (d.get("prompt_eval_count") or 0),
            etok + (d.get("eval_count") or 0), True)


def json_objects(text):
    """Yield every balanced {...} substring (string-aware), outermost first."""
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, j, in_str, esc = 0, i, False, False
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield text[i:j + 1]
                    break
            j += 1
        i = j + 1 if depth == 0 else i + 1


def parse_action(text):
    """Strip R1's <think> block and take the last valid JSON object that looks like a decision."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    for cand in reversed(list(json_objects(text))):
        try:
            d = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and ("orders" in d or "action" in d):
            if "orders" not in d:                                   # tolerate the old single-action shape
                d["orders"] = [] if str(d.get("action")).lower() == "hold" else [
                    {k: d[k] for k in ("action", "symbol", "usd", "sell_pct") if k in d}]
            return d
    raise ValueError("no JSON decision found in model reply")


def mock_decide(market, positions, cash):
    orders = []
    held = list(positions)
    for _ in range(random.choice([0, 1, 1, 2, 3])):
        r = random.random()
        if held and r < 0.3:
            orders.append({"action": "sell", "symbol": random.choice(held), "sell_pct": random.choice([25, 50, 100])})
        elif r < 0.4:                                               # excluded coin: must be rejected
            orders.append({"action": "buy", "symbol": random.choice(["KORUBUSDT", "MSTRBUSDT"]), "usd": 3})
        elif r < 0.55:                                              # oversized buy: must be shrunk
            orders.append({"action": "buy", "symbol": random.choice(market.universe[:8]), "usd": round(cash + 5, 2)})
        else:
            orders.append({"action": "buy", "symbol": random.choice(market.universe), "usd": round(random.uniform(2, 12), 2)})
    return {"orders": orders, "watch": random.sample(market.universe, 2),
            "reason": f"mock bot placed {len(orders)} random order(s)",
            "note": random.choice(["", "", "", "diversify a bit", "buy dips, stay small"])}


# ───────────────────────── the referee ─────────────────────────
def norm_symbol(s, prices):
    s = re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()
    if s not in prices and s + "USDT" in prices:
        s += "USDT"
    return s


def referee(orders, cash, positions, prices, uni_set, cfg, low_volume=None):
    """The model only *asks*. This code decides what is legal and updates the ledger.
    Sells run first, then buys. uni_set = coins that may be bought; low_volume = {symbol: 24h volume} of
    listed coins under the minimum. Returns (cash, positions, executed_trades, rejection_messages, notes)."""
    positions = {s: dict(p) for s, p in positions.items()}
    low_volume = low_volume or {}
    trades, rejected, notes = [], [], []
    if not isinstance(orders, list):
        return cash, positions, trades, ["orders must be a list"], notes
    if len(orders) > 3:
        rejected.append(f"only the first 3 of {len(orders)} orders are considered")
    orders = [o for o in orders[:3]]
    clean = []
    for o in orders:
        if not isinstance(o, dict):
            rejected.append(f"malformed order {str(o)[:40]!r}")
            continue
        act = str(o.get("action", "")).strip().lower()
        if act == "hold":
            continue
        if act not in ("buy", "sell"):
            rejected.append(f"unknown action {act!r}")
            continue
        clean.append((act, o))

    for want in ("sell", "buy"):
        for act, o in clean:
            if act != want:
                continue
            sym = norm_symbol(o.get("symbol"), prices)
            label = f"{act} {o.get('symbol')}"
            if sym not in prices:
                rejected.append(f"{label}: unknown symbol")
                continue
            price = prices[sym]
            try:
                if act == "buy":
                    usd = float(o.get("usd") or 0)
                    if sym in low_volume:
                        rejected.append(f"{label}: 24h volume {fmt_vol(low_volume[sym])} is below the "
                                        f"{fmt_vol(cfg.min_volume)} minimum, can't buy")
                        continue
                    if sym not in uni_set:
                        rejected.append(f"{label}: not in the current buy list (excluded or outside the top 40)")
                        continue
                    if usd < 1.0:
                        rejected.append(f"{label}: buy must be at least $1")
                        continue
                    if usd * (1 + cfg.fee_rate) > cash + 1e-9:
                        shrunk = cash / (1 + cfg.fee_rate)
                        if shrunk < 1.0:
                            rejected.append(f"{label}: only ${cash:.2f} cash left, under the $1 minimum")
                            continue
                        notes.append(f"buy {base_of(sym)} shrunk from ${usd:.2f} to ${shrunk:.2f} (cash limit)")
                        usd = shrunk
                    fee = usd * cfg.fee_rate
                    q = usd / price
                    p = positions.setdefault(sym, {"qty": 0.0, "avg": 0.0})
                    p["avg"] = (p["qty"] * p["avg"] + usd + fee) / (p["qty"] + q)
                    p["qty"] += q
                    cash = max(0.0, cash - usd - fee)
                    trades.append(dict(action="BUY", symbol=sym, qty=q, price=price, usd=usd, fee=fee,
                                       cash_after=cash, qty_after=p["qty"]))
                else:
                    p = positions.get(sym)
                    if not p or p["qty"] <= 0:
                        rejected.append(f"{label}: you don't own it")
                        continue
                    pct = 100.0 if o.get("sell_pct") is None else float(o["sell_pct"])
                    if pct <= 0:
                        rejected.append(f"{label}: sell_pct must be 1-100")
                        continue
                    pct = min(pct, 100.0)
                    q = p["qty"] if pct >= 100 else p["qty"] * pct / 100
                    usd = q * price
                    fee = usd * cfg.fee_rate
                    cash += usd - fee
                    p["qty"] = 0.0 if pct >= 100 else p["qty"] - q
                    trades.append(dict(action="SELL", symbol=sym, qty=q, price=price, usd=usd, fee=fee,
                                       cash_after=cash, qty_after=p["qty"]))
                    if p["qty"] * price < 0.005:
                        positions.pop(sym, None)
            except (TypeError, ValueError):
                rejected.append(f"{label}: invalid number")
    return cash, {s: p for s, p in positions.items() if p["qty"] > 0}, trades, rejected, notes


def charge_living_cost(cash, positions, prices, cost):
    if cost <= 0:
        return cash, positions
    if cash >= cost:
        return cash - cost, positions
    short = cost - cash
    for s in sorted(positions, key=lambda s: -positions[s]["qty"] * prices.get(s, positions[s]["avg"])):
        if short <= 0:
            break
        price = prices.get(s, positions[s]["avg"])
        take = min(short, positions[s]["qty"] * price)
        positions[s]["qty"] -= take / price
        short -= take
    return 0.0, {s: p for s, p in positions.items() if p["qty"] > 1e-12}


# ───────────────────────── memory: reflection, lessons, wisdom ─────────────────────────
def short_duration(seconds):
    s = int(max(0, seconds))
    if s >= 86400:
        return f"{s // 86400}d {(s % 86400) // 3600}h"
    if s >= 3600:
        return f"{s // 3600}h"
    return f"{max(1, s // 60)}m"


def similar(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def load_core(con):
    """The current wisdom: up to CORE_MAX lessons, each {"lesson": text, "life": the life that taught it}."""
    try:
        data = json.loads(get_setting(con, "core_lessons") or "[]")
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict) and isinstance(d.get("lesson"), str)]


def save_core(con, core):
    set_setting(con, "core_lessons", json.dumps(core))


def clean_lessons(items):
    out = []
    for x in items:
        if not isinstance(x, str):
            continue
        t = " ".join(x.split())[:220].strip()
        if len(t) >= 12 and not any(similar(t, o) > 0.9 for o in out):
            out.append(t)
    return out[:10]


def parse_lessons(text):
    """Strip any <think> block and take the last JSON object that has a "lessons" list."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    for cand in reversed(list(json_objects(text))):
        try:
            d = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and isinstance(d.get("lessons"), list):
            out = clean_lessons(d["lessons"])
            if out:
                return out
    raise ValueError("no lessons found in model reply")


def one_line_summary(S):
    """e.g. "Life 2: survived 14h, died at $0.98 after going all-in on PEPE" (built from the ledger, not by the model)."""
    s = S["summary"]
    if s["status"] == "DEAD":
        outcome = f"died at ${s['final_value']:.2f}"
    else:
        outcome = f"ended by a restart at ${s['final_value']:.2f} ({s['pl_pct']:+.1f}%)"
    clause = ""
    worst = s["closed_trades"]["worst"]
    if s["all_in"]:
        clause = f" after going all-in on {base_of(s['all_in']['symbol'])}"
    elif worst and worst["pnl_usd"] < 0:
        clause = f"; worst trade {base_of(worst['symbol'])} {worst['pnl_usd']:+.2f}"
    return f"Life {s['life_id']}: survived {short_duration(s['duration_seconds'])}, {outcome}{clause}"[:170]


REFLECTION_SYSTEM = (
    "You are {name}, an autonomous trader in a simulation. One of your lives just ended. Study the summary and write "
    "5 to 10 short, concrete lessons for your future self: one sentence each, under 200 characters. Be specific: "
    "name the rules of your world (fees, minimum volume, order limits, shrinking) and the exact habits or mistakes "
    "that cost or earned money, using the trades and coins in the summary. No generic advice, no repetition. "
    'Reply with ONLY a JSON object: {{"lessons": ["...", "..."]}}')


def reflection_prompt(S, cfg):
    """A compact life summary (aim for under ~1000 tokens) plus the system prompt for the reflection call."""
    s, st = S["summary"], S["summary"]["settings"]
    dd, tr, ct = s["drawdown"], s["trades"], s["closed_trades"]
    if s["status"] == "DEAD":
        outcome = (f"YOU DIED: total value fell to ${s['final_value']:.2f}, below the ${st['death_line']:g} death line")
    else:
        outcome = f"This life was ended by a restart at ${s['final_value']:.2f} (you did not die)"
    btc = s["btc_buy_and_hold"]
    lines = [
        f"RULES: you started with ${st['start_cash']:.2f}; every buy and sell costs a {st['fee_pct']:.2f}% fee; you die below "
        f"${st['death_line']:g}; max 3 orders per turn; coins under ${st['min_volume_usd'] / 1e6:g}M 24h volume can't be bought.",
        f"LIFE #{s['life_id']}: {outcome}.",
        f"Survived {s['turns']} turns ({fmt_duration(s['duration_seconds'])}). Start ${st['start_cash']:.2f}, "
        f"peak ${dd['peak_value']:.2f}, final ${s['final_value']:.2f} ({s['pl_pct']:+.1f}%). "
        f"Max drawdown {dd['max_drawdown_pct']:.1f}%."
        + (f" BTC buy and hold over the same time: {btc['pl_pct']:+.2f}%." if btc else ""),
        f"{tr['count']} trades ({tr['buys']} buys, {tr['sells']} sells), fees paid ${tr['fees_paid']:.2f}. "
        + (f"Win rate on closed trades {ct['win_rate_pct']:.0f}% ({ct['wins']} of {ct['count']})." if ct["count"] else "No closed trades."),
    ]
    if s["all_in"]:
        a = s["all_in"]
        lines.append(f"Biggest single buy: ${a['usd']:.2f} of {base_of(a['symbol'])} on turn {a['turn']}, almost all your cash.")

    def trade_line(c):
        return (f"- turn {c['turn']}, sold {base_of(c['symbol'])}: {c['pnl_usd']:+.2f} ({c['pnl_pct']:+.1f}%); "
                f"your reason then: \"{(c.get('reason') or '')[:110]}\"")

    closed = sorted(S["closed"], key=lambda c: c["pnl_usd"])
    best = [c for c in reversed(closed) if c["pnl_usd"] > 0][:5]
    worst = [c for c in closed if c["pnl_usd"] <= 0 and c not in best][:5]
    if best:
        lines += ["5 BEST CLOSED TRADES:"] + [trade_line(c) for c in best]
    if worst:
        lines += ["5 WORST CLOSED TRADES:"] + [trade_line(c) for c in worst]
    coins = S["summary"]["coin_results"]
    if coins:
        lines.append("HOW THE COINS YOU BOUGHT PERFORMED (realized + unrealized, $): " + ", ".join(
            f"{base_of(c['symbol'])} {c['net']:+.2f}" for c in (coins[:6] + [x for x in coins[-6:] if x not in coins[:6]])))
    last = S["decisions"][-4:]
    if last:
        lines += ["YOUR LAST DECISIONS:"] + [f"- turn {d['turn']}: {d['orders']} | {(d['reason'] or '')[:100]}" for d in last]
    notes = s["last_notes"][:5]
    if notes:
        lines += ["YOUR LAST NOTES TO SELF:"] + [f"- turn {n['turn']}: {n['note'][:140]}" for n in notes]
    return REFLECTION_SYSTEM.format(name=cfg.bot_name), "\n".join(lines)[:5200]


def mock_lessons(S):
    """Offline stand-in for the model: lessons built from the numbers, so the whole pipeline can be tested."""
    s = S["summary"]
    tr, dd = s["trades"], s["drawdown"]
    coins = s["coin_results"]
    out = [
        f"Fees took ${tr['fees_paid']:.2f} over {tr['count']} trades in life {s['life_id']}; trade less, every order costs {s['settings']['fee_pct']:.2f}%.",
        f"Max drawdown reached {dd['max_drawdown_pct']:.0f}% in life {s['life_id']}; keep positions smaller so one bad coin cannot sink the run.",
        "Never spend the last dollars: keep cash above $1 plus fees so a bad move cannot end the life.",
        f"Life {s['life_id']} {'died' if s['status'] == 'DEAD' else 'ended'} after {s['turns']} turns; spread money over several high-volume coins.",
    ]
    if coins:
        out.append(f"{base_of(coins[0]['symbol'])} was the worst coin in life {s['life_id']} ({coins[0]['net']:+.2f}); do not chase it again.")
    else:
        out.append(f"Life {s['life_id']} made no coin trades; waiting forever earns nothing, so take small, high-volume positions.")
    return out


def llm_lessons(cfg, system, user):
    last = None
    for _ in range(2):                                              # one retry, like the trading calls
        raw, _think, _p, _e, _s2 = ask_ollama(cfg, system, user, LESSONS_SCHEMA, 900)
        try:
            return parse_lessons(raw)
        except ValueError as e:
            last = e
    raise last


def dedupe_lessons(items, threshold=0.82):
    """Keep the first of any near-duplicate pair (older lessons win, so their source life is preserved)."""
    out = []
    for it in items:
        if not any(similar(it["lesson"], o["lesson"]) >= threshold for o in out):
            out.append(it)
    return out


def attribute_life(text, pool, default_life):
    best, life = 0.0, default_life
    for it in pool:
        r = similar(text, it["lesson"])
        if r > best:
            best, life = r, it["life"]
    return life if best >= 0.55 else default_life


def merge_core_lessons(con, cfg, new_lessons, life_id):
    """Old core lessons + this life's lessons -> a max-10 list without duplicates. If it still has more than 10 the
    model picks the most useful ones; its answer is mapped back to the life that taught each lesson."""
    combined = load_core(con) + [{"lesson": t, "life": life_id} for t in new_lessons]
    merged = dedupe_lessons(combined)
    if len(merged) > CORE_MAX:
        if cfg.mock:
            merged = merged[-CORE_MAX:]
        else:
            try:
                numbered = "\n".join(f"{i}. {m['lesson']}" for i, m in enumerate(merged, 1))
                system = ("You curate a trader's core wisdom: at most 10 short, concrete lessons. Merge the list you are given: "
                          "remove duplicates and near-duplicates, drop vague advice, keep the most useful and best-supported "
                          "lessons, and shorten wording if needed (each under 200 characters). "
                          'Reply with ONLY a JSON object: {"lessons": ["...", "..."]}')
                raw, _t, _p, _e, _s2 = ask_ollama(cfg, system, f"ALL LESSONS SO FAR (oldest first):\n{numbered}", MERGE_SCHEMA, 900)
                texts = parse_lessons(raw)
                merged = [{"lesson": t, "life": attribute_life(t, merged, life_id)} for t in texts][:CORE_MAX]
            except Exception as e:
                print(f"[reflection] merge by the model failed ({type(e).__name__}: {e}); keeping the newest {CORE_MAX}", flush=True)
                merged = merged[-CORE_MAX:]
    with DB_WRITE_LOCK:
        save_core(con, merged)
        con.execute("INSERT INTO core_history(life_id, created, core) VALUES(?,?,?)", (life_id, now_iso(), json.dumps(merged)))
        con.commit()
    return merged


def wisdom_section(con, bot):
    """The block added to the system prompt of a life that has memory ON: core lessons plus one line per past life.
    Only lives that also had memory ON count as 'your past lives'. Trimmed to stay under ~500 tokens."""
    if not bot.get("learning"):
        return ""
    core = load_core(con)
    past = [dict(r) for r in con.execute(
        "SELECT id, summary FROM bots WHERE id<? AND learning=1 AND summary IS NOT NULL AND summary<>'' "
        "ORDER BY id DESC LIMIT 5", (bot["id"],))][::-1]
    if not core and not past:
        return ""
    deaths = con.execute("SELECT COUNT(*) FROM bots WHERE id<? AND learning=1 AND alive=0", (bot["id"],)).fetchone()[0]

    def render(core_items, past_items):
        lines = [f"WISDOM FROM YOUR PAST LIVES (you have died {deaths} time{'s' if deaths != 1 else ''} before)"]
        if core_items:
            lines += ["Lessons you wrote for yourself:"] + [f"{i}. {c['lesson']}" for i, c in enumerate(core_items, 1)]
        if past_items:
            lines += ["Your past lives:"] + [f"- {p['summary']}" for p in past_items]
        lines.append("Use this wisdom, but keep thinking for yourself.")
        return "\n".join(lines)

    text = render(core, past)
    while len(text) / 4 > WISDOM_MAX_TOKENS and (len(past) > 1 or len(core) > 3):
        if len(past) > 1:
            past = past[1:]                                         # oldest past-life line goes first
        else:
            core = core[:-1]
        text = render(core, past)
    return text


def finish_life(con, cfg, market, bot_id):
    """Close a life's books: store its one-line summary and, if it had memory ON, reflect on it, save the lessons and
    fold them into the core wisdom. Runs for deaths and for manual restarts, before the next life is created."""
    row = con.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
    if not row or row["reflected"]:
        return
    S = life_stats(con, cfg, market, bot_id)
    with DB_WRITE_LOCK:
        con.execute("UPDATE bots SET summary=? WHERE id=?", (one_line_summary(S), bot_id))
        con.commit()
    if row["learning"] and row["ticks"] > 0:
        AGENT.update(phase="reflecting", reflecting_life=bot_id)
        t0 = time.time()
        try:
            system, user = reflection_prompt(S, cfg)
            lessons = mock_lessons(S) if cfg.mock else llm_lessons(cfg, system, user)
            now = now_iso()
            with DB_WRITE_LOCK:
                con.executemany("INSERT INTO lessons(life_id, lesson, created) VALUES(?,?,?)", [(bot_id, t, now) for t in lessons])
                con.commit()
            core = merge_core_lessons(con, cfg, lessons, bot_id)
            print(f"[reflection] life #{bot_id}: {len(lessons)} lessons saved, core wisdom now {len(core)} lessons "
                  f"({time.time() - t0:.0f}s)", flush=True)
        except Exception as e:
            print(f"[reflection] life #{bot_id} failed: {type(e).__name__}: {e}", flush=True)
        finally:
            AGENT.update(phase="trading", reflecting_life=None)
    with DB_WRITE_LOCK:
        con.execute("UPDATE bots SET reflected=1 WHERE id=?", (bot_id,))
        con.commit()


# ───────────────────────── simulation loop ─────────────────────────
def run_sim(cfg, market, mood):
    con = init_db(cfg.db)
    sync_settings(con, cfg)
    bot = current_bot(con) or new_life(con, cfg, market.btc)
    if not con.execute("SELECT 1 FROM equity WHERE bot_id=?", (bot["id"],)).fetchone():
        con.execute("INSERT INTO equity(bot_id, ts, btc_price, cash, positions_value, equity) VALUES(?,?,?,?,?,?)",
                    (bot["id"], now_iso(), market.btc, bot["cash"], 0.0, bot["cash"]))
        con.commit()
    cfg.start_cash, cfg.fee_rate = bot["start_cash"], bot["fee_rate"]      # the active life's own rules
    model_now = "mock (random)" if cfg.mock else cfg.model
    if bot["model"] != model_now:                                          # one model per life, never mixed
        if bot["ticks"] == 0:
            con.execute("UPDATE bots SET model=? WHERE id=?", (model_now, bot["id"]))
            con.commit()
            bot["model"] = model_now
        else:
            print(f"Model changed ({bot['model']} -> {model_now}): archiving life #{bot['id']} and starting a new one "
                  f"with the same settings.", flush=True)
            with RESTART_LOCK:
                RESTART_REQ["req"] = {"start_cash": cfg.start_cash, "fee_pct": round(cfg.fee_rate * 100, 4), "model_change": True}
    announced_death = False
    announced_life = None
    news_ts = 0.0

    while not STOP.is_set():
        t0 = time.time()
        restarted = apply_pending_restart(con, cfg, market, bot)
        if restarted:
            bot, announced_death = restarted, False
            AGENT.update(phase="trading", next_life_at=None)
        bot = dict(con.execute("SELECT * FROM bots WHERE id=?", (bot["id"],)).fetchone())
        system = system_prompt(cfg, bot.get("wisdom") or "")
        if announced_life != bot["id"]:
            announced_life = bot["id"]
            w = bot.get("wisdom") or ""
            print(f"[life #{bot['id']}] memory {'ON' if bot['learning'] else 'OFF'}; wisdom in the system prompt: "
                  f"{w.count(chr(10)) + 1 if w else 0} lines, about {len(w) // 4} tokens", flush=True)
        if not bot["alive"]:
            if not bot["reflected"]:                         # just died: close the books and learn from it
                print(f"\nXX {bot['name']} died after {bot['ticks']} turns at ${bot['final_value']:.2f}.", flush=True)
                with DB_WRITE_LOCK:
                    con.execute("UPDATE bots SET end_reason=COALESCE(end_reason, 'died') WHERE id=?", (bot["id"],))
                    con.commit()
                finish_life(con, cfg, market, bot["id"])
                bot = dict(con.execute("SELECT * FROM bots WHERE id=?", (bot["id"],)).fetchone())
            lives = con.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
            if get_toggle(con, "auto_restart") and lives < cfg.max_lives:
                if AGENT["next_life_at"] is None:
                    AGENT.update(phase="waiting", next_life_at=time.time() + cfg.restart_delay)
                    print(f"Auto-restart: a new life starts in {cfg.restart_delay:g}s (life {lives + 1} of at most {cfg.max_lives}).", flush=True)
                remaining = AGENT["next_life_at"] - time.time()
                if remaining > 0:
                    nap(min(5.0, remaining))
                    continue
                with DB_WRITE_LOCK:
                    con.execute("UPDATE bots SET ended=? WHERE id=?", (now_iso(), bot["id"]))
                    con.commit()
                    bot = new_life(con, cfg, market.btc)     # same settings, same model; memory per the current toggle
                AGENT.update(phase="trading", next_life_at=None)
                announced_death = False
                print(f">> Auto-restart: new life #{bot['id']} (start ${cfg.start_cash:.2f}, fee {cfg.fee_pct:g}%).", flush=True)
                continue
            AGENT.update(phase="dead", next_life_at=None)
            if not announced_death:
                why = "the maximum number of lives is reached" if (lives >= cfg.max_lives and get_toggle(con, "auto_restart")) else "auto-restart is off"
                print(f"The dashboard stays up; {why}. Restart from the dashboard settings panel, or rerun with --reset.", flush=True)
                announced_death = True
            nap(5)
            continue
        AGENT.update(phase="trading", next_life_at=None)

        market.tick()
        if time.time() - news_ts > 900:
            news_ts = time.time()
            try:
                fetch_news(con, market.universe)
            except Exception as e:
                print(f"[news] failed: {e}")
        try:
            mood.update()
        except Exception as e:
            print(f"[mood] failed: {e}")

        prices = market.prices
        positions = load_positions(con, bot["id"])
        turn = bot["ticks"] + 1
        last = con.execute("SELECT watch FROM decisions WHERE bot_id=? ORDER BY id DESC LIMIT 1", (bot["id"],)).fetchone()
        try:
            watch = [s for s in json.loads(last["watch"] or "[]") if s in prices][:5] if last else []
        except Exception:
            watch = []

        error, ptok, etok, thinking, stage2 = None, None, None, "", 0
        try:
            if cfg.mock:
                d = mock_decide(market, positions, bot["cash"])
            else:
                prompt = turn_prompt(con, cfg, market, mood, bot, turn, positions, watch)
                ptok = etok = 0
                for attempt in (1, 2):                              # one retry if the reply has no JSON
                    raw, thinking, p_, e_, s2_ = ask_ollama(cfg, system, prompt)
                    ptok, etok, stage2 = ptok + p_, etok + e_, stage2 + int(s2_)
                    try:
                        d = parse_action(raw)
                        break
                    except ValueError:
                        if attempt == 2:
                            raise ValueError(f"no JSON in reply: {raw.strip()[:150]!r}")
        except Exception as e:
            d = {"orders": [], "reason": "(no valid decision this turn)"}
            error = f"{type(e).__name__}: {e}"[:300]

        if RESTART_REQ["req"]:          # a restart arrived mid-turn: drop this turn's decision, the life is being replaced
            continue
        orders = d.get("orders") if isinstance(d.get("orders"), list) else []
        low_volume = {s: v[1] for s, v in market.stats.items() if v[1] < cfg.min_volume}
        buyable = market.uni_set - set(low_volume)
        cash, positions, trades, rejected, notes = referee(orders, bot["cash"], positions, prices, buyable, cfg, low_volume)
        for msg in (("NOTE: " + "; ".join(notes)) if notes else None, ("REJECTED: " + "; ".join(rejected)) if rejected else None):
            if msg:
                error = f"{error}; {msg}" if error else msg
        reason = str(d.get("reason", ""))[:300]
        note = str(d.get("note", "") or "")[:300]
        wl = d.get("watch")
        new_watch = [norm_symbol(s, prices) for s in wl[:5]] if isinstance(wl, list) else []
        new_watch = [s for s in new_watch if s in prices]
        ts = now_iso()

        with DB_WRITE_LOCK:                                  # a shutdown waits for these writes, never cuts them
            for t in trades:
                con.execute(
                    "INSERT INTO trades(bot_id, ts, turn, action, symbol, qty, price, usd, fee, cash_after, qty_after, reason)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (bot["id"], ts, turn, t["action"], t["symbol"], t["qty"], t["price"], t["usd"], t["fee"],
                     t["cash_after"], t["qty_after"], reason))

            cash, positions = charge_living_cost(cash, positions, prices, cfg.living_cost)
            pos_val = sum(p["qty"] * prices.get(s, p["avg"]) for s, p in positions.items())
            eq = cash + pos_val
            alive = 1 if eq >= cfg.death_line else 0

            summary = ", ".join(
                f"{str(o.get('action')).upper()} {base_of(norm_symbol(o.get('symbol'), prices))}"
                + (f" ${o['usd']}" if o.get("usd") else "") + (f" {o['sell_pct']}%" if o.get("sell_pct") else "")
                for o in orders[:3] if isinstance(o, dict)) or "none"
            con.execute("INSERT INTO decisions(bot_id, ts, turn, action, orders, watch, reason, note, error,"
                        " prompt_tokens, eval_tokens, thinking, duration, stage2) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (bot["id"], ts, turn, "trade" if trades else "hold", summary, json.dumps(new_watch),
                         reason, note, error, ptok, etok, thinking, time.time() - t0, stage2))
            con.execute("INSERT INTO equity(bot_id, ts, btc_price, cash, positions_value, equity) VALUES(?,?,?,?,?,?)",
                        (bot["id"], ts, market.btc, cash, pos_val, eq))
            save_positions(con, bot["id"], positions, prices)
            con.execute("UPDATE bots SET cash=?, alive=?, ticks=?, died=?, final_value=? WHERE id=?",
                        (cash, alive, turn, None if alive else ts, eq, bot["id"]))
            con.commit()

        tr = " ".join(f"{t['action']} {base_of(t['symbol'])} ${t['usd']:.2f}" for t in trades) or "HOLD"
        tok = (f" | tok {ptok}+{etok}" if ptok else "") + (" | stage2" if stage2 else "")
        print(f"turn {turn:>4} | {time.time() - t0:5.1f}s | value ${eq:8.2f} | cash ${cash:7.2f} | {len(positions)} coins | {tr}{tok}"
              f" | {reason[:60]}" + (f"  [!] {error[:80]}" if error else ""), flush=True)

        nap(cfg.interval - (time.time() - t0))


# ───────────────────────── dashboard server ─────────────────────────
def open_readonly(cfg):
    con = connect(cfg.db)
    con.execute("PRAGMA query_only=ON")             # exports can never write, so they can't disturb the agent
    return con


def parse_ts(iso):
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def fmt_when(iso):
    d = parse_ts(iso)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if d else "-"


def fmt_duration(seconds):
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    return (f"{d}d " if d else "") + f"{h}h {s // 60}m"


def replay_closed_trades(trades):
    """Rebuild average cost per coin from the trade log and score every SELL: net profit after the sell fee
    versus the average cost of the coins sold."""
    pos, closed = {}, []
    for t in trades:
        p = pos.setdefault(t["symbol"], [0.0, 0.0])                 # [qty, avg cost incl. buy fees]
        if t["action"] == "BUY":
            p[1] = (p[0] * p[1] + t["usd"] + t["fee"]) / (p[0] + t["qty"]) if p[0] + t["qty"] > 0 else 0.0
            p[0] += t["qty"]
        elif p[1] > 0:
            cost = t["qty"] * p[1]
            pnl = t["usd"] - t["fee"] - cost
            closed.append({"symbol": t["symbol"], "turn": t["turn"], "ts": t["ts"], "price": t["price"],
                           "avg_cost": p[1], "pnl_usd": pnl, "pnl_pct": pnl / cost * 100 if cost else 0.0,
                           "reason": t.get("reason") or ""})
            p[0] = max(0.0, p[0] - t["qty"])
    return closed


def life_stats(con, cfg, market, bot_id):
    """Everything the report and summary.json show for one life. Returns None for an unknown life."""
    row = con.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
    if not row:
        return None
    bot = dict(row)
    cur = current_bot(con)
    live = bool(cur) and cur["id"] == bot["id"] and bool(bot["alive"]) and not bot["ended"]
    equity = [dict(r) for r in con.execute(
        "SELECT ts, btc_price, cash, positions_value, equity FROM equity WHERE bot_id=? ORDER BY id", (bot_id,))]
    trades = [dict(r) for r in con.execute("SELECT * FROM trades WHERE bot_id=? ORDER BY id", (bot_id,))]
    decisions = [dict(r) for r in con.execute("SELECT * FROM decisions WHERE bot_id=? ORDER BY id", (bot_id,))]

    holdings = []
    for r in con.execute("SELECT symbol, qty, avg_cost, last_price FROM positions WHERE bot_id=? AND qty>0", (bot_id,)):
        price = (market.prices.get(r["symbol"]) if live else None) or r["last_price"]
        at_cost = price is None                                      # old ledgers didn't store the closing price
        price = price if price is not None else r["avg_cost"]
        holdings.append({"symbol": r["symbol"], "qty": r["qty"], "avg_cost": r["avg_cost"], "price": price,
                         "value": r["qty"] * price, "pl_pct": (price / r["avg_cost"] - 1) * 100 if r["avg_cost"] else 0.0,
                         "valued_at_cost": at_cost})
    holdings.sort(key=lambda h: -h["value"])

    start_cash = bot["start_cash"]
    if live:
        final = bot["cash"] + sum(h["value"] for h in holdings)
    elif bot["final_value"] is not None:
        final = bot["final_value"]
    else:
        final = equity[-1]["equity"] if equity else start_cash
    end_iso = bot["ended"] or bot["died"]
    t_start, t_end = parse_ts(bot["started"]), parse_ts(end_iso) or datetime.now(timezone.utc)
    duration = (t_end - t_start).total_seconds() if t_start else 0.0
    status = "DEAD" if not bot["alive"] else ("ALIVE" if live else "ENDED")

    btc0 = next((e["btc_price"] for e in equity if e["btc_price"]), None)
    btc1 = (market.btc if live else None) or next((e["btc_price"] for e in reversed(equity) if e["btc_price"]), None)
    btc = None
    if btc0 and btc1:
        btc = {"start_price": btc0, "end_price": btc1, "value": start_cash * btc1 / btc0, "pl_pct": (btc1 / btc0 - 1) * 100}
    pl_pct = (final / start_cash - 1) * 100 if start_cash else 0.0

    peak, max_dd, max_dd_usd, peak_value = 0.0, 0.0, 0.0, start_cash
    for e in equity:
        v = e["equity"]
        peak = max(peak, v)
        peak_value = max(peak_value, v)
        if peak > 0 and (peak - v) / peak > max_dd:
            max_dd, max_dd_usd = (peak - v) / peak, peak - v

    closed = replay_closed_trades(trades)
    wins = [c for c in closed if c["pnl_usd"] > 0]
    by_coin = collections.defaultdict(lambda: [0, 0.0])
    for t in trades:
        by_coin[t["symbol"]][0] += 1
        by_coin[t["symbol"]][1] += t["usd"]
    top_coins = [{"symbol": s, "trades": n, "volume_usd": v}
                 for s, (n, v) in sorted(by_coin.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))[:5]]
    realized = collections.defaultdict(float)
    for c in closed:
        realized[c["symbol"]] += c["pnl_usd"]
    unrealized = {h["symbol"]: h["value"] - h["qty"] * h["avg_cost"] for h in holdings}
    coin_results = sorted(
        ({"symbol": sym, "realized": realized.get(sym, 0.0), "unrealized": unrealized.get(sym, 0.0),
          "net": realized.get(sym, 0.0) + unrealized.get(sym, 0.0)} for sym in set(by_coin) | set(unrealized)),
        key=lambda r: r["net"])
    all_in = None                                   # a single buy that used (almost) all the cash the agent had
    for t in trades:
        if t["action"] == "BUY":
            before = t["cash_after"] + t["usd"] + t["fee"]
            if t["usd"] >= 0.8 * before and t["usd"] >= 0.3 * start_cash and (all_in is None or t["usd"] > all_in["usd"]):
                all_in = {"symbol": t["symbol"], "usd": t["usd"], "turn": t["turn"]}

    by_ts = {d["ts"]: d for d in decisions}
    moments = []
    for prev, e in zip(equity, equity[1:]):
        d = by_ts.get(e["ts"], {})
        delta = e["equity"] - prev["equity"]
        moments.append({"turn": d.get("turn"), "ts": e["ts"], "change_usd": delta,
                        "change_pct": delta / prev["equity"] * 100 if prev["equity"] else 0.0,
                        "orders": d.get("orders") or "", "reason": d.get("reason") or ""})
    moments = sorted(moments, key=lambda m: -abs(m["change_usd"]))[:5]
    notes = [{"turn": d["turn"], "ts": d["ts"], "note": d["note"]} for d in reversed(decisions) if d["note"]][:10]

    summary = {
        "generated": now_iso(), "life_id": bot["id"], "bot_name": bot["name"], "model": bot["model"], "status": status,
        "started": bot["started"], "ended": bot["ended"], "died": bot["died"],
        "turns": bot["ticks"], "duration_seconds": round(duration),
        "settings": {"start_cash": start_cash, "fee_pct": (bot["fee_rate"] or 0) * 100, "death_line": cfg.death_line,
                     "interval_seconds": cfg.interval, "min_volume_usd": cfg.min_volume},
        "final_value": final, "pl_usd": final - start_cash, "pl_pct": pl_pct,
        "btc_buy_and_hold": btc, "vs_btc_pct_points": pl_pct - btc["pl_pct"] if btc else None,
        "trades": {"count": len(trades), "buys": sum(t["action"] == "BUY" for t in trades),
                   "sells": sum(t["action"] == "SELL" for t in trades),
                   "fees_paid": sum(t["fee"] for t in trades), "volume_usd": sum(t["usd"] for t in trades)},
        "closed_trades": {"count": len(closed), "wins": len(wins), "losses": len(closed) - len(wins),
                          "win_rate_pct": len(wins) / len(closed) * 100 if closed else None,
                          "best": max(closed, key=lambda c: c["pnl_usd"]) if closed else None,
                          "worst": min(closed, key=lambda c: c["pnl_usd"]) if closed else None},
        "drawdown": {"peak_value": peak_value, "max_drawdown_pct": max_dd * 100, "max_drawdown_usd": max_dd_usd},
        "top_coins": top_coins, "holdings": holdings, "key_moments": moments, "last_notes": notes,
        "coin_results": coin_results, "all_in": all_in,
        "learning": bool(bot.get("learning")), "end_reason": bot.get("end_reason"), "life_summary": bot.get("summary"),
        "wisdom_at_start": bot.get("wisdom") or "",
        "lessons": [dict(r) for r in con.execute("SELECT lesson, created FROM lessons WHERE life_id=? ORDER BY id", (bot_id,))],
    }
    return {"summary": summary, "bot": bot, "equity": equity, "trades": trades, "decisions": decisions, "live": live,
            "closed": closed,
            "end_iso": end_iso or now_iso()}


def svg_chart(equity, start_cash):
    """Total value vs the start line vs BTC buy and hold, as a self-contained SVG (prints without JS or a CDN).
    Line styles differ (solid, dashed, dotted) so it still reads in black and white."""
    W, H, L, R, T, B = 720, 250, 62, 14, 26, 30
    if len(equity) > 400:
        step = math.ceil(len(equity) / 400)
        equity = equity[::step] + ([equity[-1]] if (len(equity) - 1) % step else [])
    times = [(parse_ts(e["ts"]) or datetime.now(timezone.utc)).timestamp() for e in equity] or [0.0]
    btc0 = next((e["btc_price"] for e in equity if e["btc_price"]), None)
    eq = [e["equity"] for e in equity] or [start_cash]
    bh = [start_cash * e["btc_price"] / btc0 if btc0 and e["btc_price"] else None for e in equity]
    vals = eq + [v for v in bh if v is not None] + [start_cash]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or max(1.0, hi * 0.02)
    lo, hi = lo - pad, hi + pad
    t0, t1 = min(times), max(times)
    span = (t1 - t0) or 1.0
    X = lambda t: L + (t - t0) / span * (W - L - R) if t1 > t0 else L + (W - L - R) / 2
    Y = lambda v: T + (hi - v) / (hi - lo) * (H - T - B)

    def line(series, style):
        pts = " ".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in zip(times, series) if v is not None)
        return f'<polyline fill="none" points="{pts}" {style}/>' if pts.count(",") > 1 else (
            f'<circle cx="{pts.split(",")[0]}" cy="{pts.split(",")[1]}" r="2.5" fill="#222"/>' if pts else "")

    out = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" '
           f'aria-label="Total value over time compared with the start line and BTC buy and hold" font-family="Arial, sans-serif" font-size="10">']
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="#e3e3e3"/>'
                   f'<text x="{L - 6}" y="{Y(v) + 3:.1f}" text-anchor="end" fill="#555">${v:,.2f}</text>')
    out.append(f'<text x="{L}" y="{H - 10}" fill="#555">{html.escape(fmt_when(equity[0]["ts"]) if equity else "")}</text>'
               f'<text x="{W - R}" y="{H - 10}" text-anchor="end" fill="#555">{html.escape(fmt_when(equity[-1]["ts"]) if equity else "")}</text>')
    out.append(line([start_cash] * len(times), 'stroke="#777" stroke-width="1.2" stroke-dasharray="2 4"'))
    out.append(line(bh, 'stroke="#c77700" stroke-width="1.6" stroke-dasharray="7 4"'))
    out.append(line(eq, 'stroke="#2f2fb8" stroke-width="2.2"'))
    lx = L
    for label, dash, color, w in (("Total value", "", "#2f2fb8", 2.2), ("BTC buy &amp; hold", "7 4", "#c77700", 1.6),
                                  ("Start amount", "2 4", "#777", 1.2)):
        out.append(f'<line x1="{lx}" x2="{lx + 22}" y1="12" y2="12" stroke="{color}" stroke-width="{w}"'
                   + (f' stroke-dasharray="{dash}"' if dash else "") + f'/><text x="{lx + 27}" y="15" fill="#222">{label}</text>')
        lx += 130
    out.append("</svg>")
    return "".join(out)


REPORT_CSS = """
@page { size: A4; margin: 14mm 12mm; }
* { box-sizing: border-box; }
body { margin: 0; background: #fff; color: #111; font: 10pt/1.4 -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
.page { max-width: 190mm; margin: 0 auto; padding: 10mm 0 14mm; }
h1 { font-size: 20pt; margin: 0 0 1mm; }
h2 { font-size: 12pt; margin: 7mm 0 2.5mm; padding-bottom: 1mm; border-bottom: 1px solid #888; page-break-after: avoid; break-after: avoid; }
.sub { color: #555; font-size: 9pt; }
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 3mm; }
.box { border: 1px solid #bbb; border-radius: 2mm; padding: 2.2mm 3mm; page-break-inside: avoid; break-inside: avoid; }
.box .k { font-size: 8pt; color: #555; }
.box .v { font-size: 13pt; font-weight: 700; }
.box .s { font-size: 8pt; color: #555; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 8pt; }
th, td { border-bottom: 1px solid #ddd; padding: 1.2mm 1.5mm; text-align: left; vertical-align: top; overflow-wrap: anywhere; word-break: break-word; }
th { background: #f0f0f0; font-weight: 600; }
thead { display: table-header-group; }
tr { page-break-inside: avoid; break-inside: avoid; }
.r { text-align: right; }
.pos { color: #0a6b35; } .neg { color: #b42318; }
svg { width: 100%; height: auto; display: block; page-break-inside: avoid; break-inside: avoid; }
.bar { display: flex; gap: 3mm; align-items: center; justify-content: space-between; margin-bottom: 5mm; }
.btn { font: inherit; font-weight: 600; padding: 2mm 4mm; border: 1px solid #444; background: #fff; border-radius: 2mm; cursor: pointer; }
.break { page-break-before: always; break-before: page; }
.note { margin: 1mm 0; }
.wisdom { margin: 1mm 0 0; padding: 2mm 3mm; border: 1px solid #ccc; border-radius: 2mm; background: #fafafa; white-space: pre-wrap;
          font: 8pt/1.4 -apple-system, "Segoe UI", Roboto, Arial, sans-serif; page-break-inside: avoid; break-inside: avoid; }
.lessons { margin: 1mm 0 0; padding-left: 5mm; } .lessons li { margin: 0.8mm 0; }
.disclaimer { margin-top: 8mm; padding-top: 3mm; border-top: 1px solid #888; font-size: 8.5pt; color: #444; page-break-inside: avoid; break-inside: avoid; }
@media screen and (max-width: 640px) { .grid { grid-template-columns: repeat(2, 1fr); } }
@media print { .no-print { display: none !important; } body { -webkit-print-color-adjust: exact; print-color-adjust: exact; } .page { padding: 0; } }
"""


def render_report(S):
    s, e = S["summary"], html.escape
    money = lambda x: ("-" if x < 0 else "") + f"${abs(x):,.2f}"
    signed = lambda x: ("+" if x >= 0 else "-") + f"${abs(x):,.2f}"
    cls = lambda x: "pos" if x >= 0 else "neg"
    st = s["settings"]
    when_end = s["died"] if s["status"] == "DEAD" else s["ended"]
    status_line = {"ALIVE": "ALIVE (still running)", "DEAD": f"DEAD at {fmt_when(s['died'])}",
                   "ENDED": f"ENDED (restarted) at {fmt_when(s['ended'])}"}[s["status"]]
    btc, tr, ct, dd = s["btc_buy_and_hold"], s["trades"], s["closed_trades"], s["drawdown"]

    def box(k, v, sub=""):
        return f'<div class="box"><div class="k">{e(k)}</div><div class="v">{v}</div><div class="s">{sub}</div></div>'

    def trade_line(c):
        if not c:
            return "n/a"
        return (f'{e(base_of(c["symbol"]))}, turn {c["turn"]}: <span class="{cls(c["pnl_usd"])}">{signed(c["pnl_usd"])} '
                f'({c["pnl_pct"]:+.1f}%)</span>')

    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
           f'<title>{e(s["bot_name"])} - Life #{s["life_id"]} report</title><style>{REPORT_CSS}</style></head><body><div class="page">',
           '<div class="bar no-print"><span class="sub">Use your browser\'s print dialog: paper size A4, margins default.</span>'
           '<button class="btn" type="button" onclick="window.print()">Print / Save as PDF</button></div>',
           f'<h1>{e(s["bot_name"])} — Life #{s["life_id"]} report</h1>',
           f'<div class="sub">Model: {e(str(s["model"]))} &middot; generated {e(fmt_when(s["generated"]))}</div>']

    out.append("<h2>Settings</h2><div class=\"grid\">"
               + box("Starting cash", money(st["start_cash"])) + box("Fee (every buy and sell)", f'{st["fee_pct"]:.2f}%')
               + box("Death line", money(st["death_line"]), "dies if total value falls below")
               + box("Turn interval", f'{st["interval_seconds"]:g} s', "arena setting at export time")
               + box("Minimum 24h volume to buy", money(st["min_volume_usd"]).replace(".00", ""), "arena setting at export time")
               + "</div>")

    out.append("<h2>Outcome</h2><div class=\"grid\">"
               + box("Status", e(status_line))
               + box("Turns survived", str(s["turns"]), f'over {e(fmt_duration(s["duration_seconds"]))}')
               + box("Final value", money(s["final_value"]))
               + box("Profit / loss", f'<span class="{cls(s["pl_usd"])}">{signed(s["pl_usd"])}</span>',
                     f'<span class="{cls(s["pl_pct"])}">{s["pl_pct"]:+.2f}%</span> vs start')
               + (box("BTC buy & hold", f'<span class="{cls(btc["pl_pct"])}">{btc["pl_pct"]:+.2f}%</span>',
                      f'{money(btc["value"])} from the same start cash')
                  if btc else box("BTC buy & hold", "n/a", "no BTC price recorded"))
               + (box("Versus BTC", f'<span class="{cls(s["vs_btc_pct_points"])}">{s["vs_btc_pct_points"]:+.2f} pts</span>',
                      "agent minus BTC, same period") if btc else box("Versus BTC", "n/a"))
               + "</div>")

    wr = f'{ct["win_rate_pct"]:.0f}% ({ct["wins"]} of {ct["count"]})' if ct["count"] else "n/a (no closed trades)"
    coins = ", ".join(f'{e(base_of(c["symbol"]))} ({c["trades"]})' for c in s["top_coins"]) or "none"
    out.append("<h2>Statistics</h2><div class=\"grid\">"
               + box("Trades", str(tr["count"]), f'{tr["buys"]} buys, {tr["sells"]} sells')
               + box("Fees paid", money(tr["fees_paid"]), f'on {money(tr["volume_usd"])} traded')
               + box("Win rate (closed trades)", wr, "sell proceeds after fee vs average cost")
               + box("Best trade", trade_line(ct["best"]))
               + box("Worst trade", trade_line(ct["worst"]))
               + box("Peak value", money(dd["peak_value"]))
               + box("Max drawdown", f'{dd["max_drawdown_pct"]:.1f}%', f'{money(dd["max_drawdown_usd"])} from a peak')
               + box("Most-traded coins", coins)
               + "</div>")

    out.append("<h2>Total value over time</h2>" + svg_chart(S["equity"], st["start_cash"]))

    out.append("<h2>Final holdings</h2>")
    if s["holdings"]:
        out.append('<table><colgroup><col style="width:16%"><col style="width:20%"><col style="width:16%"><col style="width:16%">'
                   '<col style="width:16%"><col style="width:16%"></colgroup><thead><tr><th>Coin</th><th class="r">Qty</th>'
                   '<th class="r">Avg cost</th><th class="r">Price</th><th class="r">Value</th><th class="r">P/L</th></tr></thead><tbody>')
        for h in s["holdings"]:
            out.append(f'<tr><td>{e(base_of(h["symbol"]))}</td><td class="r">{h["qty"]:.6g}</td><td class="r">{fmt_price(h["avg_cost"])}</td>'
                       f'<td class="r">{fmt_price(h["price"])}{"*" if h["valued_at_cost"] else ""}</td><td class="r">{money(h["value"])}</td>'
                       f'<td class="r {cls(h["pl_pct"])}">{h["pl_pct"]:+.1f}%</td></tr>')
        out.append("</tbody></table>")
        if any(h["valued_at_cost"] for h in s["holdings"]):
            out.append('<div class="sub">* closing price was not recorded for this life, so the holding is shown at cost.</div>')
        out.append(f'<div class="sub">Cash: {money(S["bot"]["cash"])}</div>')
    else:
        out.append(f'<div class="sub">No coins held. Cash: {money(S["bot"]["cash"])}</div>')

    out.append("<h2>Key moments</h2>")
    if s["key_moments"]:
        out.append('<table><colgroup><col style="width:7%"><col style="width:17%"><col style="width:14%"><col style="width:62%"></colgroup>'
                   '<thead><tr><th>Turn</th><th>Time</th><th class="r">Change</th><th>Orders and the agent\'s reason</th></tr></thead><tbody>')
        for m in s["key_moments"]:
            out.append(f'<tr><td>{m["turn"] if m["turn"] is not None else "?"}</td><td>{e(fmt_when(m["ts"]))}</td>'
                       f'<td class="r {cls(m["change_usd"])}">{signed(m["change_usd"])}<br>{m["change_pct"]:+.2f}%</td>'
                       f'<td>{("<b>" + e(m["orders"]) + "</b><br>") if m["orders"] and m["orders"] != "none" else ""}{e(m["reason"])}</td></tr>')
        out.append("</tbody></table><div class=\"sub\">The 5 biggest per-turn swings in total value (market moves count, not only trades).</div>")
    else:
        out.append('<div class="sub">No turns recorded yet.</div>')

    out.append("<h2>Last notes to self</h2>")
    out.append("".join(f'<div class="note"><b>Turn {n["turn"]}:</b> {e(n["note"])}</div>' for n in s["last_notes"])
               or '<div class="sub">The agent wrote no notes.</div>')

    out.append("<h2>Memory and lessons</h2>")
    out.append(f'<div class="sub">Cross-life learning was <b>{"ON" if s["learning"] else "OFF"}</b> for this life.</div>')
    if s["wisdom_at_start"]:
        out.append('<div class="sub" style="margin-top:2mm">Wisdom this life started with (added to its system prompt):</div>'
                   f'<pre class="wisdom">{e(s["wisdom_at_start"])}</pre>')
    elif s["learning"]:
        out.append('<div class="sub">No lessons from earlier lives existed yet, so it started without wisdom.</div>')
    if s["lessons"]:
        out.append('<div class="sub" style="margin-top:2mm">Lessons the agent wrote when this life ended:</div><ol class="lessons">'
                   + "".join(f'<li>{e(x["lesson"])}</li>' for x in s["lessons"]) + "</ol>")
    elif s["status"] == "ALIVE":
        out.append('<div class="sub" style="margin-top:2mm">The agent reflects when this life ends.</div>')
    else:
        out.append('<div class="sub" style="margin-top:2mm">This life taught no lessons'
                   + ("" if s["learning"] else " (memory was off, so no reflection ran)") + ".</div>")
    out.append('<div class="break"></div><h2>All trades</h2>')
    if S["trades"]:
        out.append('<table><colgroup><col style="width:15%"><col style="width:5%"><col style="width:8%"><col style="width:7%">'
                   '<col style="width:11%"><col style="width:10%"><col style="width:9%"><col style="width:7%"><col style="width:28%"></colgroup>'
                   '<thead><tr><th>Time</th><th>Turn</th><th>Coin</th><th>Action</th><th class="r">Qty</th><th class="r">Price</th>'
                   '<th class="r">Value</th><th class="r">Fee</th><th>Reason</th></tr></thead><tbody>')
        for t in S["trades"]:
            d = parse_ts(t["ts"])
            out.append(f'<tr><td>{e(d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") if d else "-")}</td><td>{t["turn"]}</td>'
                       f'<td>{e(base_of(t["symbol"]))}</td><td><b>{e(t["action"])}</b></td><td class="r">{t["qty"]:.6g}</td>'
                       f'<td class="r">{fmt_price(t["price"])}</td><td class="r">{money(t["usd"])}</td><td class="r">{money(t["fee"])}</td>'
                       f'<td>{e(t["reason"] or "")}</td></tr>')
        out.append("</tbody></table>")
    else:
        out.append('<div class="sub">No trades in this life.</div>')

    out.append('<div class="disclaimer"><b>Disclaimer.</b> This is a simulation. No real money was used and no real orders were placed. '
               'Nothing here is financial advice, and the results say nothing about how a real strategy would perform.</div>')
    out.append("</div></body></html>")
    return "".join(out).encode("utf-8")


def safe_cell(v):
    """Stop spreadsheet formula injection: model- and feed-written text starting with = + - @ gets a leading quote."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


DECIMAL_COMMA_LANGS = {"de", "fr", "es", "it", "pt", "nl", "ru", "pl", "sv", "da", "fi", "nb", "no", "tr", "cs", "sk",
                       "hu", "ro", "bg", "hr", "sl", "el", "uk", "id", "vi"}


def system_decimal_separator():
    """The decimal separator of the user's regional settings (Excel derives its CSV delimiter from it)."""
    try:
        if sys.platform == "win32":
            import ctypes
            buf = ctypes.create_unicode_buffer(8)
            if ctypes.windll.kernel32.GetLocaleInfoEx(None, 0x0E, buf, 8):      # LOCALE_SDECIMAL, user default locale
                return buf.value
        else:
            lang = os.environ.get("LC_NUMERIC") or os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
            return "," if lang[:2].lower() in DECIMAL_COMMA_LANGS else "."
    except Exception:
        pass
    return "."


def csv_bytes(header, rows, delim=","):
    """UTF-8 with a BOM. With delim=';' floats use a decimal comma, which is what Excel expects when the
    system's decimal separator is a comma (it then also splits columns on ';' instead of ',')."""
    def cell(v):
        v = safe_cell(v)
        return str(v).replace(".", ",") if delim == ";" and isinstance(v, float) else v
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delim)
    w.writerow(header)
    for r in rows:
        w.writerow([cell(v) for v in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")           # BOM so Excel reads umlauts and emojis as UTF-8


def build_export_zip(con, S, delim=","):
    s, bot_id = S["summary"], S["bot"]["id"]
    csv_b = lambda header, rows: csv_bytes(header, rows, delim)
    core_texts = {c["lesson"] for c in load_core(con)}
    files = {
        "trades.csv": csv_b(
            ["id", "ts", "turn", "action", "symbol", "qty", "price", "usd", "fee", "cash_after", "qty_after", "reason"],
            [[t["id"], t["ts"], t["turn"], t["action"], t["symbol"], t["qty"], t["price"], t["usd"], t["fee"],
              t["cash_after"], t["qty_after"], t["reason"]] for t in S["trades"]]),
        "equity.csv": csv_b(
            ["ts", "btc_price", "cash", "positions_value", "equity"],
            [[q["ts"], q["btc_price"], q["cash"], q["positions_value"], q["equity"]] for q in S["equity"]]),
        "decisions.csv": csv_b(
            ["ts", "turn", "action", "orders", "watch", "reason", "note", "error", "thinking",
             "prompt_tokens", "eval_tokens", "duration_seconds"],
            [[d["ts"], d["turn"], d["action"], d["orders"], d["watch"], d["reason"], d["note"], d["error"], d["thinking"],
              d["prompt_tokens"], d["eval_tokens"], d["duration"]] for d in S["decisions"]]),
        "positions.csv": csv_b(
            ["symbol", "qty", "avg_cost", "price", "value", "pl_pct", "valued_at_cost"],
            [[h["symbol"], h["qty"], h["avg_cost"], h["price"], h["value"], h["pl_pct"], h["valued_at_cost"]]
             for h in s["holdings"]]),
        "lessons.csv": csv_b(
            ["life_id", "lesson", "created", "in_current_core_wisdom"],
            [[bot_id, x["lesson"], x["created"], x["lesson"] in core_texts] for x in s["lessons"]]),
        "news.csv": csv_b(
            ["published", "source", "title", "link", "tags"],
            [[n["published"], n["source"], n["title"], n["link"], n["tags"]] for n in con.execute(
                "SELECT published, source, title, link, tags FROM news WHERE published>=? AND published<=? ORDER BY published",
                (S["bot"]["started"], S["end_iso"]))]),
        "summary.json": json.dumps(s, indent=2, ensure_ascii=False).encode("utf-8"),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    name = f"arena_life{bot_id}_{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}.zip"
    return name, buf.getvalue()


# ───────────────────────── export: all lives ─────────────────────────
def cause_text(S):
    """Why a life ended, from the ledger (a death gets a cause; other lives get their end reason)."""
    s = S["summary"]
    if s["status"] == "ALIVE":
        return "still running"
    if s["status"] == "ENDED":
        return "ended: model changed" if s.get("end_reason") == "model change" else "ended by a restart"
    parts = []
    if s["all_in"]:
        parts.append(f"went all-in on {base_of(s['all_in']['symbol'])} (turn {s['all_in']['turn']})")
    losers = [c for c in s["coin_results"] if c["net"] < 0]
    if losers:
        parts.append(f"biggest loss {base_of(losers[0]['symbol'])} {losers[0]['net']:+.2f}")
    if not parts:
        parts.append(f"slow bleed, fees ${s['trades']['fees_paid']:.2f}")
    return f"died at ${s['final_value']:.2f}: " + "; ".join(parts)


def all_lives_data(con, cfg, market):
    """Stats for every life plus the totals, memory ON vs OFF comparison and how the core lessons evolved."""
    lives = [life_stats(con, cfg, market, r["id"]) for r in con.execute("SELECT id FROM bots ORDER BY id")]
    rows = [S["summary"] for S in lives]
    for S in lives:
        S["summary"]["cause"] = cause_text(S)
    played = [r for r in rows if r["turns"] > 0]

    def group(items):
        if not items:
            return {"lives": 0, "deaths": 0, "avg_survival_seconds": None, "avg_pl_pct": None, "best_pl_pct": None}
        return {"lives": len(items), "deaths": sum(r["status"] == "DEAD" for r in items),
                "avg_survival_seconds": sum(r["duration_seconds"] for r in items) / len(items),
                "avg_pl_pct": sum(r["pl_pct"] for r in items) / len(items), "best_pl_pct": max(r["pl_pct"] for r in items)}

    snaps, prev, steps = [], [], []
    for r in con.execute("SELECT life_id, created, core FROM core_history ORDER BY id"):
        try:
            snaps.append((r["life_id"], r["created"], json.loads(r["core"])))
        except ValueError:
            continue
    for life_id, created, core in snaps:                    # diff each snapshot against the one before it
        old, new = {c["lesson"] for c in prev}, {c["lesson"] for c in core}
        steps.append({"after_life": life_id, "created": created, "size": len(core),
                      "added": [c for c in core if c["lesson"] not in old], "dropped": [c for c in prev if c["lesson"] not in new]})
        prev = core
    written = {r["life_id"]: r["n"] for r in con.execute("SELECT life_id, COUNT(*) n FROM lessons GROUP BY life_id")}
    agg = {
        "generated": now_iso(), "lives": len(rows), "deaths": sum(r["status"] == "DEAD" for r in rows),
        "ended_by_restart": sum(r["status"] == "ENDED" for r in rows), "running": sum(r["status"] == "ALIVE" for r in rows),
        "total_runtime_seconds": sum(r["duration_seconds"] for r in rows), "total_turns": sum(r["turns"] for r in rows),
        "total_trades": sum(r["trades"]["count"] for r in rows), "total_fees": sum(r["trades"]["fees_paid"] for r in rows),
        "longest_survival": max(rows, key=lambda r: r["duration_seconds"]) if rows else None,
        "best_life": max(played, key=lambda r: r["pl_pct"]) if played else None,
        "worst_life": min(played, key=lambda r: r["pl_pct"]) if played else None,
        "memory_on": group([r for r in rows if r["learning"] and r["turns"] > 0]),
        "memory_off": group([r for r in rows if not r["learning"] and r["turns"] > 0]),
        "lessons_written_per_life": written, "core_now": load_core(con), "core_evolution": steps,
    }
    return {"lives": lives, "agg": agg}


def svg_lives_chart(rows):
    """Survival time (bars) and final value as a % of start cash (dots and a dashed line) for every life.
    Percent keeps lives with different start cash comparable. Print-safe inline SVG."""
    W, H, L, R, T, B = 720, 270, 60, 62, 30, 34
    n = len(rows)
    if not n:
        return ""
    secs = [r["duration_seconds"] for r in rows]
    unit, div = ("hours", 3600.0) if max(secs) > 3 * 3600 else ("minutes", 60.0)
    bars = [s / div for s in secs]
    pct = [r["final_value"] / r["settings"]["start_cash"] * 100 if r["settings"]["start_cash"] else 100.0 for r in rows]
    bmax = max(max(bars), 1e-9) * 1.1
    plo, phi = min(min(pct), 100.0), max(max(pct), 100.0)
    pad = (phi - plo) * 0.12 or 2.0
    plo, phi = plo - pad, phi + pad
    slot = (W - L - R) / n
    bw = min(34.0, slot * 0.6)
    X = lambda i: L + slot * (i + 0.5)
    YB = lambda v: T + (1 - v / bmax) * (H - T - B)
    YP = lambda v: T + (phi - v) / (phi - plo) * (H - T - B)
    color = {"DEAD": "#b42318", "ENDED": "#8c929a", "ALIVE": "#2f2fb8"}
    out = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" font-family="Arial, sans-serif" font-size="10" '
           f'aria-label="Survival time and final value per life">']
    for k in range(5):
        v = bmax * k / 4
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{YB(v):.1f}" y2="{YB(v):.1f}" stroke="#e3e3e3"/>'
                   f'<text x="{L - 6}" y="{YB(v) + 3:.1f}" text-anchor="end" fill="#555">{v:.3g}</text>')
        pv = plo + (phi - plo) * k / 4
        out.append(f'<text x="{W - R + 6}" y="{YP(pv) + 3:.1f}" fill="#c77700">{pv:.0f}%</text>')
    out.append(f'<text x="{L}" y="14" fill="#555">Survival time ({unit})</text>'
               f'<text x="{W - R}" y="14" text-anchor="end" fill="#c77700">Final value, % of start cash</text>')
    out.append(f'<line x1="{L}" x2="{W - R}" y1="{YP(100):.1f}" y2="{YP(100):.1f}" stroke="#c77700" stroke-width="0.8" stroke-dasharray="2 4"/>')
    for i, r in enumerate(rows):
        h = (H - B) - YB(bars[i])
        out.append(f'<rect x="{X(i) - bw / 2:.1f}" y="{YB(bars[i]):.1f}" width="{bw:.1f}" height="{max(h, 0.5):.1f}" '
                   f'fill="{color[r["status"]]}" opacity="0.85"/>'
                   f'<text x="{X(i):.1f}" y="{H - 18}" text-anchor="middle" fill="#222">#{r["life_id"]}{chr(9733) if r["learning"] else ""}</text>')
    pts = " ".join(f"{X(i):.1f},{YP(v):.1f}" for i, v in enumerate(pct))
    if n > 1:
        out.append(f'<polyline fill="none" stroke="#c77700" stroke-width="1.6" stroke-dasharray="6 3" points="{pts}"/>')
    for i, v in enumerate(pct):
        out.append(f'<circle cx="{X(i):.1f}" cy="{YP(v):.1f}" r="3.4" fill="#fff" stroke="#c77700" stroke-width="1.8"/>')
    lx = L
    for label, c in (("died", color["DEAD"]), ("ended by restart", color["ENDED"]), ("running", color["ALIVE"])):
        out.append(f'<rect x="{lx}" y="{H - 9}" width="9" height="8" fill="{c}" opacity="0.85"/><text x="{lx + 13}" y="{H - 2}" fill="#222">{label}</text>')
        lx += 20 + len(label) * 5.6
    out.append(f'<text x="{lx + 6}" y="{H - 2}" fill="#222">{chr(9733)} memory on · dots = final value</text></svg>')
    return "".join(out)


def render_all_report(A, bot_name):
    e = html.escape
    lives, agg = A["lives"], A["agg"]
    rows = [S["summary"] for S in lives]
    money = lambda x: ("-" if x < 0 else "") + f"${abs(x):,.2f}"
    cls = lambda x: "pos" if x >= 0 else "neg"

    def box(k, v, sub=""):
        return f'<div class="box"><div class="k">{e(k)}</div><div class="v">{v}</div><div class="s">{sub}</div></div>'

    def life_ref(r):
        return (f'Life #{r["life_id"]}: <span class="{cls(r["pl_pct"])}">{r["pl_pct"]:+.1f}%</span>',
                f'{money(r["final_value"])} from {money(r["settings"]["start_cash"])}, {e(short_duration(r["duration_seconds"]))}, memory {"ON" if r["learning"] else "OFF"}')

    def group_row(label, g):
        if not g["lives"]:
            return f"<tr><td>{label}</td><td class=\"r\">0</td><td class=\"r\">-</td><td class=\"r\">-</td><td class=\"r\">-</td><td class=\"r\">-</td></tr>"
        return (f'<tr><td>{label}</td><td class="r">{g["lives"]}</td><td class="r">{g["deaths"]}</td>'
                f'<td class="r">{e(short_duration(g["avg_survival_seconds"]))}</td>'
                f'<td class="r {cls(g["avg_pl_pct"])}">{g["avg_pl_pct"]:+.1f}%</td><td class="r {cls(g["best_pl_pct"])}">{g["best_pl_pct"]:+.1f}%</td></tr>')

    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
           f'<title>{e(bot_name)} - all lives report</title><style>{REPORT_CSS}</style></head><body><div class="page">',
           '<div class="bar no-print"><span class="sub">Use your browser\'s print dialog: paper size A4, margins default.</span>'
           '<button class="btn" type="button" onclick="window.print()">Print / Save as PDF</button></div>',
           f'<h1>{e(bot_name)} — all lives report</h1>',
           f'<div class="sub">{agg["lives"]} lives in the ledger &middot; generated {e(fmt_when(agg["generated"]))}</div>']

    out.append("<h2>Overview</h2><div class=\"grid\">"
               + box("Lives", str(agg["lives"]), f'{agg["running"]} running, {agg["ended_by_restart"]} ended by a restart')
               + box("Deaths", str(agg["deaths"]), "total value fell below the death line")
               + box("Total runtime", e(fmt_duration(agg["total_runtime_seconds"])), f'{agg["total_turns"]} turns')
               + box("Trades", str(agg["total_trades"]), f'fees paid {money(agg["total_fees"])}')
               + (box("Best life", *life_ref(agg["best_life"])) if agg["best_life"] else box("Best life", "n/a"))
               + (box("Worst life", *life_ref(agg["worst_life"])) if agg["worst_life"] else box("Worst life", "n/a"))
               + (box("Longest survival", f'Life #{agg["longest_survival"]["life_id"]}',
                      e(short_duration(agg["longest_survival"]["duration_seconds"]))) if agg["longest_survival"] else "")
               + "</div>")

    out.append("<h2>Survival time and final value per life</h2>" + svg_lives_chart(rows))

    out.append('<h2>Every life</h2><table><colgroup><col style="width:5%"><col style="width:12%"><col style="width:10%"><col style="width:9%">'
               '<col style="width:6%"><col style="width:9%"><col style="width:9%"><col style="width:12%"><col style="width:28%"></colgroup>'
               '<thead><tr><th>Life</th><th>Model</th><th>Start / fee</th><th>Memory</th><th class="r">Turns</th><th class="r">Survived</th>'
               '<th class="r">Peak</th><th class="r">Final</th><th>Outcome / cause of death</th></tr></thead><tbody>')
    for r in rows:
        out.append(f'<tr><td>#{r["life_id"]}</td><td>{e(str(r["model"]))}</td>'
                   f'<td>{money(r["settings"]["start_cash"])}<br>{r["settings"]["fee_pct"]:.2f}%</td>'
                   f'<td>{"ON" if r["learning"] else "OFF"}</td><td class="r">{r["turns"]}</td>'
                   f'<td class="r">{e(short_duration(r["duration_seconds"]))}</td><td class="r">{money(r["drawdown"]["peak_value"])}</td>'
                   f'<td class="r">{money(r["final_value"])}<br><span class="{cls(r["pl_pct"])}">{r["pl_pct"]:+.1f}%</span></td>'
                   f'<td>{e(r["cause"])}</td></tr>')
    out.append("</tbody></table>")

    out.append("<h2>Memory on vs off</h2>"
               '<table><colgroup><col style="width:28%"><col style="width:12%"><col style="width:12%"><col style="width:16%"><col style="width:16%"><col style="width:16%"></colgroup>'
               '<thead><tr><th>Group (lives with at least one turn)</th><th class="r">Lives</th><th class="r">Deaths</th>'
               '<th class="r">Avg survival</th><th class="r">Avg P/L</th><th class="r">Best P/L</th></tr></thead><tbody>'
               + group_row("Memory ON", agg["memory_on"]) + group_row("Memory OFF", agg["memory_off"]) + "</tbody></table>"
               '<div class="sub">Small samples prove little; compare lives that share the same model, cash, fee and death line.</div>')

    out.append("<h2>How the core lessons evolved</h2>")
    steps = agg["core_evolution"]
    if steps:
        out.append('<div class="sub">After each reflection the lessons are merged into at most 10 core lessons. Added lessons show the life that wrote them.</div>')
        for st in steps:
            out.append(f'<div class="keep" style="margin-top:2.5mm"><b>After life #{st["after_life"]}</b> '
                       f'<span class="sub">({e(fmt_when(st["created"]))}, {st["size"]} core lessons)</span><ul class="lessons">'
                       + "".join(f'<li><b class="pos">+</b> {e(c["lesson"])} <span class="sub">(life #{c["life"]})</span></li>' for c in st["added"])
                       + "".join(f'<li><b class="neg">&minus;</b> <s>{e(c["lesson"])}</s> <span class="sub">(from life #{c["life"]}, dropped)</span></li>' for c in st["dropped"])
                       + ("" if st["added"] or st["dropped"] else '<li class="sub">No change.</li>') + "</ul></div>")
    else:
        out.append('<div class="sub">No lesson history yet: it is recorded each time a life with memory ON ends and the agent reflects.</div>')
    written = agg["lessons_written_per_life"]
    if written:
        out.append('<div class="sub" style="margin-top:3mm">Lessons written per life: '
                   + ", ".join(f"#{k}: {v}" for k, v in sorted(written.items())) + ".</div>")
    out.append("<h2>Core lessons now</h2>")
    out.append('<ol class="lessons">' + "".join(f'<li>{e(c["lesson"])} <span class="sub">(life #{c["life"]})</span></li>' for c in agg["core_now"]) + "</ol>"
               if agg["core_now"] else '<div class="sub">None yet.</div>')

    out.append('<div class="disclaimer"><b>Disclaimer.</b> This is a simulation. No real money was used and no real orders were placed. '
               'Nothing here is financial advice, and the results say nothing about how a real strategy would perform.</div>')
    out.append("</div></body></html>")
    return "".join(out).encode("utf-8")


def build_all_zip(con, cfg, A, delim=","):
    """Every CSV across all lives in one ZIP, each with a life_id column, plus lives.csv, core_history.csv and summary.json."""
    csv_b = lambda header, rows: csv_bytes(header, rows, delim)
    lives, agg = A["lives"], A["agg"]
    core_texts = {c["lesson"] for c in agg["core_now"]}
    q = lambda sql: [list(r) for r in con.execute(sql)]
    news_rows = []
    for S in lives:
        for n in con.execute("SELECT published, source, title, link, tags FROM news WHERE published>=? AND published<=? ORDER BY published",
                             (S["bot"]["started"], S["end_iso"])):
            news_rows.append([S["bot"]["id"]] + list(n))
    files = {
        "lives.csv": csv_b(
            ["life_id", "model", "started", "ended", "end_reason", "status", "memory_on", "start_cash", "fee_pct", "turns",
             "survival_seconds", "peak_value", "final_value", "pl_usd", "pl_pct", "trades", "fees_paid", "cause", "summary"],
            [[s["life_id"], s["model"], s["started"], s["ended"] or s["died"], s["end_reason"], s["status"], s["learning"],
              s["settings"]["start_cash"], s["settings"]["fee_pct"], s["turns"], s["duration_seconds"], s["drawdown"]["peak_value"],
              s["final_value"], s["pl_usd"], s["pl_pct"], s["trades"]["count"], s["trades"]["fees_paid"], s["cause"], s["life_summary"]]
             for s in (S["summary"] for S in lives)]),
        "trades.csv": csv_b(
            ["life_id", "id", "ts", "turn", "action", "symbol", "qty", "price", "usd", "fee", "cash_after", "qty_after", "reason"],
            q("SELECT bot_id, id, ts, turn, action, symbol, qty, price, usd, fee, cash_after, qty_after, reason FROM trades ORDER BY bot_id, id")),
        "equity.csv": csv_b(
            ["life_id", "ts", "btc_price", "cash", "positions_value", "equity"],
            q("SELECT bot_id, ts, btc_price, cash, positions_value, equity FROM equity ORDER BY bot_id, id")),
        "decisions.csv": csv_b(
            ["life_id", "ts", "turn", "action", "orders", "watch", "reason", "note", "error", "thinking", "prompt_tokens",
             "eval_tokens", "duration_seconds", "stage2_calls"],
            q("SELECT bot_id, ts, turn, action, orders, watch, reason, note, error, thinking, prompt_tokens, eval_tokens, duration, stage2 "
              "FROM decisions ORDER BY bot_id, id")),
        "positions.csv": csv_b(
            ["life_id", "symbol", "qty", "avg_cost", "price", "value", "pl_pct", "valued_at_cost"],
            [[S["bot"]["id"], h["symbol"], h["qty"], h["avg_cost"], h["price"], h["value"], h["pl_pct"], h["valued_at_cost"]]
             for S in lives for h in S["summary"]["holdings"]]),
        "lessons.csv": csv_b(
            ["life_id", "lesson", "created", "in_current_core_wisdom"],
            [[r[0], r[1], r[2], r[1] in core_texts] for r in q("SELECT life_id, lesson, created FROM lessons ORDER BY id")]),
        "core_history.csv": csv_b(
            ["after_life_id", "snapshot_created", "position", "lesson", "taught_by_life_id"],
            [[r["life_id"], r["created"], i, c["lesson"], c["life"]] for r in con.execute("SELECT life_id, created, core FROM core_history ORDER BY id")
             for i, c in enumerate(json.loads(r["core"]), 1)]),
        "news.csv": csv_b(["life_id", "published", "source", "title", "link", "tags"], news_rows),
        "summary.json": json.dumps({"overview": agg, "lives": [S["summary"] for S in lives]}, indent=2, ensure_ascii=False, default=str).encode("utf-8"),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return f"arena_all_lives_{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}.zip", buf.getvalue()


def build_runs(cfg, market):
    """Every life, newest first: start cash, fee, turns, final value and whether it died or was ended."""
    con = connect(cfg.db)
    try:
        cur = current_bot(con)
        out = []
        for r in con.execute("SELECT * FROM bots ORDER BY id DESC"):
            b = dict(r)
            is_current = bool(cur) and b["id"] == cur["id"]
            value = b["final_value"] if b["final_value"] is not None else b["start_cash"]
            if is_current and b["alive"]:
                value = total_value(b["cash"], load_positions(con, b["id"]), market.prices)
            status = "died" if not b["alive"] else ("running" if is_current and not b["ended"] else "ended")
            t_start, t_end = parse_ts(b["started"]), parse_ts(b["ended"] or b["died"]) or datetime.now(timezone.utc)
            out.append({"id": b["id"], "model": b["model"], "started": b["started"], "ended": b["ended"] or b["died"],
                        "start_cash": b["start_cash"], "fee_pct": (b["fee_rate"] or 0) * 100, "turns": b["ticks"],
                        "final_value": value, "status": status, "current": is_current,
                        "learning": bool(b["learning"]), "summary": b["summary"],
                        "duration_seconds": round((t_end - t_start).total_seconds()) if t_start else 0})
        return out
    finally:
        con.close()


def build_state(cfg, market, mood):
    con = connect(cfg.db)
    try:
        bot = current_bot(con)
        if not bot:
            return {"ready": False}
        prices = market.prices
        eq = [dict(r) for r in con.execute(
            "SELECT ts, btc_price, equity FROM equity WHERE bot_id=? ORDER BY id", (bot["id"],))]
        if len(eq) > 1500:                                         # keep the chart light
            step = math.ceil(len(eq) / 1500)
            last = eq[-1]
            eq = eq[::step]
            if eq[-1] is not last:
                eq.append(last)
        holdings = []
        for s, p in load_positions(con, bot["id"]).items():
            now = prices.get(s, p["avg"])
            holdings.append({"symbol": s, "qty": p["qty"], "avg_cost": p["avg"], "price": now,
                             "value": p["qty"] * now, "pl_pct": (now / p["avg"] - 1) * 100 if p["avg"] else 0.0})
        holdings.sort(key=lambda h: -h["value"])
        equity_now = bot["cash"] + sum(h["value"] for h in holdings)
        trades = [dict(r) for r in con.execute("SELECT * FROM trades WHERE bot_id=? ORDER BY id DESC LIMIT 200", (bot["id"],))]
        decisions = [dict(r) for r in con.execute(
            "SELECT ts, turn, action, orders, watch, reason, note, error, prompt_tokens, eval_tokens, thinking, duration FROM decisions "
            "WHERE bot_id=? ORDER BY id DESC LIMIT 30", (bot["id"],))]
        agg = con.execute("SELECT COUNT(*) n, COALESCE(SUM(fee),0) fees FROM trades WHERE bot_id=?", (bot["id"],)).fetchone()
        avg_turn = con.execute("SELECT AVG(duration) FROM (SELECT duration FROM decisions WHERE bot_id=? AND duration IS NOT NULL "
                               "ORDER BY id DESC LIMIT 50)", (bot["id"],)).fetchone()[0]
        news = [dict(r) for r in con.execute(
            "SELECT title, link, source, published, tags FROM news ORDER BY published DESC LIMIT 30")]
        return {
            "ready": True, "bot": bot, "btc_price": market.btc, "equity_now": equity_now,
            "holdings": holdings, "trade_count": agg["n"], "fees_paid": agg["fees"],
            "config": {"market": market.label, "start_cash": bot["start_cash"], "fee_rate": bot["fee_rate"],
                       "death_line": cfg.death_line, "living_cost": cfg.living_cost, "interval": cfg.interval,
                       "universe_size": len(market.universe), "min_volume": cfg.min_volume, "avg_turn_seconds": avg_turn},
            "settings": {"start_cash": float(get_setting(con, "start_cash") or bot["start_cash"]),
                         "fee_pct": float(get_setting(con, "fee_pct") or bot["fee_rate"] * 100)},
            "restart_pending": RESTART_REQ["req"], "lan": cfg.lan,
            "memory": {
                "learning_this_life": bool(bot["learning"]), "learning_next": get_toggle(con, "learning"),
                "auto_restart": get_toggle(con, "auto_restart"), "max_lives": cfg.max_lives,
                "lives": con.execute("SELECT COUNT(*) FROM bots").fetchone()[0],
                "deaths": con.execute("SELECT COUNT(*) FROM bots WHERE alive=0").fetchone()[0],
                "core_lessons": load_core(con), "phase": AGENT["phase"], "reflecting_life": AGENT["reflecting_life"],
                "next_life_in": max(0, round(AGENT["next_life_at"] - time.time())) if AGENT["next_life_at"] else None,
                "wisdom": bot.get("wisdom") or "",
            },
            "mood": {"fng": mood.fng, "trending": mood.trending},
            "equity": eq, "trades": trades, "decisions": decisions, "news": news,
        }
    finally:
        con.close()


def make_handler(cfg, market, mood):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/state":
                try:
                    self._send(200, json.dumps(build_state(cfg, market, mood)).encode(), "application/json")
                except Exception as e:
                    self._send(500, json.dumps({"ready": False, "error": str(e)}).encode(), "application/json")
            elif path == "/api/runs":
                try:
                    self._send(200, json.dumps(build_runs(cfg, market)).encode(), "application/json")
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            elif path in ("/api/report", "/api/export.zip"):
                self._export(path)
            elif path in ("/api/report/all", "/api/export_all.zip"):
                self._export_all(path)
            else:
                self._send(404, b"not found", "text/plain")

        def _local_error(self, strict=False):
            """Same access rule as restart: loopback only (and a sane Host header) unless --lan is set.
            strict=True ignores --lan: some things are never allowed from the network."""
            if cfg.lan and not strict:
                return None
            if self.client_address[0] not in LOOPBACK:
                return "only accepted from localhost (start with --lan to allow your network)"
            if (urllib.parse.urlsplit("//" + (self.headers.get("Host") or "")).hostname or "") not in ("localhost", "127.0.0.1", "::1"):
                return "unexpected Host header"
            return None

        def _export_all(self, path):
            """One report / one ZIP covering every life. Read-only, same access rule as the other exports."""
            err = self._local_error()
            if err:
                return self._json(403, {"error": "exports are " + err})
            con = open_readonly(cfg)
            try:
                A = all_lives_data(con, cfg, market)
                if path == "/api/report/all":
                    return self._send(200, render_all_report(A, cfg.bot_name), "text/html; charset=utf-8")
                sep = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("sep", [None])[0]
                delim = {"comma": ",", "semicolon": ";"}.get(sep, cfg.csv_sep)
                name, data = build_all_zip(con, cfg, A, delim)
                self._send(200, data, "application/zip", {"Content-Disposition": f'attachment; filename="{name}"'})
            except Exception as e:
                self._json(500, {"error": f"export failed: {e}"})
            finally:
                con.close()

        def _export(self, path):
            err = self._local_error()
            if err:
                return self._json(403, {"error": "exports are " + err})
            run = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("run", [None])[0]
            con = open_readonly(cfg)
            try:
                if run is None:
                    cur = current_bot(con)
                    bot_id = cur["id"] if cur else None
                else:
                    try:
                        bot_id = int(run)
                    except ValueError:
                        return self._json(400, {"error": "run must be a life number"})
                S = life_stats(con, cfg, market, bot_id) if bot_id is not None else None
                if not S:
                    return self._json(404, {"error": "no such life"})
                if path == "/api/report":
                    return self._send(200, render_report(S), "text/html; charset=utf-8")
                sep = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("sep", [None])[0]
                delim = {"comma": ",", "semicolon": ";"}.get(sep, cfg.csv_sep)
                name, data = build_export_zip(con, S, delim)
                self._send(200, data, "application/zip", {"Content-Disposition": f'attachment; filename="{name}"'})
            except Exception as e:
                self._json(500, {"error": f"export failed: {e}"})
            finally:
                con.close()

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _json_body(self, strict=False):
            """Checks shared by every POST: same-origin, JSON-only, localhost-only (unless --lan), small body.
            A random web page can't change anything. strict=True (used by shutdown) is localhost-only even with --lan.
            Returns the parsed dict, or None after sending an error."""
            err = self._local_error(strict)
            if err:
                self._json(403, {"error": "changes are " + err if err.startswith("only") else err})
                return None
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
                self._json(403, {"error": "cross-origin request refused"})
                return None
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                self._json(415, {"error": "Content-Type must be application/json"})
                return None
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 < n <= 1024:
                self._json(413, {"error": "bad body size"})
                return None
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                body = None
            if not isinstance(body, dict):
                self._json(400, {"error": "body must be a JSON object"})
                return None
            return body

        def _settings(self, body):
            """POST /api/settings {"learning": bool, "auto_restart": bool}: either or both. Auto-restart applies at the
            next death; learning applies from the next life (a life never changes its memory halfway)."""
            known = {k: body[k] for k in ("learning", "auto_restart") if k in body}
            if not known or any(not isinstance(v, bool) for v in known.values()):
                return self._json(400, {"error": "send {\"learning\": true/false} and/or {\"auto_restart\": true/false}"})
            con = connect(cfg.db)
            try:
                for k, v in known.items():
                    set_toggle(con, k, v)
                con.commit()
                out = {"learning": get_toggle(con, "learning"), "auto_restart": get_toggle(con, "auto_restart")}
            finally:
                con.close()
            WAKE.set()                                      # let a waiting auto-restart notice the change at once
            self._json(200, {"ok": True, **out})

        def do_POST(self):
            path = self.path.split("?")[0]
            if path not in ("/api/restart", "/api/settings", "/api/shutdown"):
                return self._json(404, {"error": "not found"})
            body = self._json_body(strict=(path == "/api/shutdown"))
            if body is None:
                return
            if path == "/api/shutdown":
                self._json(200, {"ok": True, "stopping": True})
                threading.Thread(target=graceful_exit, args=("Shutdown requested",), daemon=True).start()
                return
            if path == "/api/settings":
                return self._settings(body)
            try:
                start_cash, fee_pct = body["start_cash"], body["fee_pct"]
            except (KeyError, TypeError):
                return self._json(400, {"error": "body must be JSON like {\"start_cash\": 50, \"fee_pct\": 0.1}"})
            for v in (start_cash, fee_pct):
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                    return self._json(400, {"error": "start_cash and fee_pct must be numbers"})
            if not 1 <= start_cash <= 100000:
                return self._json(400, {"error": "start_cash must be between 1 and 100000"})
            if not 0 <= fee_pct <= 5:
                return self._json(400, {"error": "fee_pct must be between 0 and 5"})
            with RESTART_LOCK:
                RESTART_REQ["req"] = {"start_cash": round(float(start_cash), 2), "fee_pct": round(float(fee_pct), 4)}
            WAKE.set()
            self._json(202, {"ok": True, "pending": True, "start_cash": round(float(start_cash), 2),
                             "fee_pct": round(float(fee_pct), 4)})

        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def check_ollama(host, model):
    try:
        tags = json.loads(http_get(f"{host}/api/tags", timeout=5))
    except Exception as e:
        raise SystemExit(f"Can't reach Ollama at {host}. Start it first (ollama serve / the Ollama app). ({e})")
    names = {m.get("name", "") for m in tags.get("models", [])}
    if model not in names and f"{model}:latest" not in names:
        print(f"[warn] model '{model}' isn't pulled yet. Run:  ollama pull {model}")


# ───────────────────────── main ─────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Autonomous multi-coin LLM trader with a fake ledger and live dashboard")
    ap.add_argument("--model", default="deepseek-r1:14b",
                    help="Ollama model name. A life never mixes models: starting with a different one archives the current life and begins a new one")
    ap.add_argument("--name", dest="bot_name", default="DeepSeek", help="label for the bot")
    ap.add_argument("--cash", dest="start_cash", type=float, default=50.0,
                    help="starting cash in $ (only seeds a brand-new ledger; later changes come from the dashboard)")
    ap.add_argument("--fee", dest="fee_pct", type=float, default=0.10,
                    help="fee in %% on every buy and sell (only seeds a brand-new ledger)")
    ap.add_argument("--death", dest="death_line", type=float, default=1.0, help="dies below this total value")
    ap.add_argument("--living-cost", type=float, default=0.0, help="$ charged every turn just for being alive")
    ap.add_argument("--interval", type=float, default=120,
                    help="seconds from the start of one turn to the start of the next (a slower turn starts the next at once)")
    ap.add_argument("--min-volume", type=float, default=50_000_000,
                    help="24h quote volume ($) a coin needs before it can be bought (sells always allowed)")
    ap.add_argument("--market", choices=["binance", "random"], default="binance",
                    help="real Binance top-40 coins, or 40 fake random-walk coins")
    ap.add_argument("--host", default="http://localhost:11434", help="Ollama URL")
    ap.add_argument("--port", type=int, default=8001, help="dashboard port")
    ap.add_argument("--lan", action="store_true",
                    help="listen on all interfaces (phone/other PCs) and accept restarts from them; default is 127.0.0.1 only")
    ap.add_argument("--db", default="arena.db", help="SQLite ledger file (relative to this script)")
    ap.add_argument("--reset", action="store_true", help="wipe the ledger and start a new life")
    ap.add_argument("--num-ctx", type=int, default=12288,
                    help="Ollama context window in tokens. Must hold the prompt plus the reasoning budget (about 6k is enough). 12288 keeps the 14b fully on a 12 GB GPU; if `ollama ps` shows CPU, lower it to 10240 or 8192")
    ap.add_argument("--think-budget", type=int, default=2000,
                    help="max reasoning tokens before the answer is forced into the JSON schema (0 = skip reasoning)")
    ap.add_argument("--csv-delimiter", choices=["auto", "comma", "semicolon"], default="auto",
                    help="CSV export style. auto follows your regional settings: a decimal comma means ';' columns and decimal commas (what Excel expects)")
    ap.add_argument("--learning", nargs="?", const="on", choices=["on", "off"], default=None,
                    help="cross-life memory: when a life ends the agent reflects, and later lives start with its lessons "
                         "(default on). Only changes the saved setting when you pass it; the dashboard toggle also edits it. "
                         "Applies from the next life, so compare 'off' and 'on' runs fairly")
    ap.add_argument("--auto-restart", nargs="?", const="on", choices=["on", "off"], default=None,
                    help="when the agent dies: reflect, wait --restart-delay seconds, then start a new life with the same "
                         "settings and model (default on; saved setting, also a dashboard toggle)")
    ap.add_argument("--max-lives", type=int, default=20,
                    help="auto-restart stops once the ledger holds this many lives (default 20)")
    ap.add_argument("--restart-delay", type=float, default=60, help="seconds to wait before an auto-restart (default 60)")
    ap.add_argument("--mock", action="store_true", help="no LLM, random multi-coin orders (to test the dashboard)")
    cfg = ap.parse_args()
    cfg.fee_rate = cfg.fee_pct / 100
    cfg.csv_sep = {"comma": ",", "semicolon": ";"}.get(cfg.csv_delimiter) or (";" if system_decimal_separator() == "," else ",")
    for stream in (sys.stdout, sys.stderr):         # a legacy Windows code page must never crash the loop on a print
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    if not os.path.isabs(cfg.db):
        cfg.db = os.path.join(HERE, cfg.db)
    if cfg.reset:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(cfg.db + suffix):
                os.remove(cfg.db + suffix)
        print("Ledger wiped. New life.")

    if not cfg.mock:
        check_ollama(cfg.host, cfg.model)

    market = make_market(cfg)
    mood = Mood()
    mood.update()
    con = init_db(cfg.db)
    sync_settings(con, cfg)
    sync_toggles(con, cfg)
    fetch_news(con, market.universe)
    print(f"Memory (cross-life learning): {'ON' if get_toggle(con, 'learning') else 'OFF'} for the next life | "
          f"auto-restart after death: {'ON' if get_toggle(con, 'auto_restart') else 'OFF'} "
          f"(wait {cfg.restart_delay:g}s, at most {cfg.max_lives} lives) | core lessons stored: {len(load_core(con))}")
    con.close()

    print(f"Tradable universe ({len(market.universe)} coins, sorted by 24h volume; '*' = under the "
          f"{cfg.min_volume / 1e6:g}M minimum, sell-only):")
    row = []
    for s in market.universe:
        vol = market.stats.get(s, (0, 0))[1]
        row.append(f"{base_of(s)}{'*' if vol < cfg.min_volume else ''} {fmt_vol(vol)}")
    print("  " + " | ".join(row))
    if getattr(market, "tokenized", None) is not None:
        print(f"  Excluded: {len(market.tokenized)} tokenized stocks/ETFs flagged by exchangeInfo + exclude.txt entries")
    print()
    server = ThreadingHTTPServer(("0.0.0.0" if cfg.lan else "127.0.0.1", cfg.port), make_handler(cfg, market, mood))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Dashboard: http://localhost:{cfg.port}" + ("  (also on your network: http://<this-pc-ip>:%d)" % cfg.port if cfg.lan
                                                       else "  (local only; use --lan for phone access)"))
    print(f"Bot: {cfg.bot_name} | model: {'mock' if cfg.mock else cfg.model} | market: {market.label} "
          f"({len(market.universe)} coins) | next life: start ${cfg.start_cash:.2f}, fee {cfg.fee_pct:g}% "
          f"| a turn every {cfg.interval:g}s | Ctrl+C to stop\n")

    RUNTIME["db"] = cfg.db

    def on_signal(signum, frame):
        # Ctrl+C, a plain `kill`, or closing the console: stop through the same graceful path as /api/shutdown.
        threading.Thread(target=graceful_exit, args=(f"Signal {signum} received",), daemon=True).start()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), on_signal)
            except (ValueError, OSError):
                pass

    try:
        run_sim(cfg, market, mood)
    except KeyboardInterrupt:
        print("\nStopped. The ledger is saved; run again to continue this life.")
    finally:
        STOP.set()
        WAKE.set()
        server.shutdown()


if __name__ == "__main__":
    main()
