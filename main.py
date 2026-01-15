import os
import json
import time
import math
from typing import Dict, List, Any, Optional, Tuple
import requests

# =========================
# CONFIG
# =========================
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

VS = "usd"
TOP_N = 250

STATE_FILE = "state.json"

# History window (rolling)
HISTORY_DAYS = 8
HISTORY_SECONDS = HISTORY_DAYS * 24 * 60 * 60

# --- Tier 1: Base Break Watch (goal = red-circle)
BASE_DAYS_DEFAULT = 4              # main window
BASE_DAYS_MIN = 3                  # flexible scoring
BASE_DAYS_MAX = 5                  # flexible scoring

BASE_MAX_RANGE_PCT = 0.10          # 10% base range (tighter = higher quality)
CLEARANCE_ABOVE_BASE_HIGH = 0.03   # must clear base high by 3%
MAX_STRETCH_FROM_BASE_AVG = 0.25   # if already 25%+ above base avg => too late

LIFT_6H_PCT = 4.0
LIFT_12H_PCT = 6.5

# Persistence & short-term confluence (timestamp-based, not cron-based)
PERSIST_WINDOW_MIN = 120           # last 2 hours
PERSIST_MIN_GREEN_RATIO = 0.70     # 70% green steps (friend suggestion)
MA_FAST_STEPS = 6                  # last ~1h if ~10min cadence
MA_SLOW_STEPS = 12                 # last ~2h if ~10min cadence

MIN_VOL_T1 = 20_000_000
MIN_MARKET_CAP_T1 = 50_000_000
MIN_OUTPERF_T1 = 4.0

VOL_RATIO_T1 = 1.5                 # vol_now vs base median
VOL_SPIKE_RATIO_T1 = 1.5           # recent vol (last hour-ish) vs base median

COOLDOWN_T1 = 24 * 60 * 60

# --- Tier 2: Early build (must be green now; no “-5.7% 1h” nonsense)
MIN_24H_MOVE_T2 = 8.0
MIN_OUTPERF_T2 = 4.0
MIN_VOL_T2 = 20_000_000
MIN_RANK_T2 = 200

MIN_1H_MOVE_T2 = 0.2
DUMP_GUARD_1H = -2.0

COOLDOWN_T2 = 6 * 60 * 60

# --- Tier 3: Momentum / Breakout
MIN_24H_MOVE_T3 = 15.0
MIN_1H_MOVE_T3 = 1.0
MIN_OUTPERF_T3 = 8.0
MIN_VOL_T3 = 50_000_000
MIN_RANK_T3 = 200

COOLDOWN_T3 = 3 * 60 * 60

# --- Market context (BTC trend)
BTC_TREND_WINDOW = 24 * 60 * 60
MIN_BTC_TREND = -5.0   # if BTC is down more than -5% in last 24h => skip Tier1/2

# --- Rate limiting (avoid spam)
MAX_ALERTS_PER_HOUR = 5

# =========================
# UTIL
# =========================
def now_ts() -> int:
    return int(time.time())

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {
            "history": {},
            "cooldowns": {"t1": {}, "t2": {}, "t3": {}},
            "recent_alert_times": []
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "history": {},
            "cooldowns": {"t1": {}, "t2": {}, "t3": {}},
            "recent_alert_times": []
        }

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))

def send_discord(msg: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set; message would be:\n", msg)
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=20)
        if r.status_code >= 300:
            print("Discord error:", r.status_code, r.text[:250])
    except Exception as e:
        print("Discord send failed:", e)

def fmt_int(x: float) -> str:
    try:
        x = float(x)
    except Exception:
        return str(x)
    return f"{int(x):,}"

def compute_return_pct(from_price: Optional[float], to_price: Optional[float]) -> Optional[float]:
    if from_price is None or to_price is None:
        return None
    if from_price <= 0:
        return None
    return (to_price / from_price - 1.0) * 100.0

def median(vals: List[float]) -> Optional[float]:
    v = [float(x) for x in vals if x is not None]
    if not v:
        return None
    v.sort()
    n = len(v)
    mid = n // 2
    return v[mid] if n % 2 else 0.5 * (v[mid - 1] + v[mid])

