# Shark BTC SMC MCP Bridge

This bridge reads Shark Exchange public Kline data and exposes it as MCP tools.

## Important
- This project is READ-ONLY.
- It does not place orders, withdraw funds, or use an API secret.
- Confirm the exact Shark Futures symbol before deployment. Set `SHARK_PAIR` accordingly if it is not `BTCUSDT`.

## Tools
- get_btc_4h
- get_btc_1h
- get_btc_15m
- get_btc_5m
- get_btc_smc_data

## Local test
1. Install Python 3.11+.
2. `pip install -r requirements.txt`
3. Set the correct pair:
   - Linux/macOS: `export SHARK_PAIR=YOUR_EXACT_FUTURES_SYMBOL`
   - Windows PowerShell: `$env:SHARK_PAIR="YOUR_EXACT_FUTURES_SYMBOL"`
4. Run: `python app.py`
5. The MCP server listens on port 8000 using Streamable HTTP.

## Deploy
Deploy this folder to a host that provides a public HTTPS URL (for example, Render).
ChatGPT custom MCP apps require a remote MCP server; local servers are not directly connected.

## SMC workflow
The ChatGPT-side instructions should be:
4H bias -> 1H BOS/CHOCH and liquidity -> 15M POI -> 5M liquidity sweep + MSS + displacement + FVG/OB retest -> LONG/SHORT/WAIT.

Never treat a single sweep, MSS or FVG as sufficient confirmation.
