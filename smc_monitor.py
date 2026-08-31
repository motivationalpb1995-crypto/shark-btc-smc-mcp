import asyncio
import os
import time
from typing import Any

import httpx

from app import DEFAULT_PAIR, _run_one

PAIR = DEFAULT_PAIR.upper()
POLL_SECONDS = int(os.getenv("SMC_POLL_SECONDS", "10"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Alert only after a CLOSED 5m candle has confirmed the SMC trigger.
# The live ticker is then used to detect the retracement into the confirmed POI.
last_alert_key: str | None = None


def _fmt(v: Any) -> str:
    return "-" if v is None else f"{float(v):,.2f}"


async def telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM NOT CONFIGURED:\n" + text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
        r.raise_for_status()
    return True


def strict_confirmed_setup(result: dict) -> tuple[bool, str]:
    """Apply an explicit SMC checklist on top of the normalized engine output.

    Required sequence:
      1) 4H directional structure
      2) 1H same-direction structure
      3) 15M liquidity sweep against the intended direction
      4) 5M MSS in the intended direction
      5) 5M displacement
      6) FVG and/or order block POI
      7) entry only on POI retracement, never on chase
    """
    direction = result.get("direction")
    if direction not in {"LONG", "SHORT"}:
        return False, "No directional HTF bias"

    h4 = result.get("4H", {})
    h1 = result.get("1H", {})
    m15 = result.get("15M", {})
    m5 = result.get("5M", {})
    sweep = m15.get("liquidity", {})
    mss = m5.get("MSS", {})

    if h4.get("bias") != direction_to_bias(direction):
        return False, "4H bias not aligned"
    if h1.get("bias") != direction_to_bias(direction):
        return False, "1H bias not aligned"

    expected_sweep = "SELL_SIDE_LIQUIDITY_SWEEP" if direction == "LONG" else "BUY_SIDE_LIQUIDITY_SWEEP"
    expected_mss = "BULLISH_MSS" if direction == "LONG" else "BEARISH_MSS"
    if sweep.get("type") != expected_sweep:
        return False, "15M liquidity sweep not confirmed"
    if mss.get("type") != expected_mss:
        return False, "5M MSS not confirmed"
    if not m5.get("displacement"):
        return False, "5M displacement not confirmed"
    if not (m5.get("FVG") or m5.get("order_block")):
        return False, "No 5M FVG/OB POI"
    if not result.get("POI"):
        return False, "No usable POI"
    return True, "FULL_SMC_CONFIRMATION"


def direction_to_bias(direction: str) -> str:
    return "BULLISH" if direction == "LONG" else "BEARISH"


def alert_key(result: dict) -> str:
    m5 = result.get("5M", {})
    mss = m5.get("MSS", {})
    poi = result.get("POI") or {}
    return f"{result.get('direction')}:{mss.get('level')}:{poi.get('low')}:{poi.get('high')}"


def build_message(ex: str, result: dict) -> str:
    direction = result["direction"]
    poi = result["POI"]
    return (
        f"🚨 ENTRY AA GAYA — BTCUSDT PERPETUAL\n\n"
        f"Exchange: {ex}\nDirection: {direction}\n"
        f"Live Price: {_fmt(result.get('live_price'))}\n"
        f"Entry Zone: {_fmt(poi.get('low'))} – {_fmt(poi.get('high'))}\n"
        f"SL: {_fmt(result.get('stop_loss'))}\n"
        f"TP1 (2R): {_fmt(result.get('take_profit_1'))}\n"
        f"TP2 (3R): {_fmt(result.get('take_profit_2'))}\n\n"
        f"SMC CHECKLIST\n"
        f"4H bias: ✅\n1H structure: ✅\n"
        f"15M liquidity sweep: ✅\n5M MSS: ✅\n"
        f"5M displacement: ✅\nFVG/OB POI: ✅\n"
        f"Entry: POI retracement only\n\n"
        f"⚠️ Educational setup — risk management required."
    )


async def scan_once() -> None:
    global last_alert_key
    results = await asyncio.gather(
        _run_one("BINANCE", PAIR),
        _run_one("BYBIT", PAIR),
        return_exceptions=True,
    )

    valid: list[tuple[str, dict]] = []
    for ex, item in zip(("BINANCE", "BYBIT"), results):
        if isinstance(item, Exception):
            print(f"{ex} error: {item}")
            continue
        ok, why = strict_confirmed_setup(item)
        print(f"{time.strftime('%H:%M:%S')} {ex}: {item.get('live_price')} {item.get('direction')} {ok} {why}")
        if ok:
            valid.append((ex, item))

    # Highest-quality alert requires both exchanges to agree on direction and have
    # their own complete SMC confirmation. This avoids exchange-specific noise.
    if len(valid) != 2:
        return
    if valid[0][1]["direction"] != valid[1][1]["direction"]:
        return

    b_key = alert_key(valid[0][1])
    y_key = alert_key(valid[1][1])
    key = f"{valid[0][1]['direction']}|{b_key}|{y_key}"
    if key == last_alert_key:
        return

    # Alert when the current live price is actually inside each exchange's POI.
    # This is the entry event, not merely the MSS event.
    for ex, item in valid:
        poi = item["POI"]
        px = item["live_price"]
        if poi["low"] <= px <= poi["high"]:
            last_alert_key = key
            await telegram(build_message(ex, item))
            break


async def main() -> None:
    print(f"SMC monitor started: {PAIR} | poll={POLL_SECONDS}s | Binance + Bybit")
    while True:
        try:
            await scan_once()
        except Exception as exc:
            print(f"Monitor error: {exc}")
        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
