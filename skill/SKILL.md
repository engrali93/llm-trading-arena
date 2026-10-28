---
name: llm-trading-arena
description: Build a local AI trading simulation where an open-source LLM (via Ollama) gets fake money, picks any crypto coin, reads free news and market mood, and trades until it grows or dies, with a SQLite ledger and a live web dashboard. Use when someone wants to create, rebuild or extend an autonomous LLM trading agent, AI trading arena, or "AI survival" trading experiment.
---

# LLM Trading Arena: build your own autonomous AI trader

This guide builds a self-running experiment. A local LLM gets **$50 of fake money**, sees real crypto prices, news and market mood, and decides every trade by itself until it grows its money or "dies". You watch it live on a dashboard.

No exchange account, no API keys and no paid services are needed. Everything runs on your own machine.

> This is a simulation for learning and entertainment. It is **not** financial advice and it does **not** place real orders.

## What you end up with

| Part | What it does |
|---|---|
| `arena.py` | One Python process: the simulation loop, the referee, and the web server. Standard library only |
| `dashboard.html` | A live page: money chart, holdings, trades, news, thoughts, settings, past lives |
| `arena.db` | SQLite ledger (created on first run): every trade, decision, equity snapshot and news item |
| `exclude.txt` | Symbols the agent may never buy (editable, re-read hourly) |
| `start.bat` / `start-fresh.bat` / `start.sh` | One-click start (continue the same life / wipe and start over) |

## Ask the user first (use defaults if they don't care)

| Setting | Default |
|---|---|
| Project folder | `./llm-trading-arena` |
| Dashboard port | `8001` |
| Starting cash | `$50` (1 to 100000) |
| Fee per buy and per sell | `0.10%` (0 to 5) |
| Death line | total value below `$1` |
| Model | `deepseek-r1:8b` (or `deepseek-r1:1.5b` on weak hardware) |
| Turn interval | `120` s, timed start to start |
| Minimum 24h volume to buy | `$50,000,000` |

## Step 1: Environment

1. **Python 3.9+.** Check with `python --version` (`python3` on Mac/Linux). Install from python.org, `winget install Python.Python.3.12`, or `brew install python`. Use the **standard library only**: no pip installs.
2. **Ollama.** Check with `ollama --version` and that `http://localhost:11434/api/tags` responds. Install from ollama.com if missing.
3. **Pick the model by hardware.**
   - NVIDIA GPU with 8 GB+ VRAM, or Apple Silicon with 16 GB+ -> `ollama pull deepseek-r1:8b`
   - Otherwise -> `ollama pull deepseek-r1:1.5b`
   - Test it: `ollama run <model> "reply with OK"`
4. Any Ollama model works. Non-reasoning models (e.g. `llama3.1:8b`) are faster and follow JSON more reliably, but they won't produce a reasoning trace for the dashboard.

## Step 2: Architecture (follow these rules)

### The golden rule: the model asks, the referee decides
The LLM **never** edits its own balance. It only returns requested orders. Python code checks them, applies fees, and writes the ledger. If the model keeps its own books, it will hallucinate money within a few turns.

### The turn loop
```
every turn (start-to-start; no sleep if the turn overran):
  1. apply a queued restart, if any (archive the life, start a new one)
  2. refresh market data (one call for all prices) and, when due, news and mood
  3. build the prompt: rules + market + mood + news + watchlist + portfolio + memory
  4. ask the model -> JSON (retry once on a parse failure, then hold)
  5. referee validates and executes the orders (sells first, then buys)
  6. charge any living cost, mark to market, check for death
  7. write trades / equity / decisions / positions to SQLite
```
If a restart arrives while the model is thinking, drop that turn's decision (never half-apply it) and restart at the top of the loop.

