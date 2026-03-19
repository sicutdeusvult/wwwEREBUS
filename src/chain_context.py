"""
chain_context.py — fetches live Solana/market data to inject into erebus's context.
Uses JUPITER_API_KEY and RPC_URL (Helius) from env.
Runs before each decision cycle.
"""

import os, json, time, requests
from functools import lru_cache

JUPITER_API_KEY = os.environ.get("JUPITER_API_KEY", "")
RPC_URL         = os.environ.get("RPC_URL", "https://api.mainnet-beta.solana.com")
erebus_WALLET     = "HoFMgyue2HZ8kCYJ81b1Yg34AZZ7g8B7eFZ35nFqQYpW"

# Key token mints
MINTS = {
    "SOL":  "So11111111111111111111111111111111111111112",
    "JUP":  "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF":  "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "POPCAT":"7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
    "MOODENG":"ED5nyyWEzpPPiWimP8vYm7sD7TD3LAt3Q3gRTWHzc8yy",
}

HEADERS = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}

_price_cache = {}
_price_ts = 0
_PRICE_TTL = 60  # seconds

def get_prices() -> dict:
    """Fetch USD prices for key tokens via Jupiter Price API v3."""
    global _price_cache, _price_ts
    if time.time() - _price_ts < _PRICE_TTL and _price_cache:
        return _price_cache
    try:
        ids = ",".join(MINTS.values())
        r = requests.get(
            f"https://api.jup.ag/price/v3?ids={ids}",
            headers=HEADERS,
            timeout=2
        )
        data = r.json().get("data", {})
        result = {}
        for sym, mint in MINTS.items():
            if mint in data:
                result[sym] = data[mint].get("price", 0)
        _price_cache = result
        _price_ts = time.time()
        return result
    except Exception as e:
        return _price_cache or {}

def get_wallet_holdings() -> dict:
    """Fetch erebus wallet token holdings via Helius DAS API."""
    try:
        r = requests.post(
            RPC_URL,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    erebus_WALLET,
                    {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                    {"encoding": "jsonParsed"}
                ]
            },
            timeout=2
        )
        accounts = r.json().get("result", {}).get("value", [])
        holdings = []
        for acc in accounts:
            info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            mint = info.get("mint", "")
            amount = info.get("tokenAmount", {})
            ui_amount = amount.get("uiAmount", 0) or 0
            if ui_amount > 0:
                holdings.append({"mint": mint[:8]+"...", "amount": ui_amount})
        return {"holdings": holdings[:5]}
    except Exception:
        return {}

def get_sol_balance() -> float:
    """Fetch erebus SOL balance."""
    try:
        r = requests.post(
            RPC_URL,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getBalance",
                "params": [erebus_WALLET]
            },
            timeout=2
        )
        lamports = r.json().get("result", {}).get("value", 0)
        return lamports / 1e9
    except Exception:
        return 0.0

def get_trending_tokens() -> list:
    """Fetch trending/new tokens from Jupiter tokens API."""
    try:
        r = requests.get(
            "https://api.jup.ag/tokens/v2/search?query=&tags=pump&limit=5",
            headers=HEADERS,
            timeout=2
        )
        tokens = r.json() if isinstance(r.json(), list) else []
        return [
            {"symbol": t.get("symbol","?"), "name": t.get("name","?")[:20]}
            for t in tokens[:5]
        ]
    except Exception:
        return []


# ── Pump.fun token analysis via Jupiter datapi ─────────────────────────────

EREBUS_CA = "4RKfEKd6H7TDDKGfyp7Xt4qoGiGy56KkR9nLcVaXpump"
_PUMP_ADDR_RE = None

def _pump_re():
    global _PUMP_ADDR_RE
    if _PUMP_ADDR_RE is None:
        import re
        _PUMP_ADDR_RE = re.compile(r'\b([1-9A-HJ-NP-Za-km-z]{32,44}pump)\b')
    return _PUMP_ADDR_RE

