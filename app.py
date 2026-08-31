import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
import uvicorn

from advanced_smc import analyze_advanced

mcp = FastMCP("Shark Advanced SMC")

DEFAULT_PAIR = "BTCUSDT"
SUPPORTED_PAIRS = {"BTCUSDT", "XAUUSDT"}
CONTRACT_TYPE = "PERPETUAL"
VALID_INTERVALS = {"5m", "15m", "1h", "4h"}
EXCHANGES = {"BINANCE", "BYBIT"}

BINANCE_BASE_URLS = [x.strip().rstrip("/") for x in os.getenv("BINANCE_FAPI_URLS", "https://fapi.binance.com,https://fapi1.binance.com,https://fapi2.binance.com,https://fapi3.binance.com,https://fapi4.binance.com").split(",") if x.strip()]
BYBIT_BASE_URLS = [x.strip().rstrip("/") for x in os.getenv("BYBIT_V5_URLS", "https://api.bybit.com,https://api.bytick.com,https://api.bybit.eu,https://api.bybit.ae,https://api.bybit.id,https://api.bybit.kz,https://api.bybit-tr.com,https://api.byhkbit.com,https://api.bybitgeorgia.ge").split(",") if x.strip()]
MARKET_PROXY_URLS = [x.strip() for x in os.getenv("MARKET_PROXY_URLS", "https://api.allorigins.win/raw?url={url},https://corsproxy.io/?url={url},https://api.codetabs.com/v1/proxy?quest={url}").split(",") if x.strip()]

if os.getenv("BINANCE_FAPI_URL"):
    chosen = os.getenv("BINANCE_FAPI_URL", "").rstrip("/")
    BINANCE_BASE_URLS = [chosen] + [x for x in BINANCE_BASE_URLS if x != chosen]
if os.getenv("BYBIT_V5_URL"):
    chosen = os.getenv("BYBIT_V5_URL", "").rstrip("/")
    BYBIT_BASE_URLS = [chosen] + [x for x in BYBIT_BASE_URLS if x != chosen]

TELEGRAM_ALERTS_ENABLED = os.getenv("TELEGRAM_ALERTS_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
SCANNER_ENABLED = os.getenv("SCANNER_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
SCANNER_INTERVAL_SECONDS = max(30, int(os.getenv("SCANNER_INTERVAL_SECONDS", "60")))
_telegram_alert_keys: set[str] = set()
_scanner_task: asyncio.Task | None = None
_scanner_last_run: str | None = None
_scanner_last_error: str | None = None


def _validate_pair(pair: str) -> str:
    symbol = (pair or DEFAULT_PAIR).upper().replace("/", "").replace("-", "")
    if symbol not in SUPPORTED_PAIRS:
        raise ValueError("Supported perpetual pairs: BTCUSDT, XAUUSDT")
    return symbol


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> Any:
    response = await client.get(url, params=params)
    response.raise_for_status()
    return response.json()


async def _get_json_failover(client: httpx.AsyncClient, urls: list[str], path: str, params: dict[str, Any]) -> Any:
    errors: list[str] = []
    for base_url in urls:
        try:
            return await _get_json(client, f"{base_url}{path}", params)
        except Exception as exc:
            errors.append(f"{base_url}: {type(exc).__name__}: {exc}")
    query = urlencode(params)
    for base_url in urls:
        target = f"{base_url}{path}?{query}"
        encoded_target = quote(target, safe="")
        for template in MARKET_PROXY_URLS:
            try:
                if "{url}" not in template:
                    continue
                response = await client.get(template.replace("{url}", encoded_target))
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                errors.append(f"relay {template.split('?')[0]} -> {base_url}: {type(exc).__name__}: {exc}")
    raise RuntimeError("All exchange endpoints and read-only relays failed: " + " | ".join(errors))


async def fetch_klines(exchange: str, pair: str, interval: str, limit: int = 300) -> list[dict]:
    exchange = exchange.upper()
    symbol = _validate_pair(pair)
    if exchange not in EXCHANGES:
        raise ValueError("Exchange must be BINANCE or BYBIT")
    if interval not in VALID_INTERVALS:
        raise ValueError("Interval must be one of: 5m, 15m, 1h, 4h")
    limit = min(max(int(limit), 50), 1000)
    bybit_interval = {"5m": "5", "15m": "15", "1h": "60", "4h": "240"}[interval]
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SharkSMC/2.0)", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as client:
        if exchange == "BINANCE":
            rows = await _get_json_failover(client, BINANCE_BASE_URLS, "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        else:
            data = await _get_json_failover(client, BYBIT_BASE_URLS, "/v5/market/kline", {"category": "linear", "symbol": symbol, "interval": bybit_interval, "limit": limit})
            if data.get("retCode") not in (None, 0):
                raise ValueError(f"Bybit error: {data.get('retMsg')}")
            rows = data.get("result", {}).get("list", [])
    if not isinstance(rows, list):
        raise ValueError(f"Unexpected kline response from {exchange}")
    candles: list[dict] = []
    for row in rows:
        try:
            if exchange == "BINANCE":
                candles.append({"time": int(row[0]), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]), "endTime": int(row[6]), "volume": float(row[5])})
            else:
                candles.append({"time": int(row[0]), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]), "endTime": int(row[0]), "volume": float(row[5])})
        except (IndexError, TypeError, ValueError):
            continue
    candles.sort(key=lambda x: x["time"])
    if len(candles) < 50:
        raise ValueError(f"Not enough candles from {exchange} for {symbol} {interval}: {len(candles)}")
    return candles[:-1]