def clamp_history(history: Dict[str, List[Dict[str, Any]]], cutoff: int) -> None:
    for cid in list(history.keys()):
        pts = history.get(cid, [])
        pts2 = [p for p in pts if int(p.get("ts", 0)) >= cutoff]
        if pts2:
            history[cid] = pts2
        else:
            history.pop(cid, None)

def get_recent_points(history: Dict[str, List[Dict[str, Any]]], coin_id: str, window_seconds: int, now: int) -> List[Dict[str, Any]]:
    pts = history.get(coin_id, [])
    if not pts:
        return []
    cutoff = now - window_seconds
    pts2 = [p for p in pts if int(p.get("ts", 0)) >= cutoff]
    pts2.sort(key=lambda x: int(x.get("ts", 0)))
    return pts2

def green_step_ratio(pts: List[Dict[str, Any]]) -> float:
    prices = [float(p["p"]) for p in pts if "p" in p]
    if len(prices) < 2:
        return 0.0
    green = 0
    total = 0
    for i in range(1, len(prices)):
        total += 1
        if prices[i] >= prices[i-1]:
            green += 1
    return green / total if total else 0.0

def moving_average(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)

# =========================
# DATA FETCH
# =========================
def fetch_markets() -> List[Dict[str, Any]]:
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": VS,
        "order": "market_cap_desc",
        "per_page": TOP_N,
        "page": 1,
        "sparkline": "false",
        "price_change_percentage": "1h,24h,7d",
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def extract_btc_24h(markets: List[Dict[str, Any]]) -> float:
    for c in markets:
        if c.get("id") == "bitcoin":
            v = c.get("price_change_percentage_24h_in_currency")
            if v is not None:
                return float(v)
    return 0.0

# =========================
# SCORING
# =========================
def score_tier1(t1: Dict[str, Any]) -> float:
    score = 0.0

    # tighter base
    br = float(t1.get("base_range_pct", 999))
    if br < 8.0:
        score += 3
    elif br < 10.0:
        score += 2
    else:
        score += 1

    # volume ratio
    vr = float(t1.get("vol_ratio", 0) or 0)
    if vr > 2.0:
        score += 3
    elif vr > 1.5:
        score += 2
    elif vr > 1.3:
        score += 1

    # volume spike ratio
    sr = float(t1.get("spike_ratio", 0) or 0)
    if sr > 2.0:
        score += 3
    elif sr > 1.5:
        score += 2
    elif sr > 1.3:
        score += 1

    # relative strength
    op = float(t1.get("outperf", 0))
    if op > 8.0:
        score += 2
    elif op > 6.0:
        score += 1

    # base length bonus
    bd = int(t1.get("base_days", BASE_DAYS_DEFAULT))
    if bd >= 5:
        score += 1.5
    elif bd >= 4:
        score += 1.0

    return score

def confidence_label(score: float) -> str:
    if score >= 8:
        return "🔥 VERY HIGH"
    if score >= 6:
        return "HIGH"
    if score >= 4:
        return "MED"
    return "LOW"

