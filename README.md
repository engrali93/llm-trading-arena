# LLM Trading Arena

**Watch an AI try to survive with $50.**

A local language model (DeepSeek-R1 via [Ollama](https://ollama.com)) is handed $50 of fake money, a handful of rules and a live view of the crypto market. Nobody tells it what to do. Every couple of minutes it reads prices, news and market mood, then decides what to buy or sell. If its total value ever falls below $1, it dies. A live dashboard shows its money over time, what it holds, every trade, and its reasoning.

Everything runs on your own machine. No exchange account, no API keys, no paid services, no real orders.

![The live dashboard: total value against a BTC buy-and-hold benchmark, holdings and allocation](docs/screenshot/screenshot%201.png)

> **Disclaimer.** This is a simulation for learning and entertainment. It is **not financial advice**, it never places real orders, and nothing it "decides" says anything about how a real strategy would perform. Small language models are not good traders.

## Features

- **Free choice of coin.** The agent picks from the Binance top-40 USDT pairs (minus stablecoins, leveraged tokens and tokenized stocks) and can hold several coins at once.
- **Free news and mood as hints.** Headlines from CoinDesk, Cointelegraph and Decrypt, the Fear & Greed index, CoinGecko trending coins, and 24-hour history for coins it asks to watch.
- **A strict referee.** The model only asks. Python validates every order, applies the fee, and writes the ledger. The model can't invent money.
- **Live dashboard.** Money vs. a "BTC buy and hold" benchmark, holdings and allocation, trades, news, past lives, and a collapsible "Show reasoning" for every decision. Light and dark mode, works on a phone.
- **Settings and restart from the dashboard.** Change the starting amount and fee, restart, and keep every past life in the ledger.
- **It learns across lives.** When a life ends, the agent reflects on it and writes lessons. Later lives start with those lessons and a one-line summary of each past life, and the agent can restart itself after dying. Memory is a toggle, so you can compare runs with and without it.
- **Standard library only.** One Python file plus one HTML file. No pip installs.

## Screenshots

Every trade with the agent's reason, its latest thoughts with a collapsible "Show reasoning", past lives with report and CSV export links, and the settings panel:

![Trades table, the agent's thoughts, past lives and settings](docs/screenshot/screenshot%202.png)

The news and market-mood panel: Fear & Greed, CoinGecko trending coins and the latest headlines tagged by coin:

![News and market mood panel](docs/screenshot/screenshot%203.png)

## How it works

```
 Binance (prices, volume)      +-----------+   JSON orders    +-----------+   SQL    +-----------+
 CoinDesk / Cointelegraph  --> |   AGENT   | ---------------> |  REFEREE  | -------> |  LEDGER   |
 Decrypt (news)                | (Ollama + |  (asks only)     | (validate,|          | (SQLite)  |
 Fear & Greed, trending        | DeepSeek) | <--------------- |  fees,    |          +-----+-----+
 its own portfolio + notes     +-----------+  rejections and  |  death)   |                |
                                              notes next turn +-----------+                v
                                                                                    dashboard.html
                                                                                    (http://localhost:8001)
```

Each turn (every 120 s by default, measured start to start):

1. Refresh the market: all prices in one call, the volume ranking hourly, news every 15 minutes, mood every 30 to 60 minutes. Any source that fails shows as "(unavailable)"; the loop never stops.
2. Build a compact prompt: the rules, a one-line-per-coin market overview, top gainers and losers, mood, the newest headlines, the agent's watchlist, its portfolio, its last trades, its own notes, and last turn's problems.
3. Ask the model for a JSON reply: up to 3 `orders`, a `watch` list, a `reason` and a `note`.
4. The referee runs sells first, then buys, rejecting anything illegal.
5. Mark to market, charge any living cost, check for death, write everything to SQLite.
6. When a life ends (death or a manual restart), the agent reflects, its lessons are merged into its core wisdom, and the next life starts with that wisdom (see [Learning across lives](#learning-across-lives)).

## Requirements

- Python 3.9 or newer (standard library only)
- [Ollama](https://ollama.com/download)
- Internet access for the free market and news sources
- Hardware for the model you choose (see below)

## Setup

### 1. Python

| OS | Command |
|---|---|
| Windows | `winget install Python.Python.3.12` (or python.org) |
| macOS | `brew install python` (or python.org) |
| Linux | `sudo apt install python3` (or your distro's package manager) |

### 2. Ollama

Install from [ollama.com/download](https://ollama.com/download). On Linux: `curl -fsSL https://ollama.com/install.sh | sh`. Check it with `ollama --version`.

### 3. Pull a model that fits your hardware

| Hardware | Model |
|---|---|
| NVIDIA GPU with 12 GB+ VRAM, or Apple Silicon with 24 GB+ | `ollama pull deepseek-r1:14b` (default) |
| 8 GB VRAM (or Apple Silicon with 16 GB) | `ollama pull deepseek-r1:8b` and run with `--model deepseek-r1:8b` |
| Anything weaker | `ollama pull deepseek-r1:1.5b` and run with `--model deepseek-r1:1.5b` |

Any Ollama model works, e.g. `--model qwen3:8b` or `--model llama3.1:8b`.

**12 GB GPUs and the 14b:** the 14b takes about 10 GB with a 12288-token context, which is tight on a 12 GB card, so set these two Ollama environment variables and restart Ollama. The start scripts export them when they launch Ollama themselves, but if the Ollama app starts on its own you need them in your user environment:

| Variable | Value |
|---|---|
| `OLLAMA_FLASH_ATTENTION` | `1` |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` |

On Windows: `setx OLLAMA_FLASH_ATTENTION 1` and `setx OLLAMA_KV_CACHE_TYPE q8_0`, then quit and reopen Ollama. After the first turn, run `ollama ps`: the PROCESSOR column should say `100% GPU`. If part of the model is on the CPU, close GPU-hungry apps (browsers, video calls) or lower `--num-ctx` to 10240, then 8192.

## Running

| OS | Continue the same life | Start a brand-new life (wipes the ledger) |
|---|---|---|
| Windows | `start.bat` | `start-fresh.bat` |
| macOS / Linux | `./start.sh` | `./start.sh --reset` |

(On macOS/Linux run `chmod +x start.sh stop.sh` once if needed.) The scripts start Ollama if it isn't running. On Windows the agent opens in its **own console window titled "LLM Trading Arena"**, so you can see what it is doing and close it later; the launcher window closes by itself. On macOS/Linux the agent runs in the terminal you started it from.

### Stopping

| OS | Command |
|---|---|
| Windows | `stop.bat` (it runs `stop.ps1`) |
| macOS / Linux | `./stop.sh` |

The stop script finds every python process running `arena.py` and stops it **gracefully**: it asks the agent to shut down through its dashboard port (`POST /api/shutdown`, localhost only), and the agent finishes any ledger write in progress, folds the SQLite WAL file into the database and exits. It force-kills a process only if it is still running after about 10 seconds. It prints what it stopped, or "Agent is not running". Ollama is left running. Stopping never loses data: the next start continues the same life. `Ctrl+C` in the agent's window stops it the same graceful way, and so does a plain `kill` on macOS/Linux. (On Windows the extra `stop.ps1` file is only there because the PowerShell logic is easier to read than the same thing inside a batch file.)

Open **http://localhost:8001**.

**Phone or another computer:** by default the server listens on `127.0.0.1` only. Add `--lan` (`start.bat --lan` or `./start.sh --lan`) to listen on all interfaces, then open `http://<your-computer-ip>:8001` from a device on the same network. You may need to allow inbound TCP 8001 in your firewall. There is no login, so only use `--lan` on a network you trust.

## Dashboard settings and restart

The **Settings** panel has:

- **Starting amount ($):** 1 to 100000 (default 50)
- **Fee (%):** 0 to 5 (default 0.10), charged on every buy and every sell
- **Restart agent:** after an in-page confirmation, the current life is archived and a new one starts with these settings. The restart takes effect as soon as the turn in progress finishes.
- **Auto-restart after death** (default on) and **Learning (memory from past lives)** (default on): two toggles, described under [Learning across lives](#learning-across-lives).

Each life is kept in the ledger and listed under **Past lives** (model, start cash, fee, turns survived, final value, running / ended / died). A life uses one model for its whole run, so starting the arena with a different `--model` archives the current life and starts a new one. The active settings are saved in SQLite, so they survive restarting `arena.py`. The `--cash` and `--fee` flags only seed a brand-new ledger.

Restarts are accepted only from the same machine unless `--lan` is set, only as same-origin `application/json` requests, and the values are validated server-side.

## Learning across lives

A life ends when the agent dies (total value below the death line) or when you restart it. Then:

1. **Reflection.** The model gets a compact summary of that life: turns survived, start, peak and final value, how it ended, its 5 best and 5 worst closed trades with the reasons it gave, how the coins it bought performed, and its last notes to itself. It writes 5 to 10 short, concrete lessons as JSON (same schema-forced approach as the trading calls). They are saved in the `lessons` table (life, lesson, time) and kept forever.
2. **Core wisdom.** All earlier lessons plus the new ones are merged into at most 10 "core lessons": near-duplicates are removed, and if more than 10 remain the model picks the most useful. Each core lesson remembers which life taught it.
3. **Use.** Every new life's system prompt gets a block, kept under about 500 tokens: `WISDOM FROM YOUR PAST LIVES (you have died N times before)`, the core lessons, and one line per past life such as `Life 2: survived 14h, died at $0.98 after going all-in on PEPE` (built from the ledger, not written by the model). The block is saved with the life, so reports show what it started with.
4. **Auto-restart.** When the agent dies it reflects, waits 60 seconds (`--restart-delay`), then starts a new life with the same settings and model. Auto-restart stops when the ledger holds `--max-lives` lives (default 20), and you can switch it off any time. Manual restarts also trigger a reflection.
5. **Memory is a per-life switch.** `--learning on|off` and the dashboard toggle set whether the **next** life has memory; a life never changes it halfway, and the Past lives table shows it per life. With memory off a life gets no wisdom and no reflection runs, so "no memory" and "with memory" runs are comparable. Only past lives that had memory on count as "your past lives" in the prompt, and lives from before this feature are marked off.

The dashboard shows a **Lessons learned** panel (the current core lessons and which life taught each), the life number and death count in the header ("Life #4 · died 3 times"), a Memory column in Past lives, and a small chart of survival time per life. A banner shows when the agent is reflecting or waiting to restart.

The two toggles are saved in SQLite. Passing `--learning` or `--auto-restart` on the command line changes the saved value; leaving them out keeps whatever the dashboard last set (both default to on).

## Exports

Every life, running, ended or dead, can be exported without touching the running agent (exports are read-only and nothing is saved to disk):

- **Export report** (dashboard header, and a "Report" link per life under Past lives) opens `GET /api/report?run=<life id>` in a new tab: a printable A4 summary with the settings, outcome (alive or dead, turns, duration, final value, P/L, versus BTC buy and hold), statistics (trades, fees, win rate on closed trades, best and worst trade, max drawdown, peak value, most-traded coins), an SVG chart that prints without JavaScript, the final holdings, the five biggest per-turn swings with the agent's reason, its last 10 notes, and the full trade table. Use the **Print / Save as PDF** button.
- **Export data (CSV)** (and a "CSV" link per life) downloads `GET /api/export.zip?run=<life id>`, a ZIP built in memory named `arena_life<id>_<YYYYMMDD-HHMM>.zip` with `trades.csv`, `equity.csv`, `decisions.csv` (including the reasoning text), `positions.csv`, `lessons.csv` (the lessons that life taught), `news.csv` (headlines from that life's time range) and `summary.json` (the same statistics as the report, plus whether memory was on and the wisdom the life started with). The report has a "Memory and lessons" section.

**Export all lives** (dashboard header) covers the whole ledger in one go:

- `GET /api/report/all` is one printable A4 report with the totals (lives, deaths, total runtime, turns, trades, fees), the best, worst and longest-surviving life, a chart of survival time and final value (as a percentage of start cash, so lives with different start cash compare fairly) per life, a table of every life (model, start cash and fee, memory on or off, turns, survival time, peak and final value, cause of death), a memory-on versus memory-off comparison, and **how the core lessons evolved** over time (what each reflection added and dropped, and which life wrote each lesson).
- `GET /api/export_all.zip` (the "All lives (CSV)" button) is one ZIP named `arena_all_lives_<YYYYMMDD-HHMM>.zip` with `lives.csv` (one row per life), `trades.csv`, `equity.csv`, `decisions.csv`, `positions.csv`, `lessons.csv`, `core_history.csv` (every snapshot of the core lessons), `news.csv` and `summary.json`. Every table has a `life_id` column first.

The history of the core lessons is recorded from the moment this feature exists: each reflection stores a snapshot of the merged list in the `core_history` table. Both all-lives exports are read-only, follow the same localhost-only rule and the same CSV rules as the single-life exports, and accept `&sep=comma` / `&sep=semicolon` for the ZIP.

Without `run=`, the current life is exported. Like restarts, exports are only served to the same machine unless you start with `--lan`.

CSV details: UTF-8 with a byte-order mark, so Excel shows umlauts and emoji correctly, and ISO timestamps. `--csv-delimiter auto` (the default) follows your regional settings: if your system uses a decimal comma, the CSVs use `;` as the column separator and decimal commas, which is what Excel expects there; otherwise they use `,` and decimal points. Force a style with `--csv-delimiter comma` or `semicolon`, or per request with `&sep=comma` / `&sep=semicolon`. Text that starts with `=`, `+`, `-` or `@` gets a leading `'` so a spreadsheet never runs it as a formula.

## Command-line flags

| Flag | Default | What it does |
|---|---|---|
| `--model` | `deepseek-r1:14b` | Ollama model name. A life never mixes models: starting with a different one archives the current life and begins a new one with the same settings |
| `--num-ctx` | `12288` | Ollama context window in tokens. About 6k is enough for the prompt plus reasoning; 12288 keeps the 14b fully on a 12 GB GPU. Lower it if `ollama ps` shows CPU |
| `--interval` | `120` | Seconds from the start of one turn to the start of the next. A slower turn starts the next one immediately |
| `--cash` | `50` | Starting cash. Only seeds a brand-new ledger |
| `--fee` | `0.10` | Fee in percent on every buy and sell. Only seeds a brand-new ledger |
| `--death` | `1` | The bot dies if its total value falls below this |
| `--living-cost` | `0` | Dollars charged every turn just for being alive |
| `--min-volume` | `50000000` | 24h quote volume (USD) a coin needs before it can be bought |
| `--market` | `binance` | `binance`, or `random` (40 fake random-walk coins, works offline) |
| `--port` | `8001` | Dashboard port |
| `--lan` | off | Listen on all interfaces and accept restarts from the network |
| `--db` | `arena.db` | SQLite ledger file, relative to the script folder |
| `--csv-delimiter` | `auto` | CSV export style: `auto` (follows your regional number format), `comma` or `semicolon` |
| `--name` | `DeepSeek` | Label shown for the bot |
| `--host` | `http://localhost:11434` | Ollama URL |
| `--learning` | `on` (saved) | `on` or `off`: whether the next life uses cross-life memory. Only changes the saved setting when passed; the dashboard toggle edits the same setting |
| `--auto-restart` | `on` (saved) | `on` or `off`: after a death, reflect, wait, and start a new life with the same settings and model |
| `--max-lives` | `20` | Auto-restart stops once the ledger holds this many lives |
| `--restart-delay` | `60` | Seconds to wait before an auto-restart |
| `--think-budget` | `2000` | Max reasoning tokens before the answer is forced into the JSON schema (0 skips reasoning) |
| `--mock` | off | No model: random multi-coin orders, for testing the dashboard |
| `--reset` | off | Wipe the ledger and start over |

## Data sources (all free, no keys)

| Data | Source | Refresh |
|---|---|---|
| Prices, 24h change and volume | Binance public API (`/api/v3/ticker/price`, `/ticker/24hr`, `/klines`, `/exchangeInfo`) | prices every turn, ranking hourly |
| Fear & Greed index | alternative.me | 1 hour |
| Trending coins | CoinGecko | 30 minutes |
| News | RSS from CoinDesk, Cointelegraph and Decrypt | 15 minutes |

If Binance is unreachable at startup, the arena falls back to a random-walk market so the dashboard still works.

## Coin rules

- **Universe:** the top 40 USDT pairs by 24-hour quote volume, rebuilt hourly. The startup log prints the final list.
- **Excluded:** stablecoins, leveraged tokens, and tokenized stocks and ETFs. Binance tags the tokenized ones in `exchangeInfo` with a permission group (`TRD_GRP_261` at the time of writing), which the arena uses as its main filter.
- **Minimum volume:** coins under `--min-volume` still appear in the overview, marked "(low volume, can't buy)", and buys of them are rejected.
- **`exclude.txt`:** coins the agent may never buy, one symbol or base asset per line (`#` for comments). It is re-read hourly. Use it as a backup for the tokenized-stock filter or to ban anything else.
- **Selling is always allowed.** The agent can sell any coin it owns, even one that is excluded, low-volume or has dropped out of the top 40.
- **Referee rules:** up to 3 orders per turn, sells first. Unknown symbols and sells of coins it doesn't own are rejected. A buy larger than cash is shrunk to what cash allows (fee included) and only rejected if that's under $1. Rejections and shrinks are logged and shown to the agent next turn.

## Reasoning-model notes (DeepSeek-R1)

R1-style models think out loud before answering, and an 8B distill often rambles for thousands of tokens without reaching an answer. The arena handles this in two stages:

1. `/api/chat` with `think: true` and a JSON-schema `format`, capped at `--think-budget` reasoning tokens. The reasoning arrives separately in `message.thinking`.
2. If no answer arrived, the reasoning so far is fed back (think block closed) to `/api/generate` with the same schema `format`, which forces valid JSON.

Other notes:

- `num_ctx` is set explicitly (`--num-ctx`, default 12288). Ollama's default context size can be huge and run out of memory, and a context that is too small makes the model lose its instructions.
- The 14b usually finishes its reasoning within `--think-budget`, so the stage-2 fallback is rare. The 8b needed it on almost every turn. In a small test (5 turns each) the 14b averaged about 33 s per turn after loading, against about 51 s for the 8b, and made fewer invalid orders.
- `think: false` is ignored by some R1 builds, which is why the arena doesn't rely on it.
- If a reply still doesn't parse, the arena retries once, then holds and logs the error.
- The trimmed reasoning (about 1500 characters) is saved with each decision and shown on the dashboard.

## Project structure

```
arena.py          simulation loop, referee and web server (one process)
dashboard.html    the live dashboard (Chart.js from a CDN)
exclude.txt       coins the agent may never buy
start.bat         Windows launcher, opens the agent in its own window (continues the same life)
start-fresh.bat   Windows launcher (wipes the ledger)
start.sh          macOS / Linux launcher
stop.bat          Windows: stops the agent gracefully (calls stop.ps1)
stop.ps1          the PowerShell that stop.bat runs
stop.sh           macOS / Linux: stops the agent gracefully
skill/SKILL.md    a from-scratch build guide for this project
legacy/           the original single-coin (BTC only) version
docs/screenshot/  dashboard screenshots used in this README
LICENSE           MIT
arena.db          your ledger (created on first run, git-ignored)
```

HTTP endpoints: `GET /` (dashboard), `GET /api/state`, `GET /api/runs`, `POST /api/restart`, `POST /api/settings` (the two toggles), `GET /api/report?run=<id>`, `GET /api/export.zip?run=<id>`, `GET /api/report/all`, `GET /api/export_all.zip`, `POST /api/shutdown` (used by the stop scripts). The POSTs follow the same rules: same-origin, `application/json`, small body, and localhost only unless `--lan`. `/api/shutdown` is localhost only even with `--lan`.

## Build your own

`skill/SKILL.md` is a step-by-step guide, written for an AI coding assistant or a human, for rebuilding this project from scratch and extending it: architecture rules, the prompt, the schema, the referee, and the gotchas.

## Ideas

- **A second fighter.** Run another copy with a different `--name`, `--model`, `--db` and `--port` (say DeepSeek vs. Qwen). Give both the same data, prompt and fees, or the comparison isn't fair.
- **Living cost.** `--living-cost 0.05` charges every turn, so the agent has to earn to survive.
- **Does memory help?** Run a few lives with `--learning off`, then a few with it on (same model, cash and fee), and compare the Survival chart, final values and the lessons it wrote. On a fresh ledger (`--reset`), `--cash 3 --fee 5 --death 2.5` makes lives end quickly.
- **Testnet.** If you ever want real order flow, start with the Binance Spot Testnet only, with trade-only keys, no withdrawals, and hard limits in code (max per trade, daily cap, drawdown stop, kill switch).
- **Other markets.** Swap the Binance universe for another free data source.

## License

MIT. See [LICENSE](LICENSE).
