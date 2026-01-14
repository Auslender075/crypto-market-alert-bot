import os
import time
import json
import requests

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
STATE_FILE = "state.json"

VS_CURRENCY = "usd"
TOP_N = 250

# Alert thresholds
OUTPERFORM_BTC_24H = 8.0
MIN_24H_MOVE = 10.0
MIN_VOL_USD = 30_000_000
COOLDOWN_SECONDS = 6 * 3600  # 6 hours

def send_discord(msg: str):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL env var")
    requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=20)

def fetch_markets():
    params = {
        "vs_currency": VS_CURRENCY,
        "order": "market_cap_desc",
        "per_page": TOP_N,
        "page": 1,
        "price_change_percentage": "1h,24h,7d",
    }
    r = requests.get(COINGECKO_MARKETS_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"recent_alerts": {}, "last_startup_sent": False}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

def fmt_int(n):
    try:
        return f"{int(n):,}"
    except Exception:
        return "n/a"

def run_once():
    state = load_state()
    recent_alerts = state.get("recent_alerts", {})

    data = fetch_markets()
    btc = next((c for c in data if c["id"] == "bitcoin"), None)
    if not btc:
        raise RuntimeError("BTC not found in market data")

    btc_24h = btc.get("price_change_percentage_24h_in_currency") or 0.0

    now = int(time.time())
    alerts_sent = 0

    for c in data:
        c24 = c.get("price_change_percentage_24h_in_currency")
        c1h = c.get("price_change_percentage_1h_in_currency")
        vol = c.get("total_volume") or 0
        rank = c.get("market_cap_rank")
        coin_id = c.get("id")

        if c24 is None or coin_id is None:
            continue
        if vol < MIN_VOL_USD:
            continue
        if c24 < MIN_24H_MOVE:
            continue

        outperf = c24 - btc_24h
        if outperf < OUTPERFORM_BTC_24H:
            continue

        last = int(recent_alerts.get(coin_id, 0))
        if (now - last) < COOLDOWN_SECONDS:
            continue

        name = c.get("name", coin_id)
        sym = (c.get("symbol") or "").upper()
        link = f"https://www.coingecko.com/en/coins/{coin_id}"

        msg = (
            f"🚨 **[Rotation Alert]**\n"
            f"Coin: **{name} ({sym})** | Rank: #{rank}\n"
            f"Move: **{c24:.1f}% (24h)**, {c1h:.1f}% (1h)\n"
            f"BTC: {btc_24h:.1f}% (24h) → Outperformance: **{outperf:.1f}%**\n"
            f"Volume: **${fmt_int(vol)}**\n"
            f"Link: {link}"
        )
        send_discord(msg)
        recent_alerts[coin_id] = now
        alerts_sent += 1

    state["r]()
