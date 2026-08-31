import os
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
import uvicorn

from advanced_smc import analyze_advanced

mcp = FastMCP("BTCUSDT Perpetual SMC")

DEFAULT_PAIR = "BTCUSDT"
CONTRACT_TYPE = "PERPETUAL"
VALID_INTERVALS = {"5m", "15m", "1h", "4h"}
EXCHANGES = {"BINANCE", "BYBIT"}

BINANCE_BASE_URLS = [
    x.strip().rstrip("/") for x in os.getenv(
        "BINANCE_FAPI_URLS",
        "https://fapi.binance.com,https://fapi1.binance.com,https://fapi2.binance.com,https://fapi3.binance.com,https://fapi4.binance.com",
    ).split(",") if x.strip()
]
BYBIT_BASE_URLS = [
    x.strip().rstrip("/") for x in os.getenv(
        "BYBIT_V5_URLS",
        "https://api.bybit.com,https://api.bytick.com,https://api.bybit.eu,https://api.bybit.ae,https://api.bybit.id,https://api.bybit.kz,https://api.bybit-tr.com,https://api.byhkbit.com,https://api.bybitgeorgia.ge",
    ).split(",") if x.strip()
]

# Render/free cloud IPs can be geo-blocked by exchange APIs. These public
# read-only relays are only used after every direct exchange endpoint fails.
# Override with MARKET_PROXY_URLS if a private/managed relay is preferred.
MARKET_PROXY_URLS = [
    x.strip() for x in os.getenv(
        "MARKET_PROXY_URLS",
        "https://api.allorigins.win/raw?url={url},https://corsproxy.io/?url={url}",
    ).split(",") if x.strip()
]

if os.getenv("BINANCE_FAPI_URL"):
    chosen = os.getenv("BINANCE_FAPI_URL", "").rstrip("/")
    BINANCE_BASE_URLS = [chosen] + [x for x in BINANCE_BASE_URLS if x != chosen]
if os.getenv("BYBIT_V5_URL"):
    chosen = os.getenv("BYBIT_V5_URL", "").rstrip("/")
    BYBIT_BASE_URLS = [chosen] + [x for x in BYBIT_BASE_URLS if x != chosen]


def _validate_pair(pair: str) -> str:
    symbol = (pair or DEFAULT_PAIR).upper().replace("/", "")
    if symbol != DEFAULT_PAIR:
        raise ValueError("This service is restricted to BTCUSDT perpetual only")
    return DEFAULT_PAIR


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict:
    response = await client.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected response from {url}: {data}")
    return data


async def _get_json_failover(
    client: httpx.AsyncClient,
    urls: list[str],
    path: str,
    params: dict[str, Any],
) -> dict:
    errors: list[str] = []

    # 1) Try official exchange endpoints directly.
    for base_url in urls:
        try:
            return await _get_json(client, f"{base_url}{path}", params)
        except Exception as exc:
            errors.append(f"{base_url}: {exc}")

    # 2) If Render's egress IP is blocked/geo-restricted, retry through a
    # read-only HTTP relay. The exchange URL and query are preserved exactly.
    query = urlencode(params)
    for base_url in urls:
        target = f"{base_url}{path}?{query}"
        encoded_target = quote(target, safe="")
        for template in MARKET_PROXY_URLS:
            try:
                if "{url}" not in template:
                    continue
                proxy_url = template.replace("{url}", encoded_target)
                response = await client.get(proxy_url)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("relay returned non-object JSON")
                return data
            except Exception as exc:
                errors.append(f"relay {template.split('?')[0]} -> {base_url}: {exc}")

    raise RuntimeError("All public exchange endpoints and read-only relays failed: " + " | ".join(errors))


async def fetch_klines(exchange: str, pair: str, interval: str, limit: int = 300) -> list[dict]:
    exchange = exchange.upper()
    symbol = _validate_pair(pair)
    if exchange not in EXCHANGES:
        raise ValueError("Exchange must be BINANCE or BYBIT")
    if interval not in VALID_INTERVALS:
        raise ValueError("Interval must be one of: 5m, 15m, 1h, 4h")

    limit = min(max(int(limit), 50), 1000)
    bybit_interval = {"5m": "5", "15m": "15", "1h": "60", "4h": "240"}[interval]
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SharkBTC-SMC/1.0)",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as client:
        if exchange == "BINANCE":
            data = await _get_json_failover(
                client, BINANCE_BASE_URLS, "/fapi/v1/klines",
                {"symbol": symbol, "interval": interval, "limit": limit},
            )
            rows = data
        else:
            data = await _get_json_failover(
                client, BYBIT_BASE_URLS, "/v5/market/kline",
                {"category": "linear", "symbol": symbol, "interval": bybit_interval, "limit": limit},
            )
            if data.get("retCode") not in (None, 0):
                raise ValueError(f"Bybit error: {data.get('retMsg')}")
            rows = data.get("result", {}).get("list", [])

    candles: list[dict] = []
    for row in rows or []:
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
    if exchange not in EXCHANGES:
        raise ValueError("Exchange must be BINANCE or BYBIT")
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SharkBTC-SMC/1.0)",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
        if exchange == "BINANCE":
            data = await _get_json_failover(client, BINANCE_BASE_URLS, "/fapi/v1/ticker/price", {"symbol": symbol})
            return float(data["price"])
        data = await _get_json_failover(client, BYBIT_BASE_URLS, "/v5/market/tickers", {"category": "linear", "symbol": symbol})
        return float(data["result"]["list"][0]["lastPrice"])