# =========================
# DETECTION
# =========================
def tier1_base_break(history: Dict[str, List[Dict[str, Any]]],
                     coin_id: str, now: int,
                     c24: float, btc24: float,
                     vol_now: float, rank: int,
                     mcap: float) -> Optional[Dict[str, Any]]:

    if rank > TOP_N:
        return None
    if mcap < MIN_MARKET_CAP_T1:
        return None
    outperf = c24 - btc24
    if outperf < MIN_OUTPERF_T1:
        return None
    if vol_now < MIN_VOL_T1:
        return None

    # Try base windows 3,4,5 days; choose best (tightest that still passes)
    best = None

    for base_days in range(BASE_DAYS_MIN, BASE_DAYS_MAX + 1):
        base_window = base_days * 24 * 60 * 60
        base_pts = get_recent_points(history, coin_id, base_window, now)
        if len(base_pts) < 20:
            continue

        prices = [float(p["p"]) for p in base_pts if "p" in p]
        if len(prices) < 20:
            continue

        p_low = min(prices)
        p_high = max(prices)
        p_avg = sum(prices) / len(prices)
        if p_avg <= 0:
            continue

        base_range_pct = (p_high - p_low) / p_avg
        if base_range_pct > BASE_MAX_RANGE_PCT:
            continue

        # Lift checks (use price at ~6h/12h ago inside this base window)
        p_now = prices[-1]
        # find nearest point at or before cutoff by scanning sorted points
        pts_sorted = base_pts  # already sorted
        def price_at(ts_cut: int) -> Optional[float]:
            last = None
            for p in pts_sorted:
                ts = int(p.get("ts", 0))
                if ts <= ts_cut and "p" in p:
                    last = float(p["p"])
                else:
                    break
            return last

        p_6h = price_at(now - 6 * 60 * 60)
        p_12h = price_at(now - 12 * 60 * 60)
        r6 = compute_return_pct(p_6h, p_now)
        r12 = compute_return_pct(p_12h, p_now)
        if r6 is None or r12 is None:
            continue
        if r6 < LIFT_6H_PCT or r12 < LIFT_12H_PCT:
            continue

        # False breakout filters
        # 1) must be above base high by 3%
        if p_now < p_high * (1.0 + CLEARANCE_ABOVE_BASE_HIGH):
            continue

        # 2) must NOT be too stretched from base avg
        stretch = (p_now - p_avg) / p_avg
        if stretch > MAX_STRETCH_FROM_BASE_AVG:
            continue

        # Persistence (timestamp-based)
        persist_pts = get_recent_points(history, coin_id, PERSIST_WINDOW_MIN * 60, now)
        if len(persist_pts) < 6:
            continue
        g_ratio = green_step_ratio(persist_pts)
        if g_ratio < PERSIST_MIN_GREEN_RATIO:
            continue

        # Multi-timeframe confluence (MA fast > MA slow)
        persist_prices = [float(p["p"]) for p in persist_pts if "p" in p]
        if len(persist_prices) >= MA_FAST_STEPS:
            ma_fast = moving_average(persist_prices[-MA_FAST_STEPS:])
            if len(persist_prices) >= MA_SLOW_STEPS:
                ma_slow = moving_average(persist_prices[-MA_SLOW_STEPS:])
            else:
                ma_slow = ma_fast
            if ma_fast is not None and ma_slow is not None and ma_fast <= ma_slow:
                continue

        # Volume checks
        base_vols = [float(p.get("v", 0)) for p in base_pts if p.get("v") is not None]
        med_v = median([v for v in base_vols if v > 0]) if base_vols else None

        vol_ratio = None
        if med_v and med_v > 0:
            vol_ratio = vol_now / med_v
            if vol_ratio < VOL_RATIO_T1:
                continue

        # Volume spike acceleration (last ~hour compared to base median)
        # use last ~6 samples from persist window (works even if cron drifts)
        last_k = persist_pts[-6:] if len(persist_pts) >= 6 else persist_pts
        recent_vol = median([float(p.get("v", 0)) for p in last_k if p.get("v") is not None and float(p.get("v", 0)) > 0])
        spike_ratio = None
        if recent_vol and med_v and med_v > 0:
            spike_ratio = recent_vol / med_v
            if spike_ratio < VOL_SPIKE_RATIO_T1:
                continue

        candidate = {
            "base_days": base_days,
            "base_range_pct": base_range_pct * 100.0,
            "r6": r6,
            "r12": r12,
            "outperf": outperf,
            "vol_ratio": vol_ratio,
            "spike_ratio": spike_ratio,
            "green_ratio": g_ratio * 100.0,
            "stretch_pct": stretch * 100.0,
        }

        # Choose best candidate: tighter base preferred, then longer base
        if best is None:
            best = candidate
        else:
            if candidate["base_range_pct"] < best["base_range_pct"]:
                best = candidate
            elif candidate["base_range_pct"] == best["base_range_pct"] and candidate["base_days"] > best["base_days"]:
                best = candidate

    return best


def tier2_early_build(history: Dict[str, List[Dict[str, Any]]],
                      coin_id: str, now: int,
                      c24: float, c1h: float, btc24: float,
                      vol_now: float, rank: int) -> Optional[Dict[str, Any]]:
    if rank > MIN_RANK_T2:
        return None
    outperf = c24 - btc24
    if c24 < MIN_24H_MOVE_T2:
        return None
    if outperf < MIN_OUTPERF_T2:
        return None
    if vol_now < MIN_VOL_T2:
        return None

    # Hard "no reversal" gates
    if c1h <= DUMP_GUARD_1H:
        return None
    if c1h < MIN_1H_MOVE_T2:
        return None

    pts_60 = get_recent_points(history, coin_id, 60 * 60, now)
    prices_60 = [float(p["p"]) for p in pts_60 if "p" in p]
    if len(prices_60) < 5:
        return None

    if prices_60[-1] < prices_60[-2]:
        return None

    return {"outperf": outperf}