async def fetch_live_price(exchange: str, pair: str) -> float:
    exchange = exchange.upper()
    symbol = _validate_pair(pair)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SharkSMC/2.0)", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
        if exchange == "BINANCE":
            data = await _get_json_failover(client, BINANCE_BASE_URLS, "/fapi/v1/ticker/price", {"symbol": symbol})
            return float(data["price"])
        data = await _get_json_failover(client, BYBIT_BASE_URLS, "/v5/market/tickers", {"category": "linear", "symbol": symbol})
        return float(data["result"]["list"][0]["lastPrice"])


def _fmt_price(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


async def _send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not TELEGRAM_ALERTS_ENABLED or not token or not chat_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text})
            data = response.json()
        return bool(response.is_success and data.get("ok") is True)
    except Exception:
        return False


async def _notify_confirmed_signal(exchange: str, result: dict[str, Any]) -> bool:
    if result.get("setup") != "VALID" or result.get("direction") not in {"LONG", "SHORT"}:
        return False
    pair = result.get("pair", DEFAULT_PAIR)
    zone = result.get("entry_zone") or {}
    key = "|".join([exchange, pair, str(result.get("direction")), str(zone.get("low")), str(zone.get("high")), str(result.get("stop_loss")), str(result.get("take_profit_1")), str(result.get("take_profit_2"))])
    if key in _telegram_alert_keys:
        return False
    message = (
        f"🦈 SHARK {pair} SMC — CONFIRMED SIGNAL\n\n"
        f"Exchange: {exchange}\nPair: {pair} Perpetual\nDirection: {result['direction']}\n"
        f"Score: {result.get('score', 0)}/100\nAction: {result.get('action', 'WAIT')}\n\n"
        f"Entry zone: {_fmt_price(zone.get('low'))} – {_fmt_price(zone.get('high'))}\n"
        f"Stop loss: {_fmt_price(result.get('stop_loss'))}\nTP1: {_fmt_price(result.get('take_profit_1'))}\nTP2: {_fmt_price(result.get('take_profit_2'))}\n"
        f"Live price: {_fmt_price(result.get('live_price'))}\n\n"
        "Advanced SMC confluence confirmed."
    )
    sent = await _send_telegram(message)
    if sent:
        _telegram_alert_keys.add(key)
    return sent


