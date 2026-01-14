import os
import time
import json
import requests

# -----------------------------
# Config
# -----------------------------
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
STATE_FILE = "state.json"

VS_CURRENCY = "usd"
TOP_N = 250

# Only alert on sufficiently large/liquid coins (cleaner signals)
MAX_RANK = 200

# --- Tier 1 (Strong Rotation / Breakout) ---
T1_MIN_24H_MOVE = 10.0
T1_OUTPERFORM_BTC_24H = 8.0
T1_MIN_VOL_USD = 30_000_000

# --- Tier 2 (Early Build) ---
T2_MIN_24H_MOVE = 5.0
T2_OUTPERFORM_BTC_24H = 3.0
T2_MIN_VOL_USD = 20_000_000

# --- Short-term persistence window (10-minute cadence) ---
# 6 samples = 60 minutes if the workflow runs every 10 minutes
SAMPLES_PER_HOUR = 6

# "Mostly up" steps in the last hour
MIN_UP_STEPS_T2 = 4  # early build: up in at least 4 of last 6 steps
MIN_UP_STEPS_T1 = 5  # strong rotation: up in at least 5 of last 6 steps

# Minimum move over the last hour
MIN_1H_MOVE_EARLY = 1.5
MIN_1H_MOVE_STRONG = 2.5

# Cooldowns (avoid spam)
COOLDOWN_T1 = 6 * 3600
COOLDOWN_T2 = 3 * 3600


# -----------------------------
# Helpers
# -----------------------------
def send_discord(msg: str):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL secret/env var")
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
        return {"recent_alerts_t1": {}, "recent_alerts_t2": {}, "history": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def fmt_int(n):
    try:
        return f"{int(n):,}"
    except Exception:
        return "n/a"


def update_history(history: dict, coin_id: str, now: int, price: float):
    coin_hist = history.get(coin_id, [])
    coin_hist.append({"t": now, "p": price})

    # keep only last N samples (rolling window)
    if len(coin_hist) > SAMPLES_PER_HOUR:
        coin_hist = coin_hist[-SAMPLES_PER_HOUR:]

    history[coin_id] = coin_hist


def compute_persistence(history: dict, coin_id: str):
    """
    Returns:
      up_steps: number of times price increased vs previous sample
      steps: number of comparisons (len(prices)-1)
      pct_change_over_window: % change from first to last sample
    """
    coin_hist = history.get(coin_id, [])
    if len(coin_hist) < 3:
        return 0, max(len(coin_hist) - 1, 0), 0.0

    prices = [x["p"] for x in coin_hist]
    up_steps = 0
    for i in range(1, len(prices)):
        if prices[i] > prices[i - 1]:
            up_steps += 1

    first = prices[0]
    last = prices[-1]
    pct = 0.0 if first <= 0 else ((last - first) / first) * 100.0

    return up_steps, len(prices) - 1, pct


# -----------------------------
# Main run (single pass)
# -----------------------------
def run_once():
    state = load_state()
    hist = state.get("history", {})
    recent_t1 = state.get("recent_alerts_t1", {})
    recent_t2 = state.get("recent_alerts_t2", {})

    data = fetch_markets()
    btc = next((c for c in data if c.get("id") == "bitcoin"), None)
    if not btc:
        raise RuntimeError("BTC not found")

    btc_24h = btc.get("price_change_percentage_24h_in_currency") or 0.0
    now = int(time.time())

    # 1) Update history for all coins
    for c in data:
        coin_id = c.get("id")
        price = c.get("current_price")
        if coin_id and price is not None:
            update_history(hist, coin_id, now, float(price))

    alerts_sent = 0

    # 2) Evaluate alerts
    for c in data:
        coin_id = c.get("id")
        if not coin_id:
            continue

        name = c.get("name", coin_id)
        sym = (c.get("symbol") or "").upper()
        rank = c.get("market_cap_rank")
        vol = c.get("total_volume") or 0
        c24 = c.get("price_change_percentage_24h_in_currency")
        c1h = c.get("price_change_percentage_1h_in_currency")
        price = c.get("current_price")

        # rank filter (cleaner signals)
        if rank is None or rank > MAX_RANK:
            continue

        if c24 is None or price is None:
            continue

        outperf = c24 - btc_24h
        up_steps, steps, hour_move = compute_persistence(hist, coin_id)

        link = f"https://www.coingecko.com/en/coins/{coin_id}"

        # --- Tier 1: Strong Rotation (higher confidence) ---
        last_t1 = int(recent_t1.get(coin_id, 0))
        t1_ok = (
            vol >= T1_MIN_VOL_USD and
            c24 >= T1_MIN_24H_MOVE and
            outperf >= T1_OUTPERFORM_BTC_24H and
            steps >= 4 and  # at least ~40 minutes of data at 10-min cadence
            up_steps >= MIN_UP_STEPS_T1 and
            hour_move >= MIN_1H_MOVE_STRONG and
            (now - last_t1) >= COOLDOWN_T1
        )

        if t1_ok:
            msg = (
                f"🚨 **[Rotation Alert — Tier 1]**\n"
                f"Coin: **{name} ({sym})** | Rank: #{rank}\n"
                f"Move: **{c24:.1f}% (24h)**, {c1h:.1f}% (1h)\n"
                f"BTC: {btc_24h:.1f}% (24h) → Outperformance: **{outperf:.1f}%**\n"
                f"1h persistence: {up_steps}/{steps} up-steps | 1h move: **{hour_move:.2f}%**\n"
                f"Volume: **${fmt_int(vol)}**\n"
                f"Link: {link}"
            )
            send_discord(msg)
            recent_t1[coin_id] = now
            alerts_sent += 1
            continue  # don't also send Tier 2 for same coin

        # --- Tier 2: Early Build (earlier heads-up) ---
        last_t2 = int(recent_t2.get(coin_id, 0))
        t2_ok = (
            vol >= T2_MIN_VOL_USD and
            c24 >= T2_MIN_24H_MOVE and
            outperf >= T2_OUTPERFORM_BTC_24H and
            steps >= 3 and  # at least ~30 minutes of data
            up_steps >= MIN_UP_STEPS_T2 and
            hour_move >= MIN_1H_MOVE_EARLY and
            (now - last_t2) >= COOLDOWN_T2
        )

        if t2_ok:
            msg = (
                f"🟡 **[Early Build — Tier 2]**\n"
                f"Coin: **{name} ({sym})** | Rank: #{rank}\n"
                f"Move: {c24:.1f}% (24h), {c1h:.1f}% (1h)\n"
                f"BTC: {btc_24h:.1f}% (24h) → Outperformance: **{outperf:.1f}%**\n"
                f"1h persistence: {up_steps}/{steps} up-steps | 1h move: **{hour_move:.2f}%**\n"
                f"Volume: ${fmt_int(vol)}\n"
                f"Link: {link}"
            )
            send_discord(msg)
            recent_t2[coin_id] = now
            alerts_sent += 1

    # Save updated state
    state["history"] = hist
    state["recent_alerts_t1"] = recent_t1
    state["recent_alerts_t2"] = recent_t2
    save_state(state)

    return alerts_sent


if __name__ == "__main__":
    alerts = run_once()
    print(f"Done. Alerts sent: {alerts}")