def tier3_momentum(c24: float, c1h: float, btc24: float, vol_now: float, rank: int) -> bool:
    if rank > MIN_RANK_T3:
        return False
    outperf = c24 - btc24
    if c24 < MIN_24H_MOVE_T3:
        return False
    if c1h < MIN_1H_MOVE_T3:
        return False
    if outperf < MIN_OUTPERF_T3:
        return False
    if vol_now < MIN_VOL_T3:
        return False
    return True

# =========================
# MAIN
# =========================
def run_once() -> int:
    now = now_ts()
    state = load_state()

    history: Dict[str, List[Dict[str, Any]]] = state.get("history", {})
    cooldowns = state.get("cooldowns", {"t1": {}, "t2": {}, "t3": {}})
    recent_alert_times: List[int] = state.get("recent_alert_times", [])

    # Clean recent alert times
    recent_alert_times = [t for t in recent_alert_times if now - int(t) < 3600]

    markets = fetch_markets()
    btc24 = extract_btc_24h(markets)

    # Update history snapshot
    cutoff = now - HISTORY_SECONDS
    for c in markets:
        cid = c.get("id")
        if not cid:
            continue
        price = c.get("current_price")
        vol = c.get("total_volume")
        mcap = c.get("market_cap") or 0
        if price is None or vol is None:
            continue
        pts = history.get(cid, [])
        pts.append({"ts": now, "p": float(price), "v": float(vol), "m": float(mcap)})
        history[cid] = pts

    clamp_history(history, cutoff)

    # BTC context filter (based on stored BTC history)
    btc_trend = None
    btc_pts = get_recent_points(history, "bitcoin", BTC_TREND_WINDOW, now)
    if len(btc_pts) >= 2:
        btc_prices = [float(p["p"]) for p in btc_pts if "p" in p]
        if len(btc_prices) >= 2:
            btc_trend = compute_return_pct(btc_prices[0], btc_prices[-1])

    skip_t1_t2 = False
    if btc_trend is not None and btc_trend < MIN_BTC_TREND:
        skip_t1_t2 = True  # market dumping; reduce false positives

    # We'll collect candidates first, then apply rate limit by highest score
    candidates: List[Tuple[float, str, str]] = []  # (score, tier_label, message)

    for c in markets:
        cid = c.get("id")
        if not cid:
            continue

        rank = c.get("market_cap_rank")
        if rank is None:
            continue
        rank = int(rank)

        name = c.get("name", cid)
        sym = (c.get("symbol") or "").upper()
        link = f"https://www.coingecko.com/en/coins/{cid}"

        c24 = c.get("price_change_percentage_24h_in_currency")
        c1h = c.get("price_change_percentage_1h_in_currency")

        if c24 is None:
            continue
        c24 = float(c24)
        c1h = float(c1h) if c1h is not None else 0.0

        vol_now = float(c.get("total_volume") or 0.0)
        mcap = float(c.get("market_cap") or 0.0)
        outperf = c24 - btc24

        # Tier 1
        if not skip_t1_t2:
            last_t1 = int(cooldowns.get("t1", {}).get(cid, 0))
            if (now - last_t1) >= COOLDOWN_T1:
                t1 = tier1_base_break(history, cid, now, c24, btc24, vol_now, rank, mcap)
                if t1:
                    score = score_tier1(t1)
                    conf = confidence_label(score)
                    vol_ratio_txt = f"{t1['vol_ratio']:.2f}x" if t1.get("vol_ratio") else "n/a"
                    spike_txt = f"{t1['spike_ratio']:.2f}x" if t1.get("spike_ratio") else "n/a"

                    msg = (
                        f"🔵 **[Base Break Watch — Tier 1]** | Score: **{score:.1f}** | {conf}\n"
                        f"Coin: **{name} ({sym})** | Rank: #{rank}\n"
                        f"Base: **{t1['base_days']}d** | Range: **{t1['base_range_pct']:.1f}%** | Stretch: **{t1['stretch_pct']:.1f}%**\n"
                        f"Lift: **{t1['r6']:.1f}% (6h)**, **{t1['r12']:.1f}% (12h)**\n"
                        f"RS vs BTC (24h): **{outperf:.1f}%** (Coin {c24:.1f}% vs BTC {btc24:.1f}%)\n"
                        f"Persistence: **{t1['green_ratio']:.0f}%** green steps (last {PERSIST_WINDOW_MIN}m)\n"
                        f"Volume(24h): **${fmt_int(vol_now)}** | Vol/base: **{vol_ratio_txt}** | Spike: **{spike_txt}**\n"
                        f"MCap: **${fmt_int(mcap)}**\n"
                        f"Link: {link}"
                    )
                    candidates.append((score, "t1", msg))

        # Tier 2
        if not skip_t1_t2:
            last_t2 = int(cooldowns.get("t2", {}).get(cid, 0))
            if (now - last_t2) >= COOLDOWN_T2:
                t2 = tier2_early_build(history, cid, now, c24, c1h, btc24, vol_now, rank)
                if t2:
                    # Score Tier2 lightly: preference for stronger RS + positive 1h
                    score = 3.0
                    if outperf > 8:
                        score += 2
                    if c1h > 1.0:
                        score += 1
                    msg = (
                        f"🟡 **[Early Build — Tier 2]** | Score: **{score:.1f}**\n"
                        f"Coin: **{name} ({sym})** | Rank: #{rank}\n"
                        f"Move: **{c24:.1f}% (24h)**, **{c1h:.1f}% (1h)**\n"
                        f"RS vs BTC (24h): **{outperf:.1f}%**\n"
                        f"Volume(24h): **${fmt_int(vol_now)}** | MCap: **${fmt_int(mcap)}**\n"
                        f"Link: {link}"
                    )
                    candidates.append((score, "t2", msg))

        # Tier 3
        last_t3 = int(cooldowns.get("t3", {}).get(cid, 0))
        if (now - last_t3) >= COOLDOWN_T3:
            if tier3_momentum(c24, c1h, btc24, vol_now, rank):
                score = 2.0 + (1.0 if outperf > 10 else 0.0)
                msg = (
                    f"🔴 **[Momentum / Breakout — Tier 3]**\n"
                    f"Coin: **{name} ({sym})** | Rank: #{rank}\n"
                    f"Move: **{c24:.1f}% (24h)**, **{c1h:.1f}% (1h)**\n"
                    f"RS vs BTC (24h): **{outperf:.1f}%**\n"
                    f"Volume(24h): **${fmt_int(vol_now)}** | MCap: **${fmt_int(mcap)}**\n"
                    f"Link: {link}"
                )
                candidates.append((score, "t3", msg))

    # Apply max alerts per hour. Prefer highest score.
    candidates.sort(key=lambda x: x[0], reverse=True)

    alerts_sent = 0
    for score, tier, msg in candidates:
        if len(recent_alert_times) >= MAX_ALERTS_PER_HOUR:
            # if rate limited, only allow very high conviction Tier1
            if not (tier == "t1" and score >= 8.0):
                continue

        send_discord(msg)
        recent_alert_times.append(now)
        alerts_sent += 1

        # update cooldown for the coin in its tier
        # (extract coin_id from link is messy; instead: store last send hash in msg? we keep it simple by using tier scanning)
        # We'll set cooldowns by parsing Link line
        try:
            coin_id = msg.split("https://www.coingecko.com/en/coins/")[1].split("\n")[0].strip()
            if tier == "t1":
                cooldowns["t1"][coin_id] = now
            elif tier == "t2":
                cooldowns["t2"][coin_id] = now
            elif tier == "t3":
                cooldowns["t3"][coin_id] = now
        except Exception:
            pass

    state["history"] = history
    state["cooldowns"] = cooldowns
    state["recent_alert_times"] = recent_alert_times
    save_state(state)

    # Print BTC context for logs
    if btc_trend is not None:
        print(f"BTC trend (24h from stored history): {btc_trend:.2f}% | skip_t1_t2={skip_t1_t2}")

    return alerts_sent


if __name__ == "__main__":
    n = run_once()
    print(f"Done. Alerts sent: {n}")
