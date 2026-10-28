#!/usr/bin/env python3
"""
LLM Trading Arena
An autonomous LLM trader (via Ollama) with a simulated SQLite ledger and a live dashboard.
No exchange account, no API keys. Python standard library only.

Quick start:
    ollama pull deepseek-r1:8b
    python arena.py
    -> open http://localhost:8001
"""
import argparse
import json
import math
import os
import random
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STOP = threading.Event()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_price(p):
    return f"{p:,.2f}" if p >= 1 else f"{p:.6f}"


# ───────────────────────── database (the ledger) ─────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS bots(
    id INTEGER PRIMARY KEY, name TEXT UNIQUE, model TEXT,
    cash REAL, qty REAL, alive INTEGER, started TEXT, died TEXT, ticks INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS trades(
    id INTEGER PRIMARY KEY, bot_id INTEGER, ts TEXT, turn INTEGER, action TEXT,
    qty REAL, price REAL, usd REAL, fee REAL, cash_after REAL, qty_after REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS equity(
    id INTEGER PRIMARY KEY, bot_id INTEGER, ts TEXT, price REAL, cash REAL, qty REAL, equity REAL);
CREATE TABLE IF NOT EXISTS decisions(
    id INTEGER PRIMARY KEY, bot_id INTEGER, ts TEXT, turn INTEGER, action TEXT,
    reason TEXT, note TEXT, error TEXT);
"""


def connect(path):
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db(path):
    con = connect(path)
    con.executescript(SCHEMA)
    con.commit()
    return con


def get_or_create_bot(con, cfg):
    row = con.execute("SELECT * FROM bots WHERE name=?", (cfg.bot_name,)).fetchone()
    if row:
        return dict(row)
    model = "mock (random)" if cfg.mock else cfg.model
    con.execute(
        "INSERT INTO bots(name, model, cash, qty, alive, started, ticks) VALUES(?,?,?,?,1,?,0)",
        (cfg.bot_name, model, cfg.start_cash, 0.0, now_iso()),
    )
    con.commit()
    return dict(con.execute("SELECT * FROM bots WHERE name=?", (cfg.bot_name,)).fetchone())


# ───────────────────────── market ─────────────────────────
class Market:
    """Real prices from Binance's public API (no account), or a random-walk fake coin."""

    def __init__(self, kind, symbol):
        self.kind = kind
        self.symbol = symbol
        self.history = []
        self.price = None

    @property
    def label(self):
        return self.symbol if self.kind == "binance" else "SIMCOIN"

    @property
    def asset(self):
        if self.kind != "binance":
            return "SIM"
        for quote in ("USDT", "USDC", "FDUSD", "EUR", "BUSD"):
            if self.symbol.endswith(quote):
                return self.symbol[: -len(quote)]
        return self.symbol

    def _get(self, url):
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())

    def warmup(self):
        if self.kind == "binance":
            try:
                kl = self._get(
                    f"https://api.binance.com/api/v3/klines?symbol={self.symbol}&interval=1m&limit=30")
                self.history = [float(k[4]) for k in kl]
                self.price = self.history[-1]
                return
            except Exception as e:
                print(f"[market] Binance unreachable ({e}); using the random-walk market instead.")
                self.kind = "random"
        self.price = 100.0
        self.history = [self.price]
        for _ in range(29):
            self._step()

    def _step(self):
        r = random.gauss(0, 0.005)                      # ~0.5% noise per turn
        if random.random() < 0.03:                      # occasional pump or crash
            r += random.choice([-1, 1]) * random.uniform(0.02, 0.06)
        self.price = max(0.01, self.price * math.exp(r))
        self.history.append(self.price)

    def tick(self):
        if self.kind == "binance":
            try:
                d = self._get(f"https://api.binance.com/api/v3/ticker/price?symbol={self.symbol}")
                self.price = float(d["price"])
            except Exception as e:
                print(f"[market] price fetch failed ({e}); reusing last price")
            self.history.append(self.price)
        else:
            self._step()
        self.history = self.history[-200:]
        return self.price