### Market data (all free, no keys)
| Source | Endpoint | Refresh |
|---|---|---|
| 24h tickers (universe, % change, volume) | `https://api.binance.com/api/v3/ticker/24hr` | stats every 15 min, universe rebuilt hourly |
| Tokenized-stock flag | `https://api.binance.com/api/v3/exchangeInfo?symbolStatus=TRADING` | with each universe rebuild |
| All current prices in one call | `https://api.binance.com/api/v3/ticker/price` | every turn |
| Watchlist history | `/api/v3/klines?symbol=X&interval=1h&limit=24` | cached 10 min, max 5 coins |
| Fear & Greed | `https://api.alternative.me/fng/` | 1 h |
| Trending coins | `https://api.coingecko.com/api/v3/search/trending` | 30 min |
| News (RSS, parse with `xml.etree`) | `https://www.coindesk.com/arc/outboundfeeds/rss/`, `https://cointelegraph.com/rss`, `https://decrypt.co/feed` | 15 min |

Send a `User-Agent` header on every request. If any source fails, show `(unavailable)` for that section and keep going. **Never crash the loop.** If Binance is unreachable at startup, fall back to a random-walk market of 40 fake coins (also offered as `--market random`).

### Coin universe
- The top 40 USDT pairs by `quoteVolume`.
- Exclude **stablecoins** (USDC, FDUSD, TUSD, DAI, USDP, EUR, and anything whose base asset starts or ends with `USD`).
- Exclude **leveraged tokens** by regex on the *base asset*: `^[A-Z0-9]{3,}(UP|DOWN)$`, plus any base containing `BULL` or `BEAR`. Don't use "contains UP", or `JUP` gets excluded wrongly.
- Exclude **tokenized stocks/ETFs** (tickers like `MSTRB`, `CRCLB`). Binance marks them in `exchangeInfo` with an extra permission group in each symbol's `permissionSets` (`TRD_GRP_261` at the time of writing; verify it, since it can change). Keep `exclude.txt` as a manual backup and for anything else you want banned.
- **Minimum volume to buy** (`--min-volume`, default $50M of 24h quote volume). Low-volume coins still appear in the overview, marked "(low volume, can't buy)".
- Coins that drop out of the universe, are excluded or are low-volume can always be **sold**, never bought.
- Print the final universe once at startup.

### Prompt (aim for under ~4k tokens)
- **System prompt:** the rules of the world (starting cash, fee, death line, min volume, max 3 orders per turn, that oversized buys are shrunk, that rejections come back next turn) and the goal "stay alive and grow your money". No strategy hints. Regenerate it each turn, because cash and fee can change on a restart.
- **Turn prompt:** a compact market overview (one line per coin: `SOL 142.3 +5.2% vol $1.2B`), the top 5 gainers and losers, Fear & Greed, trending coins, the 10 newest headlines with age and coin tags, the watchlist history, the portfolio (cash, positions with avg cost and P/L), its last 5 trades, its last 8 notes-to-self, and **last turn's rejections and notes**, so it learns. End with "keep your thinking brief, then answer with JSON only".

### Reply format (enforce it with a JSON schema)
```json
{"orders": [{"action": "buy", "symbol": "SOLUSDT", "usd": 10},
            {"action": "sell", "symbol": "BTCUSDT", "sell_pct": 50}],
 "watch": ["SOLUSDT"],
 "reason": "one short sentence",
 "note": "optional memo to future self"}
```
Schema: `orders` (array, max 3; each has `action` enum buy/sell, `symbol`, `usd`, `sell_pct`; `action` and `symbol` required), `watch` (array of strings, max 5), `reason` (string, max 200 chars), `note` (string, max 200 chars). Required: `orders`, `reason`. An empty `orders` list means hold. Tolerate the older single-action shape (`{"action": "hold"}`) when parsing.

