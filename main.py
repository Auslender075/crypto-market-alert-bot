import os
import time
import requests

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"

VS_CURRENCY = "usd"
TOP_N = 250
CHECK_EVERY_SECONDS = 300  # 5 minutes

OUTPERFORM_BTC_24H = 8.0
MIN_24H_MOVE = 10.0
MIN_VOL_USD = 30_000_000

recent_alerts = {}

def send_discord(msg):
    requests.post(DISCORD_WEBHOOK_URL, json={"content": msg})

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

def main():
    send_discord("🟢 Crypto market alert bot started.")
    while True:
        try:
            data = fetch_markets()
            btc = next(c for c in data if c["id"] == "bitcoin")
            btc_24h = btc["price_change_percentage_24h_in_currency"] or 0

            for c in data:
                c24 = c.get("price_change_percentage_24h_in_currency")
                if c24 is None:
                    continue

                if c["total_volume"] < MIN_VOL_USD:
                    continue

                if c24 < MIN_24H_MOVE:
                    continue

                outperf = c24 - btc_24h
                if outperf >= OUTPERFORM_BTC_24H:
                    coin_id = c["id"]
                    now = time.time()
                    if now - recent_alerts.get(coin_id, 0) > 6 * 3600:
                        msg = (
                            f"🚨 **[Rotation Alert]**\n"
                            f"{c['name']} ({c['symbol'].upper()})\n"
                            f"24h: {c24:.1f}% | BTC: {btc_24h:.1f}%\n"
                            f"Outperformance: {outperf:.1f}%\n"
                            f"Volume: ${int(c['total_volume']):,}\n"
                            f"https://www.coingecko.com/en/coins/{coin_id}"
                        )
                        send_discord(msg)
                        recent_alerts[coin_id] = now

        except Exception as e:
            send_discord(f"⚠️ Bot error: {e}")

        time.sleep(CHECK_EVERY_SECONDS)

if __name__ == "__main__":
    main()