# ───────────────────────── the agent ─────────────────────────
def system_prompt(cfg, market):
    if cfg.living_cost > 0:
        living = (f"Being alive costs ${cfg.living_cost:.2f} every turn, taken from your cash "
                  f"(or from your holdings if you have no cash).")
    else:
        living = "Staying alive is free; only fees and bad trades can hurt you."
    return f"""You are {cfg.bot_name}, an autonomous trader living inside a simulation.
Nobody will give you instructions after this message. Every decision is yours.

THE RULES OF YOUR WORLD
- You started with ${cfg.start_cash:.2f} in cash. You can trade one asset: {market.label}.
- Every trade costs a {cfg.fee_rate * 100:.2f}% fee.
- {living}
- If your total value (cash + holdings) drops below ${cfg.death_line:.2f}, you die. Permanently.
- Your goal: stay alive and grow your money for as long as possible.
- Each turn you see the market and your own ledger, then choose ONE action.

Reply with ONLY a JSON object, nothing else:
{{"action": "buy" | "sell" | "hold", "usd": <dollars to spend, buy only>, "sell_pct": <1-100, percent of holdings to sell, sell only>, "reason": "<one short sentence>", "note": "<optional memo to your future self, or empty string>"}}
Example of a valid reply: {{"action": "hold", "usd": 0, "sell_pct": 0, "reason": "Price is flat, no edge.", "note": ""}}"""


def turn_prompt(con, cfg, market, bot, turn):
    p = market.price
    hist = market.history[-20:]
    chg = (hist[-1] / hist[0] - 1) * 100 if hist and hist[0] else 0.0
    hold_val = bot["qty"] * p
    eq = bot["cash"] + hold_val
    pl = (eq / cfg.start_cash - 1) * 100

    trades = con.execute(
        "SELECT * FROM trades WHERE bot_id=? ORDER BY id DESC LIMIT 5", (bot["id"],)).fetchall()
    notes = con.execute(
        "SELECT turn, note FROM decisions WHERE bot_id=? AND note<>'' ORDER BY id DESC LIMIT 8",
        (bot["id"],)).fetchall()

    trade_lines = "\n".join(
        f"- turn {t['turn']}: {t['action']} {t['qty']:.8f} @ {fmt_price(t['price'])} "
        f"(${t['usd']:.2f}, fee ${t['fee']:.3f}) because: {t['reason']}"
        for t in reversed(trades)) or "- none yet"
    note_lines = "\n".join(f"- turn {n['turn']}: {n['note']}" for n in reversed(notes)) or "- none yet"

    return f"""Turn {turn} ({now_iso()} UTC)

MARKET {market.label}: price {fmt_price(p)}
Recent prices, oldest to newest: {", ".join(fmt_price(x) for x in hist)}
Change over that window: {chg:+.2f}%

YOUR LEDGER
- Cash: ${bot['cash']:.2f}
- Holdings: {bot['qty']:.8f} {market.asset} (worth ${hold_val:.2f})
- Total value: ${eq:.2f} ({pl:+.2f}% vs start)
- Distance to death: ${eq - cfg.death_line:.2f}

YOUR LAST TRADES
{trade_lines}

YOUR NOTES TO YOURSELF
{note_lines}

Decide your action now. Keep your thinking brief (a few sentences), then answer with JSON only."""


