import asyncio
import os
import time
from typing import Any

from app import DEFAULT_PAIR, fetch_klines, fetch_live_price
from advanced_smc import analyze_advanced

PAIR = DEFAULT_PAIR.upper()
POLL_SECONDS = int(os.getenv("SMC_POLL_SECONDS", "10"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
last_alert_key: str | None = None


def _fmt(v: Any) -> str:
    return "-" if v is None else f"{float(v):,.2f}"


async def telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM NOT CONFIGURED:\n" + text)
        return False
    import httpx
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
        r.raise_for_status()
    return True


def strict_advanced(result: dict) -> tuple[bool, str]:
    if result.get("setup") != "VALID":
        return False, result.get("reason", "No advanced SMC confirmation")
    checks = result.get("checks", {})
    required = ("HTF_alignment", "external_liquidity_sweep", "MSS", "displacement", "FVG_or_OB", "POI_confluence")
    if not all(checks.get(k) for k in required):
        return False, "Advanced SMC checklist incomplete"
    if result.get("score", 0) < 85:
        return False, "SMC score below 85"
    poi = result.get("POI")
    if not poi:
        return False, "No POI"
    return True, "ADVANCED_SMC_CONFIRMED"


def alert_key(result: dict) -> str:
    poi = result.get("POI") or {}
    ms = (result.get("5M") or {}).get("MSS") or {}
    return f"{result.get('direction')}:{ms.get('level')}:{poi.get('low')}:{poi.get('high')}"


def build_message(ex: str, result: dict) -> str:
    d = result["direction"]
    poi = result["POI"]
    checks = result["checks"]
    return (
        f"🚨 ENTRY AA GAYA — BTCUSDT PERPETUAL\n\n"
        f"Exchange: {ex}\nDirection: {d}\nSMC Score: {result['score']}/100\n"
        f"Live Price: {_fmt(result.get('live_price'))}\n"
        f"Entry Zone: {_fmt(poi.get('low'))} – {_fmt(poi.get('high'))}\n"
        f"SL: {_fmt(result.get('stop_loss'))}\n"
        f"TP1: {_fmt(result.get('take_profit_1'))}\n"
        f"TP2: {_fmt(result.get('take_profit_2'))}\n\n"
        f"ADVANCED SMC CHECKLIST\n"
        f"4H + 1H HTF alignment: {'✅' if checks.get('HTF_alignment') else '❌'}\n"
        f"15M external liquidity sweep: {'✅' if checks.get('external_liquidity_sweep') else '❌'}\n"
        f"5M MSS: {'✅' if checks.get('MSS') else '❌'}\n"
        f"5M displacement: {'✅' if checks.get('displacement') else '❌'}\n"
        f"FVG / Order Block: {'✅' if checks.get('FVG_or_OB') else '❌'}\n"
        f"POI confluence / breaker: {'✅' if checks.get('POI_confluence') else '❌'}\n"
        f"Premium/Discount filter: ✅\n"
        f"Entry: POI retracement only\n\n"
        f"⚠️ Educational setup — risk management required."
    )


async def run_exchange(ex: str) -> dict:
    h4, h1, m15, m5 = await asyncio.gather(
        fetch_klines(ex, PAIR, "4h", 300),
        fetch_klines(ex, PAIR, "1h", 300),
        fetch_klines(ex, PAIR, "15m", 300),
        fetch_klines(ex, PAIR, "5m", 300),
    )
    live = await fetch_live_price(ex, PAIR)
    return analyze_advanced(h4, h1, m15, m5, live)


async def scan_once() -> None:
    global last_alert_key
    results = await asyncio.gather(run_exchange("BINANCE"), run_exchange("BYBIT"), return_exceptions=True)
    valid: list[tuple[str, dict]] = []
    for ex, item in zip(("BINANCE", "BYBIT"), results):
        if isinstance(item, Exception):
            print(f"{ex} error: {item}")
            continue
        ok, why = strict_advanced(item)
        print(f"{time.strftime('%H:%M:%S')} {ex}: {item.get('live_price')} {item.get('direction')} score={item.get('score')} {ok} {why}")
        if ok:
            valid.append((ex, item))

    if len(valid) != 2 or valid[0][1].get("direction") != valid[1][1].get("direction"):
        return

    key = "|".join(alert_key(x[1]) for x in valid)
    if key == last_alert_key:
        return

    # Alert only when the live price is actually inside the execution POI on
    # at least one exchange; direction must already agree on both exchanges.
    for ex, item in valid:
        poi = item["POI"]
        px = item["live_price"]
        if poi["low"] <= px <= poi["high"]:
            if await telegram(build_message(ex, item)):
                last_alert_key = key
            return


async def main() -> None:
    print(f"ADVANCED SMC monitor started: {PAIR} | poll={POLL_SECONDS}s | Binance + Bybit")
    while True:
        try:
            await scan_once()
        except Exception as exc:
            print(f"Monitor error: {exc}")
        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