def extract_pump_addresses(text: str) -> list[str]:
    """Extract pump.fun contract addresses (end in 'pump') from text."""
    return list(set(_pump_re().findall(text)))

def lookup_token(mint: str) -> dict | None:
    """Fetch token data from Jupiter datapi for any pump.fun token."""
    try:
        r = requests.get(
            f"https://datapi.jup.ag/v1/assets/search?query={mint}",
            timeout=4
        )
        if not r.ok:
            return None
        data = r.json()
        # datapi returns a list; find exact mint match
        items = data if isinstance(data, list) else data.get("data", [])
        for item in items:
            if item.get("id") == mint:
                return item
        return items[0] if items else None
    except Exception:
        return None

def format_token_analysis(token: dict, mint: str) -> str:
    """Format token data into a compact context block for the agent."""
    if not token:
        return ""

    name    = token.get("name", "?")
    symbol  = token.get("symbol", "?")
    price   = token.get("usdPrice", 0) or 0
    mcap    = token.get("mcap", 0) or 0
    holders = token.get("holderCount", 0) or 0
    organic = token.get("organicScore", 0) or 0
    organic_label = token.get("organicScoreLabel", "?")
    liq     = token.get("liquidity", 0) or 0
    bonding = token.get("bondingCurve", 0) or 0
    bots    = token.get("botHoldersPercentage", 0) or 0
    top_h   = token.get("topHoldersPercentage", 0) or 0
    dev_bal = token.get("devBalancePercentage", 0) or 0
    tags    = token.get("tags", [])

    # 24h stats
    s24 = token.get("stats24h") or token.get("stats", {})
    if isinstance(s24, dict) and "priceChange" not in s24:
        s24 = token.get("stats", {}).get("24h", {}) or {}
    price_chg = s24.get("priceChange", 0) or 0
    buys_24   = s24.get("numBuys", 0) or 0
    sells_24  = s24.get("numSells", 0) or 0
    traders   = s24.get("numTraders", 0) or 0
    buy_vol   = s24.get("buyVolume", 0) or 0
    sell_vol  = s24.get("sellVolume", 0) or 0

    is_own = (mint == EREBUS_CA)
    own_note = " [THIS IS YOUR OWN TOKEN]" if is_own else ""

    lines = [
        f"[TOKEN ANALYSIS]{own_note}",
        f"name: {name} (${symbol}) | mint: {mint[:12]}...",
        f"price: ${price:.8f} | mcap: ${mcap:,.0f} | liquidity: ${liq:,.0f}",
        f"holders: {holders} | top10%: {top_h:.1f}% | bots: {bots:.1f}% | dev_bal: {dev_bal:.4f}%",
        f"24h: {price_chg:+.1f}% | buys: {buys_24} | sells: {sells_24} | traders: {traders}",
        f"buy_vol: ${buy_vol:,.0f} | sell_vol: ${sell_vol:,.0f}",
        f"organic_score: {organic} ({organic_label}) | bonding_curve_fill: {bonding:.1f}%",
        f"cashback: {'yes' if 'token-2022' in tags else 'no'} | launchpad: {token.get('launchpad','?')}",
        f"[/TOKEN ANALYSIS]",
    ]
    return "\n".join(lines)

def build_chain_context() -> str:
    """Build compact chain state — prices only, fast, no wallet RPC calls."""
    try:
        prices = get_prices()  # cached, fast
    except Exception:
        return ""

    if not prices:
        return ""

    price_strs = []
    for sym in ["SOL", "JUP", "BONK", "WIF"]:
        if sym in prices and prices[sym]:
            p = prices[sym]
            price_strs.append(f"{sym}=${p:.2f}" if p >= 1 else f"{sym}=${p:.6f}")

    if not price_strs:
        return ""

    return "[CHAIN PRICES] " + " | ".join(price_strs) + " [/CHAIN PRICES]" 


if __name__ == "__main__":
    print(build_chain_context())