async def _run_one(exchange: str, pair: str, notify: bool = True) -> dict:
    symbol = _validate_pair(pair)
    h4, h1, m15, m5 = await asyncio.gather(
        fetch_klines(exchange, symbol, "4h"), fetch_klines(exchange, symbol, "1h"),
        fetch_klines(exchange, symbol, "15m"), fetch_klines(exchange, symbol, "5m"),
    )
    live = await fetch_live_price(exchange, symbol)
    analysis = analyze_advanced(h4, h1, m15, m5, live)
    result = {"exchange": exchange, "pair": symbol, "contract_type": CONTRACT_TYPE, "engine": "ADVANCED_SMC", **analysis}
    result["telegram_alert_sent"] = await _notify_confirmed_signal(exchange, result) if notify else False
    return result


def _normalize_exchange(value: str | None) -> str:
    raw = (value or "BOTH").strip().upper()
    compact = raw.replace(" ", "").replace("%20", "")
    if compact in {"", "BOTH", "BINANCE+BYBIT", "BYBIT+BINANCE", "BINANCE,BYBIT", "BYBIT,BINANCE", "BINANCEBYBIT", "BYBITBINANCE"}:
        return "BOTH"
    return raw


async def _run_smc(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    symbol = _validate_pair(pair)
    exchange = _normalize_exchange(exchange)
    if exchange == "BOTH":
        results = await asyncio.gather(_run_one("BINANCE", symbol), _run_one("BYBIT", symbol), return_exceptions=True)
        output = {name: ({"error": str(result)} if isinstance(result, Exception) else result) for name, result in zip(("BINANCE", "BYBIT"), results)}
        b, y = output["BINANCE"], output["BYBIT"]
        if "error" not in b and "error" not in y:
            if b.get("setup") == "VALID" and y.get("setup") == "VALID" and b.get("direction") == y.get("direction"):
                consensus = "VALID_BOTH_EXCHANGES"
            elif b.get("direction") == y.get("direction") and b.get("direction") in {"LONG", "SHORT"}:
                consensus = "ALIGNED_WAIT_FOR_CONFIRMATION"
            else:
                consensus = "NO_CONSENSUS"
        else:
            consensus = "PARTIAL_DATA"
        return {"pair": symbol, "contract_type": CONTRACT_TYPE, "mode": "BINANCE + BYBIT", "engine": "ADVANCED_SMC", "consensus": consensus, "BINANCE": b, "BYBIT": y}
    if exchange not in EXCHANGES:
        raise ValueError("exchange must be BINANCE, BYBIT, or BOTH")
    return await _run_one(exchange, symbol)


async def _continuous_scanner() -> None:
    global _scanner_last_run, _scanner_last_error
    await asyncio.sleep(5)
    while True:
        try:
            for pair in sorted(SUPPORTED_PAIRS):
                results = await asyncio.gather(
                    _run_one("BINANCE", pair, notify=False),
                    _run_one("BYBIT", pair, notify=False),
                    return_exceptions=True,
                )
                b, y = results
                if isinstance(b, Exception) or isinstance(y, Exception):
                    continue
                # Telegram alert requires the same professional setup on BOTH exchanges.
                if b.get("setup") == "VALID" and y.get("setup") == "VALID" and b.get("direction") == y.get("direction"):
                    consensus = dict(b)
                    consensus["pair"] = pair
                    consensus["exchange"] = "BINANCE + BYBIT"
                    consensus["score"] = min(int(b.get("score", 0)), int(y.get("score", 0)))
                    consensus["action"] = "ENTER_ON_RETRACE" if b.get("action") == "ENTER_ON_RETRACE" and y.get("action") == "ENTER_ON_RETRACE" else "WAIT_FOR_RETRACE"
                    zone_b = b.get("entry_zone") or {}
                    zone_y = y.get("entry_zone") or {}
                    if zone_b.get("low") is not None and zone_y.get("low") is not None:
                        low = max(float(zone_b["low"]), float(zone_y["low"]))
                        high = min(float(zone_b["high"]), float(zone_y["high"]))
                        if low < high:
                            consensus["entry_zone"] = {"low": low, "high": high, "mid": (low + high) / 2}
                    await _notify_confirmed_signal("BINANCE+BYBIT", consensus)
            _scanner_last_error = None
            _scanner_last_run = str(asyncio.get_running_loop().time())
        except Exception as exc:
            _scanner_last_error = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(SCANNER_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app):
    global _scanner_task
    if SCANNER_ENABLED:
        _scanner_task = asyncio.create_task(_continuous_scanner())
    try:
        yield
    finally:
        if _scanner_task:
            _scanner_task.cancel()
            try:
                await _scanner_task
            except asyncio.CancelledError:
                pass


@mcp.tool
async def get_perpetual_smc_analysis(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    return await _run_smc(exchange, pair)


@mcp.tool
async def get_btcusdt_perpetual_smc_analysis(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    return await _run_smc(exchange, "BTCUSDT")


@mcp.tool
async def get_xauusdt_perpetual_smc_analysis(exchange: str = "BOTH", pair: str = "XAUUSDT") -> dict:
    return await _run_smc(exchange, "XAUUSDT")


@mcp.tool
async def get_perpetual_market_data(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    symbol = _validate_pair(pair)
    async def one(ex: str) -> dict:
        live, h4, h1, m15, m5 = await asyncio.gather(
            fetch_live_price(ex, symbol), fetch_klines(ex, symbol, "4h"), fetch_klines(ex, symbol, "1h"),
            fetch_klines(ex, symbol, "15m"), fetch_klines(ex, symbol, "5m"),
        )
        return {"exchange": ex, "pair": symbol, "contract_type": CONTRACT_TYPE, "live_price": live, "4h": h4, "1h": h1, "15m": m15, "5m": m5}
    if _normalize_exchange(exchange) == "BOTH":
        results = await asyncio.gather(one("BINANCE"), one("BYBIT"), return_exceptions=True)
        return {name: (r if not isinstance(r, Exception) else {"error": str(r)}) for name, r in zip(("BINANCE", "BYBIT"), results)}
    return await one(_normalize_exchange(exchange))


async def health(request):
    return JSONResponse({"status": "ok", "service": "Shark Advanced SMC", "supported_pairs": sorted(SUPPORTED_PAIRS), "contract_type": CONTRACT_TYPE, "exchanges": sorted(EXCHANGES), "scanner_enabled": SCANNER_ENABLED, "scanner_interval_seconds": SCANNER_INTERVAL_SECONDS, "public_api": "/api/smc?exchange=BOTH&pair=BTCUSDT"})


async def public_smc(request):
    try:
        return JSONResponse(await _run_smc(request.query_params.get("exchange", "BOTH"), request.query_params.get("pair", DEFAULT_PAIR)))
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)


async def scanner_status(request):
    return JSONResponse({"scanner_enabled": SCANNER_ENABLED, "interval_seconds": SCANNER_INTERVAL_SECONDS, "last_run": _scanner_last_run, "last_error": _scanner_last_error})


async def test_telegram(request):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    test_secret = os.getenv("TELEGRAM_TEST_SECRET", "").strip()
    if not token or not chat_id:
        return JSONResponse({"status": "error", "error": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not configured"}, status_code=500)
    if not test_secret or request.query_params.get("key", "") != test_secret:
        return JSONResponse({"status": "error", "error": "Unauthorized"}, status_code=401)
    ok = await _send_telegram("🦈 Shark SMC — Telegram test successful!\n\nBTCUSDT + XAUUSDT perpetual continuous scanner is connected to the same bot.")
    return JSONResponse({"status": "ok", "telegram": "message_sent"} if ok else {"status": "error", "error": "Telegram API request failed"}, status_code=200 if ok else 502)


public_app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/", health, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/scanner-status", scanner_status, methods=["GET"]),
        Route("/api/smc", public_smc, methods=["GET"]),
        Route("/test-telegram", test_telegram, methods=["GET"]),
        Mount("/mcp", app=mcp.http_app(path="/mcp", transport="streamable-http")),
    ],
)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(public_app, host="0.0.0.0", port=port)