async def _run_one(exchange: str, pair: str = DEFAULT_PAIR) -> dict:
    import asyncio
    symbol = _validate_pair(pair)
    h4, h1, m15, m5 = await asyncio.gather(
        fetch_klines(exchange, symbol, "4h"), fetch_klines(exchange, symbol, "1h"),
        fetch_klines(exchange, symbol, "15m"), fetch_klines(exchange, symbol, "5m"),
    )
    live = await fetch_live_price(exchange, symbol)
    analysis = analyze_advanced(h4, h1, m15, m5, live)
    return {"exchange": exchange, "pair": symbol, "contract_type": CONTRACT_TYPE, "engine": "ADVANCED_SMC", **analysis}


def _normalize_exchange(value: str | None) -> str:
    raw = (value or "BOTH").strip().upper()
    compact = raw.replace(" ", "").replace("%20", "")
    if compact in {"", "BOTH", "BINANCE+BYBIT", "BYBIT+BINANCE", "BINANCE,BYBIT", "BYBIT,BINANCE", "BINANCEBYBIT", "BYBITBINANCE"}:
        return "BOTH"
    return raw


async def _run_smc(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    import asyncio
    symbol = _validate_pair(pair)
    exchange = _normalize_exchange(exchange)
    if exchange == "BOTH":
        results = await asyncio.gather(_run_one("BINANCE", symbol), _run_one("BYBIT", symbol), return_exceptions=True)
        output: dict[str, Any] = {}
        for name, result in zip(("BINANCE", "BYBIT"), results):
            output[name] = {"error": str(result)} if isinstance(result, Exception) else result
        b, y = output.get("BINANCE", {}), output.get("BYBIT", {})
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


@mcp.tool
async def get_btcusdt_perpetual_smc_analysis(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    """BTCUSDT Perpetual advanced SMC setup using Binance USD-M and/or Bybit Linear."""
    return await _run_smc(exchange, pair)


@mcp.tool
async def get_btc_smc_analysis(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    """Compatibility alias for the BTCUSDT perpetual advanced SMC analysis."""
    return await _run_smc(exchange, pair)


@mcp.tool
async def get_btcusdt_perpetual_market_data(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    """Return normalized closed BTCUSDT perpetual candles plus live price for 4H/1H/15M/5M."""
    symbol = _validate_pair(pair)

    async def one(ex: str) -> dict:
        import asyncio
        live, h4, h1, m15, m5 = await asyncio.gather(
            fetch_live_price(ex, symbol), fetch_klines(ex, symbol, "4h"), fetch_klines(ex, symbol, "1h"),
            fetch_klines(ex, symbol, "15m"), fetch_klines(ex, symbol, "5m"),
        )
        return {"exchange": ex, "pair": symbol, "contract_type": CONTRACT_TYPE, "live_price": live, "4h": h4, "1h": h1, "15m": m15, "5m": m5}

    if _normalize_exchange(exchange) == "BOTH":
        import asyncio
        results = await asyncio.gather(one("BINANCE"), one("BYBIT"), return_exceptions=True)
        return {
            "BINANCE": results[0] if not isinstance(results[0], Exception) else {"error": str(results[0])},
            "BYBIT": results[1] if not isinstance(results[1], Exception) else {"error": str(results[1])},
        }
    return await one(_normalize_exchange(exchange))


async def health(request):
    return JSONResponse({"status": "ok", "service": "Shark BTC Advanced SMC", "pair": DEFAULT_PAIR, "contract_type": CONTRACT_TYPE, "exchanges": ["BINANCE", "BYBIT"], "public_api": "/api/smc?exchange=BOTH&pair=BTCUSDT"})


async def public_smc(request):
    try:
        exchange = request.query_params.get("exchange", "BOTH")
        pair = request.query_params.get("pair", DEFAULT_PAIR)
        result = await _run_smc(exchange, pair)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)


public_app = Starlette(routes=[
    Route("/", health, methods=["GET"]),
    Route("/health", health, methods=["GET"]),
    Route("/api/smc", public_smc, methods=["GET"]),
    Mount("/mcp", app=mcp.http_app(path="/mcp", transport="streamable-http")),
])


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(public_app, host="0.0.0.0", port=port)