### Referee rules
- Sells run first, then buys, so sale proceeds can fund buys in the same turn. Consider at most 3 orders.
- Unknown symbol -> reject. A buy of an excluded, out-of-universe or low-volume coin -> reject, with a message that says which.
- A buy under $1 -> reject.
- A buy larger than cash -> **shrink** it to `cash / (1 + fee)` and log a `NOTE:`. Reject only if the shrunk amount is under $1.
- Selling a coin it doesn't own -> reject. `sell_pct` defaults to 100 when omitted; reject if it is 0 or negative. Selling 100% closes the position.
- Fee = `fee_rate x trade value` on **both** buys and sells. Cost basis (`avg_cost`) includes buy fees.
- Log every rejection (`REJECTED: ...`) and shrink (`NOTE: ...`) in `decisions.error`, and show it to the agent next turn.
- **Death:** if total value (cash + positions at market) drops below the death line, mark the life dead and stop trading. The dashboard keeps running and the user can restart from the settings panel.

### SQLite schema (WAL mode)
One row per **life** in `bots`, so a restart archives the old life instead of deleting it. Every other table keys on `bot_id`.
```
settings(key PRIMARY KEY, value)        -- start_cash, fee_pct, current_bot_id
bots(id, name, model, cash, alive, started, died, ticks,
     start_cash, fee_rate, ended, final_value)   -- fee_rate is a fraction (0.001 = 0.10%)
positions(bot_id, symbol, qty, avg_cost, last_price, PRIMARY KEY(bot_id, symbol))
trades(id, bot_id, ts, turn, action, symbol, qty, price, usd, fee, cash_after, qty_after, reason)
equity(id, bot_id, ts, btc_price, cash, positions_value, equity)      -- one row per turn
decisions(id, bot_id, ts, turn, action, orders, watch, reason, note, error,
          prompt_tokens, eval_tokens, thinking, duration)
news(id, link UNIQUE, title, source, published, tags, fetched)
```
Migrations: when an older single-life ledger has no `start_cash` column on `bots`, rebuild the table (SQLite can't drop its `UNIQUE(name)`), turn the existing row into life #1, and read its start cash back from its first `equity` row. Add new columns with `ALTER TABLE` and never wipe a ledger unless the user passes `--reset`.

### Settings, lives and restart
- The active `start_cash` and `fee_pct` live in `settings`. CLI `--cash` and `--fee` only seed a brand-new ledger; afterwards the saved values win.
- A restart sets `bots.ended` and `final_value` on the old life, inserts a new `bots` row with the new settings, stores the settings, and points `current_bot_id` at it. Turn numbers start from 1 again.
- Keep the restart request in a lock-protected variable and wake the between-turns sleep with an `Event`, so a restart doesn't wait out the whole interval.

### Web server
- `http.server.ThreadingHTTPServer`. Bind to **`127.0.0.1` by default**; the `--lan` flag binds `0.0.0.0` for phone access.
- `GET /` serves `dashboard.html`. `GET /api/state` returns the active life, holdings, equity series, last 200 trades, last 30 decisions, news, mood, active config and saved settings. `GET /api/runs` lists every life.
- `GET /api/report?run=<id>` returns a standalone, printable A4 HTML report for one life, and `GET /api/export.zip?run=<id>` a ZIP of CSVs plus `summary.json` (see "Exports" below). Both default to the current life, are read-only (open a separate SQLite connection with `PRAGMA query_only=ON`), work for alive, ended and dead lives, handle a life with no trades, and follow the same localhost-only rule as restart unless `--lan` is set.
- `POST /api/restart` takes `{"start_cash": 100, "fee_pct": 0.2}` and replies `202` (the restart is applied when the current turn finishes). Validate server-side: both must be real numbers (not strings or booleans), `start_cash` 1 to 100000, `fee_pct` 0 to 5. Harden it:
  - accept it only from loopback addresses unless `--lan` is set (and check the `Host` header to resist DNS rebinding),
  - require `Content-Type: application/json` (so a web page on another site can't send it without a preflight, which the server never answers),
  - reject any `Origin` that doesn't match the `Host`,
  - limit the body to 1 KB.
- Print console text as ASCII or call `sys.stdout.reconfigure(errors="replace")`. A fancy character on a legacy Windows code page will crash the loop at the worst moment.

### Dashboard (refresh every 5 s, light + dark mode, mobile-friendly, Chart.js from cdnjs)
- Header: bot name, model, ALIVE/DEAD badge, and the active settings ("Start $50 · Fee 0.10%").
- Cards: total value, P/L, cash, coins held, Fear & Greed, turns survived with average turn time.
- Chart: total value vs the starting-cash line vs "BTC buy & hold", with buy/sell dots.
- Holdings table plus an allocation doughnut (including cash). Trade table with a coin column.
- Thoughts feed: reason, orders asked, watchlist, notes, rejections, and a collapsible "Show reasoning".
- News & mood panel (Fear & Greed, trending, headlines with links and coin tags).
- **Past lives** table: start cash, fee, turns, final value, status (running / ended / died), and Report / CSV export links per life. The header has "Export report" and "Export data (CSV)" buttons for the current life.
- **Settings panel:** starting amount, fee %, and a **Restart agent** button with an in-page confirmation modal (not `window.confirm`; keyboard accessible, Escape closes it). Don't overwrite inputs while the user is typing.
- Escape every string that came from the model or a news feed before putting it in HTML, and only link `http(s)` URLs.

### Exports
- **Report** (`/api/report`): title, model and generated time; settings (start cash, fee, death line, interval, min volume); outcome (alive or dead with time, turns, duration, final value, P/L in $ and %, versus BTC buy and hold over the same period); stats (trades, buys, sells, fees, win rate on closed trades, best and worst trade, max drawdown, peak value, most-traded coins); an **inline SVG** chart generated in Python (total value vs start line vs BTC buy and hold, with different line styles so it reads in black and white; no JS or CDN, so it prints reliably); final holdings; the 5 biggest per-turn swings in total value with the agent's reason at that turn; the last 10 notes; the full trade table; and a "simulation, not financial advice" footer. Print CSS: `@page { size: A4 }`, repeating table headers, `break-inside: avoid` on rows and boxes, a page break before the trade table, and a "Print / Save as PDF" button hidden with `@media print`. Escape every model-written string.
- **How the stats are derived:** replay the trade log per coin to get average cost (buy fees included). Score each SELL as proceeds minus sell fee minus cost, and count it a win if positive. Max drawdown is the worst peak-to-trough drop in the per-turn equity series. Store each position's last price (`positions.last_price`) every turn and when a life is archived, so a finished life's holdings can be valued later.
- **ZIP** (`/api/export.zip`): build it in memory with `zipfile`. Contents: `trades.csv`, `equity.csv`, `decisions.csv` (include the stored reasoning text), `positions.csv`, `news.csv` (headlines published during that life) and `summary.json`. Name it `arena_life<id>_<YYYYMMDD-HHMM>.zip`.
- **CSV rules for Excel:** write UTF-8 **with a BOM** and ISO timestamps. Excel decides the column separator from the system's decimal separator: with a decimal comma it expects `;` and decimal commas, and a comma-separated file opens as one column. So detect the decimal separator (on Windows `GetLocaleInfoEx(None, LOCALE_SDECIMAL=0x0E, ...)`; elsewhere guess from `LC_NUMERIC`/`LANG`) and offer an override flag. Prefix any text cell starting with `=`, `+`, `-` or `@` with `'`, because model- and feed-written text must never run as a spreadsheet formula.

## Step 3: Reasoning models (DeepSeek-R1) need special care

R1 distills love to overthink: 4,000 to 9,000+ tokens per turn, sometimes never reaching an answer. `think: false` is ignored by some builds. What works (Ollama accepts `format` and `think: true` together):

1. **Context:** set `num_ctx` to at least the prompt plus the reasoning budget (8192 works for a ~4k prompt and a 2000-token budget). An overflowing context makes the model forget its instructions.
2. **Stage 1:** `/api/chat` with `think: true`, `format` = the schema, `temperature` 0.7, and `num_predict` = the reasoning budget (default 2000, flag `--think-budget`). Reasoning arrives in `message.thinking`, the answer in `message.content`. If `content` has valid JSON, you're done.
3. **Stage 2 (the usual case for 8B):** if there is no content, call `/api/generate` with `raw: true`, a prompt in the model's chat template ending with the reasoning so far inside a closed think block (`<think>...reasoning...</think>`), and the same schema as `format`. The schema forces a valid JSON answer. (DeepSeek-R1 template: begin-of-sentence token, system text, `<｜User｜>` user text, `<｜Assistant｜><think>`...)
4. Strip any `<think>...</think>` from the content as a fallback and take the **last** valid JSON object (scan balanced braces; a simple `{[^{}]*}` regex fails on nested orders).
5. Retry once. After that, hold and log the error.
6. Save the trimmed thinking (about 1500 characters) per decision for the dashboard. It's the most entertaining part.
7. `--think-budget 0` skips the reasoning phase entirely (an empty think block plus the schema): fastest, least interesting.

## Step 4: Verify (don't skip)

1. **Mock mode** (`--mock --interval 2`, random multi-coin orders) for 30 s. Check that `/api/state` returns positions, trades with symbols, news rows and mood data, and that the page loads with no console errors. Make the mock force at least one low-volume rejection, one auto-shrink and one exclude-list block.
2. **Restart from the UI** (or `POST /api/restart`) with different settings, e.g. $100 and 0.2%. Check that the old life is archived and listed under Past lives, and the new life uses the new cash and fee (trade `fee / usd` should equal the new rate).
3. **Validation and security:** out-of-range and non-numeric values give `400`; a wrong content type gives `415`; a foreign `Origin` or `Host` gives `403`.
4. **Persistence:** stop and start `arena.py` (even with different `--cash`/`--fee` flags); the saved settings and the same life must come back.
5. **Exports:** download the report and ZIP for the current life, a finished life and an empty (0-trade) life. Print the report to PDF (headless Edge or Chrome: `--headless=new --no-pdf-header-footer --print-to-pdf=out.pdf file.html`) and check the page size is A4, no table is cut off, and the chart is drawn. Open the CSVs in real Excel and check the columns, numbers and special characters (umlauts, emoji).
6. **Real model** for at least 5 turns: 0 parse failures, reasoning visible, no excluded coins bought.

## Step 5: Make it shareable (before any public git push)

- `.gitignore`: `*.db`, `*.db-wal`, `*.db-shm`, `*.log`, `*.err`, `*.out`, `__pycache__/`, `.env`, `*_backup.*`, editor folders, local run logs.
- No absolute paths, usernames, local IPs, emails or credentials in code, README, scripts or comments. Use paths relative to the script (`os.path.dirname(os.path.abspath(__file__))`, `cd /d "%~dp0"`).
- Before committing, scan every staged file for usernames, drive-letter paths, home-directory paths, private IP addresses, email addresses and anything key-like.
- Commit under a private address (GitHub's `noreply` email) rather than a personal one: `git config user.email`.
- Add a LICENSE (MIT is a common choice) and a README with a clear "simulation, not financial advice" disclaimer.

## Gotchas collected the hard way

- Ollama may try to allocate a huge default context and run out of memory; always pass `num_ctx`.
- A reasoning model's thinking tokens count against `num_predict`; a low cap yields empty `content`.
- Binance's volume ranking is dominated by oddities (tokenized stocks, stablecoins) unless you filter them.
- The model will happily try to spend its last dollar; shrink the order instead of rejecting it.
- Small models churn: many tiny $1 trades that only pay fees. A higher fee, `--living-cost` or a smaller order cap changes that behaviour; the guide's defaults leave it alone on purpose.

## Ideas to extend

- **Second fighter:** run a second instance with a different `--name`, `--model`, `--db` and `--port` (e.g. DeepSeek vs Qwen). Give both the same data, prompt and fees, or the comparison isn't fair.
- **Living cost:** charge $X per turn so it must earn or die.
- **Real trading:** only via the **Binance Spot Testnet** first. Use trade-only API keys, **never enable withdrawals**, and enforce hard limits in code (max per trade, max per day, a drawdown stop, a kill switch).