def ask_ollama(host, model, system, user, think=False):
    if not think:
        # Skip R1's long hidden reasoning (an 8B model rambles past the token limit): pre-close the
        # think block in the raw DeepSeek chat format so the model answers with the JSON directly.
        prompt = (f"<｜begin▁of▁sentence｜>{system}<｜User｜>{user}"
                  f"<｜Assistant｜><think>\n\n</think>\n\n")
        body = json.dumps({
            "model": model, "stream": False, "raw": True, "prompt": prompt,
            "options": {"temperature": 0.7, "num_predict": 400, "num_ctx": 8192},
        }).encode()
        req = urllib.request.Request(f"{host}/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.loads(r.read())["response"]
    body = json.dumps({
        "model": model,
        "stream": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": 0.7, "num_predict": 4000, "num_ctx": 8192},
    }).encode()
    req = urllib.request.Request(f"{host}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        msg = json.loads(r.read())["message"]
    # Newer Ollama splits R1's reasoning into "thinking"; if the answer never arrived, fall back to it.
    return msg.get("content") or msg.get("thinking") or ""


def parse_action(text):
    """DeepSeek-R1 thinks out loud in <think> tags; strip that and grab the last JSON object."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    for cand in reversed(re.findall(r"\{[^{}]*\}", text, flags=re.S)):
        try:
            d = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and "action" in d:
            return d
    raise ValueError("no JSON action found in model reply")


def mock_decide():
    a = random.choices(["buy", "sell", "hold"], [0.35, 0.25, 0.40])[0]
    return {"action": a, "usd": round(random.uniform(2, 15), 2),
            "sell_pct": random.choice([25, 50, 100]),
            "reason": f"mock bot picked {a} at random",
            "note": random.choice(["", "", "", "price feels toppy", "buy dips, stay small"])}


# ───────────────────────── the referee ─────────────────────────
def referee(d, cash, qty, price, cfg):
    """The model only *asks*. This code decides what is legal and updates the ledger.
    Returns (cash, qty, trade_or_None, final_action, rejection_message_or_None)."""
    action = str(d.get("action", "hold")).strip().lower()
    try:
        if action == "buy":
            usd = float(d.get("usd") or 0)
            usd = min(usd, cash / (1 + cfg.fee_rate))
            if usd < 1.0:
                return cash, qty, None, "hold", "buy rejected: needs at least $1 and enough cash"
            fee = usd * cfg.fee_rate
            q = usd / price
            cash = max(0.0, cash - usd - fee)
            qty += q
            return cash, qty, dict(action="BUY", qty=q, price=price, usd=usd, fee=fee), "buy", None
        if action == "sell":
            pct = max(0.0, min(100.0, float(d.get("sell_pct") or 0)))
            q = qty if pct >= 100 else qty * pct / 100
            usd = q * price
            if q <= 0 or usd < 0.01:
                return cash, qty, None, "hold", "sell rejected: nothing to sell"
            fee = usd * cfg.fee_rate
            cash += usd - fee
            qty = 0.0 if pct >= 100 else qty - q
            return cash, qty, dict(action="SELL", qty=q, price=price, usd=usd, fee=fee), "sell", None
    except (TypeError, ValueError):
        return cash, qty, None, "hold", "invalid numbers in decision"
    return cash, qty, None, "hold", None


# ───────────────────────── simulation loop ─────────────────────────
def run_sim(cfg, market):
    con = init_db(cfg.db)
    bot = get_or_create_bot(con, cfg)
    if not con.execute("SELECT 1 FROM equity WHERE bot_id=?", (bot["id"],)).fetchone():
        con.execute("INSERT INTO equity(bot_id, ts, price, cash, qty, equity) VALUES(?,?,?,?,?,?)",
                    (bot["id"], now_iso(), market.price, bot["cash"], bot["qty"],
                     bot["cash"] + bot["qty"] * market.price))
        con.commit()

    system = system_prompt(cfg, market)
    announced_death = False

    while not STOP.is_set():
        t0 = time.time()
        bot = dict(con.execute("SELECT * FROM bots WHERE id=?", (bot["id"],)).fetchone())
        if not bot["alive"]:
            if not announced_death:
                print(f"\n☠  {bot['name']} died after {bot['ticks']} turns. Dashboard stays up; "
                      f"Ctrl+C to quit, or rerun with --reset for a new life.")
                announced_death = True
            STOP.wait(5)
            continue

        price = market.tick()
        turn = bot["ticks"] + 1
        error = None
        try:
            if cfg.mock:
                d = mock_decide()
            else:
                prompt = turn_prompt(con, cfg, market, bot, turn)
                for attempt in (1, 2):                          # one retry if the reply has no JSON
                    raw = ask_ollama(cfg.host, cfg.model, system, prompt, cfg.think)
                    try:
                        d = parse_action(raw)
                        break
                    except ValueError:
                        if attempt == 2:
                            raise ValueError(f"no JSON action in reply: {raw.strip()[:150]!r}")
        except Exception as e:
            d = {"action": "hold", "reason": "(no valid decision this turn)"}
            error = f"{type(e).__name__}: {e}"[:300]

        cash, qty, trade, action, rejected = referee(d, bot["cash"], bot["qty"], price, cfg)
        if rejected:
            error = f"{error}; {rejected}" if error else rejected
        reason = str(d.get("reason", ""))[:300]
        note = str(d.get("note", "") or "")[:300]
        ts = now_iso()

        if trade:
            con.execute(
                "INSERT INTO trades(bot_id, ts, turn, action, qty, price, usd, fee, cash_after, qty_after, reason)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (bot["id"], ts, turn, trade["action"], trade["qty"], trade["price"], trade["usd"],
                 trade["fee"], cash, qty, reason))

        if cfg.living_cost > 0:
            if cash >= cfg.living_cost:
                cash -= cfg.living_cost
            else:
                shortfall = cfg.living_cost - cash
                cash = 0.0
                qty = max(0.0, qty - shortfall / price)

        eq = cash + qty * price
        alive = 1 if eq >= cfg.death_line else 0

        con.execute("INSERT INTO decisions(bot_id, ts, turn, action, reason, note, error) VALUES(?,?,?,?,?,?,?)",
                    (bot["id"], ts, turn, action, reason, note, error))
        con.execute("INSERT INTO equity(bot_id, ts, price, cash, qty, equity) VALUES(?,?,?,?,?,?)",
                    (bot["id"], ts, price, cash, qty, eq))
        con.execute("UPDATE bots SET cash=?, qty=?, alive=?, ticks=?, died=? WHERE id=?",
                    (cash, qty, alive, turn, None if alive else ts, bot["id"]))
        con.commit()

        tag = trade["action"] if trade else "HOLD"
        print(f"turn {turn:>4} | {market.label} {fmt_price(price):>12} | {tag:<4} | "
              f"value ${eq:8.2f} | {reason[:70]}" + (f"  [!] {error[:60]}" if error else ""))

        STOP.wait(max(0.0, cfg.interval - (time.time() - t0)))


# ───────────────────────── dashboard server ─────────────────────────
def build_state(cfg, market):
    con = connect(cfg.db)
    try:
        row = con.execute("SELECT * FROM bots WHERE name=?", (cfg.bot_name,)).fetchone()
        if not row:
            return {"ready": False}
        bot = dict(row)
        eq = [dict(r) for r in con.execute(
            "SELECT ts, price, equity FROM equity WHERE bot_id=? ORDER BY id", (bot["id"],))]
        if len(eq) > 1500:                                     # keep the chart light
            step = math.ceil(len(eq) / 1500)
            last = eq[-1]
            eq = eq[::step]
            if eq[-1] is not last:
                eq.append(last)
        trades = [dict(r) for r in con.execute(
            "SELECT * FROM trades WHERE bot_id=? ORDER BY id DESC LIMIT 200", (bot["id"],))]
        decisions = [dict(r) for r in con.execute(
            "SELECT ts, turn, action, reason, note, error FROM decisions WHERE bot_id=? "
            "ORDER BY id DESC LIMIT 30", (bot["id"],))]
        agg = con.execute("SELECT COUNT(*) n, COALESCE(SUM(fee),0) fees FROM trades WHERE bot_id=?",
                          (bot["id"],)).fetchone()
        price = market.price
        return {
            "ready": True,
            "bot": bot,
            "price": price,
            "equity_now": bot["cash"] + bot["qty"] * price,
            "trade_count": agg["n"],
            "fees_paid": agg["fees"],
            "config": {
                "market": market.label, "asset": market.asset, "start_cash": cfg.start_cash,
                "fee_rate": cfg.fee_rate, "death_line": cfg.death_line,
                "living_cost": cfg.living_cost, "interval": cfg.interval,
            },
            "equity": eq,
            "trades": trades,
            "decisions": decisions,
        }
    finally:
        con.close()


def make_handler(cfg, market):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send(200, json.dumps(build_state(cfg, market)).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def check_ollama(host, model):
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as r:
            tags = json.loads(r.read())
    except Exception as e:
        raise SystemExit(f"Can't reach Ollama at {host}. Start it first (ollama serve / the Ollama app). ({e})")
    names = {m.get("name", "") for m in tags.get("models", [])}
    if model not in names and f"{model}:latest" not in names:
        print(f"[warn] model '{model}' isn't pulled yet. Run:  ollama pull {model}")


# ───────────────────────── main ─────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Autonomous LLM trader with a fake ledger and live dashboard")
    ap.add_argument("--model", default="deepseek-r1:8b", help="Ollama model name")
    ap.add_argument("--name", dest="bot_name", default="DeepSeek", help="bot name (one ledger per name)")
    ap.add_argument("--cash", dest="start_cash", type=float, default=50.0, help="starting cash in $")
    ap.add_argument("--fee", dest="fee_rate", type=float, default=0.001, help="fee per trade (0.001 = 0.1%%)")
    ap.add_argument("--death", dest="death_line", type=float, default=1.0, help="dies below this total value")
    ap.add_argument("--living-cost", type=float, default=0.0, help="$ charged every turn just for being alive")
    ap.add_argument("--interval", type=float, default=60, help="seconds between turns")
    ap.add_argument("--market", choices=["binance", "random"], default="binance",
                    help="real prices from Binance public API, or a fake random-walk coin")
    ap.add_argument("--symbol", default="BTCUSDT", help="Binance symbol, e.g. BTCUSDT, ETHUSDT, SOLUSDT")
    ap.add_argument("--host", default="http://localhost:11434", help="Ollama URL")
    ap.add_argument("--port", type=int, default=8001, help="dashboard port")
    ap.add_argument("--db", default="arena.db", help="SQLite ledger file")
    ap.add_argument("--reset", action="store_true", help="wipe the ledger and start a new life")
    ap.add_argument("--think", action="store_true",
                    help="let the model reason before answering (slower, often overruns on small models)")
    ap.add_argument("--mock", action="store_true", help="no LLM, random decisions (to test the dashboard)")
    cfg = ap.parse_args()

    if not os.path.isabs(cfg.db):
        cfg.db = os.path.join(HERE, cfg.db)
    if cfg.reset:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(cfg.db + suffix):
                os.remove(cfg.db + suffix)
        print("Ledger wiped. New life.")

    if not cfg.mock:
        check_ollama(cfg.host, cfg.model)

    market = Market(cfg.market, cfg.symbol)
    market.warmup()
    init_db(cfg.db).close()

    server = ThreadingHTTPServer(("0.0.0.0", cfg.port), make_handler(cfg, market))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Dashboard: http://localhost:{cfg.port}")
    print(f"Bot: {cfg.bot_name} | model: {'mock' if cfg.mock else cfg.model} | market: {market.label} "
          f"| start ${cfg.start_cash:.2f} | a turn every {cfg.interval:g}s | Ctrl+C to stop\n")

    try:
        run_sim(cfg, market)
    except KeyboardInterrupt:
        print("\nStopped. The ledger is saved; run again to continue this life.")
    finally:
        STOP.set()
        server.shutdown()


if __name__ == "__main__":
    main()
