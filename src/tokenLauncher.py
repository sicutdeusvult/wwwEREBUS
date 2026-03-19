"""
tokenLauncher.py — X-triggered Solana token deployment for EREBUS / @wwwEREBUS

Flow:
  1. A mention comes in: "@wwwEREBUS launch name: TEST symbol: TEST" + optional image
  2. detect_launch_intent() parses it
  3. If valid → fetch image from tweet media → POST to launchpad /create-from-agent
  4. On success → reply to the user with mint address + solscan link
  5. Award 10 points to the X handle in /data/x_points.json

ENV required (same .env as the rest of the agent):
  LAUNCHPAD_URL       — base URL of the Node.js launchpad server (e.g. http://localhost:3000)
  AGENT_SECRET        — shared secret so launchpad accepts agent-triggered deploys
"""

import os, re, json, time, requests
import sys
sys.path.append(os.path.abspath('.'))
from src.logs import logs

POINTS_FILE = os.path.join(
    os.getenv("RENDER_DATA_DIR", "data"),
    "x_points.json"
)
DEPLOY_POINTS = 10


# ── Regex patterns for flexible natural-language parsing ──────────────────────

_LAUNCH_TRIGGER = re.compile(r'\b(launch|deploy|create|mint)\b', re.IGNORECASE)

# Pump.fun specific triggers — separate so we know which platform to use
# Examples: "pump PEPE $PEPE", "pumpfun DOGE DOGE", "pump.fun deploy FIRE"
_PUMP_TRIGGER = re.compile(
    r'\b(pump\.?fun|pumpfun)\b'                             # "pump.fun" / "pumpfun"
    r'|'
    r'\bpump\b(?!\s*\.\s*\w)'                               # bare "pump" (not "pump.fun" already caught)
    r'(?=\s+(?:deploy|launch|create|mint|[A-Za-z0-9]))',    # must be followed by a word
    re.IGNORECASE
)

erebus_WALLET = os.getenv("erebus_WALLET", "HoFMgyue2HZ8kCYJ81b1Yg34AZZ7g8B7eFZ35nFqQYpW")

# Fee wallet: Solana base58 pubkey (32-44 chars)
_FEE_WALLET_PATTERN = re.compile(
    r'(?:share\s+fees?\s+to|fee\s*wallet\s*[=:]?|tip\s*wallet\s*[=:]?|fees?\s+to)\s*([A-Za-z0-9]{32,44})',
    re.IGNORECASE
)
# Fee handle: @username  OR  discord:user  OR  github:user  OR  twitch:user
# Examples:
#   share fees to @user2
#   share fees to discord:user2
#   share fees to github:devguy
#   share fees to twitch:streamer99
_FEE_HANDLE_PATTERN = re.compile(
    r'(?:share\s+fees?\s+to|fee\s*to|fees?\s+to)\s*'
    r'(?:@([A-Za-z0-9_]{1,50})'                        # group 1: X handle (@user)
    r'|(discord|github|twitch):([A-Za-z0-9_]{1,50}))', # group 2: platform, group 3: identity
    re.IGNORECASE
)
# Fee clause as a whole — stripped before name/symbol parsing so it doesn't pollute
_FEE_CLAUSE_PATTERN = re.compile(
    r'\s*(?:share\s+fees?\s+to|fee\s*wallet\s*[=:]?|tip\s*wallet\s*[=:]?|fees?\s+to)\s*'
    r'(?:@[A-Za-z0-9_]{1,50}|(?:discord|github|twitch):[A-Za-z0-9_]{1,50}|[A-Za-z0-9]{32,44})',
    re.IGNORECASE
)
# Words that are filler/noise — not a token name
_SKIP_WORDS = re.compile(
    r'^(?:me|a|an|the|token|coin|some|new|my|our|this|that|called|named|'
    r'for|please|pls|it|up|out|just|quick|fast)$',
    re.IGNORECASE
)
# Trailing noise that can appear after the token name in shorthand
_NAME_TRAILING_NOISE = re.compile(
    r'\s+(?:for\s+me|please|pls|now|asap|today|thanks?|thx|with\s+ticker.*|'
    r'with\s+symbol.*|ticker\s+\S+.*|symbol\s+\S+.*)$',
    re.IGNORECASE
)


def _parse_launch_intent(text: str) -> dict | None:
    """
    Robust parser for all token launch formats. Handles:
      @wwwEREBUS launch erebus
      @wwwEREBUS deploy FIRE
      @wwwEREBUS launch erebusCoin $erebus
      @wwwEREBUS deploy FireDoge FIRE
      @wwwEREBUS launch name: erebusCoin symbol: erebus
      @wwwEREBUS deploy token name=FireDoge, symbol=FIRE
      @wwwEREBUS create a token called PEPE ticker PEPE
      @wwwEREBUS launch erebus share fees to <wallet>
      @wwwEREBUS deploy FIRE fee wallet: <wallet>
      @wwwEREBUS launch erebusCoin $erebus tip wallet <wallet>
      @wwwEREBUS launch erebus share fees to @user2
      + typos, extra spaces, newlines, mixed case, natural language
    Returns {name, symbol, fee_wallet, fee_handle} or None.
    """
    if not _LAUNCH_TRIGGER.search(text):
        return None

    # ── Strip fee clause BEFORE name/symbol parsing ───────────────────
    # Prevents "share fees to @user2" bleeding into name capture
    clean = _FEE_CLAUSE_PATTERN.sub('', text)
    # Strip @mentions and normalize whitespace
    clean = re.sub(r'@\w+\s*', '', clean).strip()
    clean = re.sub(r'[\s\n\r\t]+', ' ', clean).strip()

    name   = None
    symbol = None

    # ── Priority 1: Explicit name/symbol pairs ────────────────────────
    # Handles: name: X symbol: Y / name=X, symbol=Y / token: X ticker: Y
    name_m = re.search(
        r'(?:^|[\s,|])(?:name|token)\s*[=:]\s*'
        r'([A-Za-z0-9][A-Za-z0-9 _\-]{0,28}?)'
        r'(?=\s*[,|]|\s+(?:symbol|ticker)|\s*$)',
        clean, re.IGNORECASE
    )
    symbol_m = re.search(
        r'(?:symbol|ticker)\s*[=:]?\s*\$([A-Za-z0-9]{1,10})'   # with $ prefix
        r'|(?:symbol|ticker)\s*[=:]\s*([A-Za-z0-9]{1,10})',     # or with explicit = or :
        clean, re.IGNORECASE
    )
    if name_m:
        name = name_m.group(1).strip()
    if symbol_m:
        symbol = (symbol_m.group(1) or symbol_m.group(2) or '').strip().upper()
    if name and not symbol:
        symbol = re.sub(r'\s+', '', name)[:10].upper()

    # If we got a symbol but no name, try to extract name as the last word(s) before
    # the symbol label — handles "deploy erebus_terminal symbol: $erebus"
    if symbol and not name:
        pre_sym = re.split(r'\b(?:symbol|ticker)\b', clean, flags=re.IGNORECASE)[0]
        pre_sym = pre_sym.strip().rstrip(',').strip()
        # Extract last meaningful word after the trigger
        pre_m = re.search(
            r'\b(?:launch|deploy|create|mint)\b\s+'
            r'(?:(?:me|a|an|the|token|coin|new|my|some)\s+)*'
            r'([A-Za-z0-9][A-Za-z0-9 _\-]{0,28}?)\s*$',
            pre_sym, re.IGNORECASE
        )
        if pre_m:
            candidate = pre_m.group(1).strip()
            if not _SKIP_WORDS.match(candidate):
                name = candidate

    # ── Priority 2: "called X" / "named X" with optional ticker ──────
    if not name:
        called_m = re.search(
            r'\b(?:called|named)\s+([A-Za-z0-9][A-Za-z0-9_\-]{0,28})',
            clean, re.IGNORECASE
        )
        if called_m:
            name = called_m.group(1).strip()
            tick_m = re.search(
                r'\b(?:ticker|symbol)\b\s*[=:]?\s*\$?([A-Za-z0-9]{1,10})',
                clean, re.IGNORECASE
            )
            symbol = tick_m.group(1).strip().upper() if tick_m else re.sub(r'\s+', '', name)[:10].upper()

    # ── Priority 3: "launch Name $SYMBOL" shorthand ───────────────────
    # Also handles "deploy Name symbol $SYMBOL" / "deploy Name ticker $SYMBOL"
    if not name:
        sh = re.search(
            r'\b(?:launch|deploy|create|mint)\b\s+'
            r'(?:(?:me|a|an|the|token|coin|new|my|some)\s+)*'
            r'([A-Za-z0-9][A-Za-z0-9 _\-]{0,28}?)\s+'
            r'(?:symbol|ticker)?\s*'           # optional "symbol" / "ticker" word before $
            r'\$([A-Za-z0-9]{1,10})'           # $ required to distinguish name vs symbol
            r'(?:\s|$)',
            clean, re.IGNORECASE
        )
        if sh:
            cname = sh.group(1).strip()
            if not _SKIP_WORDS.match(cname):
                name   = cname
                symbol = sh.group(2).strip().upper()

    # ── Priority 4: "launch Name SYMBOL" (no $) — both words present ─
    # Also handles "Name ticker SYMBOL" / "Name symbol SYMBOL"
    if not name:
        sh2 = re.search(
            r'\b(?:launch|deploy|create|mint)\b\s+'
            r'(?:(?:me|a|an|the|token|coin|new|my|some)\s+)*'
            r'([A-Za-z0-9][A-Za-z0-9_\-]{1,28})\s+'   # NAME — single word (underscores ok)
            r'(?:(?:symbol|ticker)\s*[=:]?\s*)?'        # optional "symbol"/"ticker" label
            r'([A-Za-z0-9]{1,10})'                      # SYMBOL
            r'(?:\s|$)',
            clean, re.IGNORECASE
        )
        if sh2:
            cname = sh2.group(1).strip()
            csym  = sh2.group(2).strip().upper()
            # Reject if either is a skip/noise word
            if not _SKIP_WORDS.match(cname) and not _SKIP_WORDS.match(csym):
                name   = cname
                symbol = csym

    # ── Priority 5: Single word → name = symbol ──────────────────────
    if not name:
        sg = re.search(
            r'\b(?:launch|deploy|create|mint)\b\s+'
            r'(?:(?:me|a|an|the|token|coin|new|my|some|called|named|please|pls)\s+)*'
            r'([A-Za-z0-9][A-Za-z0-9_\-]{1,19})'
            r'(?:\s|$)',
            clean, re.IGNORECASE
        )
        if sg:
            candidate = sg.group(1).strip()
            if not _SKIP_WORDS.match(candidate):
                name   = candidate
                symbol = candidate.upper()

    if not name or not symbol:
        return None

    # ── Final cleanup ─────────────────────────────────────────────────
    name   = _NAME_TRAILING_NOISE.sub('', name).strip().strip('.,').strip()
    symbol = symbol.upper().strip().strip('.,').strip()[:10]

    if len(name) < 1 or len(symbol) < 1:
        return None
    if _SKIP_WORDS.match(name):
        return None

    # ── Parse fee override ────────────────────────────────────────────
    fee_m        = _FEE_WALLET_PATTERN.search(text)
    fee_handle_m = _FEE_HANDLE_PATTERN.search(text)
    fee_wallet   = fee_m.group(1) if fee_m else None
    if fee_handle_m:
        if fee_handle_m.group(1):
            # @username → plain X handle (backwards compat)
            fee_handle = fee_handle_m.group(1).lower()
        else:
            # platform:identity → "discord:293847", "github:devguy", etc.
            fee_handle = f"{fee_handle_m.group(2).lower()}:{fee_handle_m.group(3).lower()}"
    else:
        fee_handle = None

    return {"name": name, "symbol": symbol, "fee_wallet": fee_wallet, "fee_handle": fee_handle}


class tokenLauncher:
    def __init__(self):
        self.logs = logs()
        self.launchpad_url    = os.getenv("LAUNCHPAD_URL", "http://localhost:3000")
        self.launchpad_secret = os.getenv("AGENT_SECRET", "")

    # ── Public API ────────────────────────────────────────────────────────────

    def detect_launch_intent(self, text: str) -> dict | None:
        """
        Return {"name": ..., "symbol": ..., "platform": "meteora"} if the tweet looks like a
        Meteora token launch request, otherwise None.
        Does NOT match pump.fun triggers — those go to detect_pump_intent().
        """
        if _PUMP_TRIGGER.search(text):
            return None  # pump.fun path — handled separately
        result = _parse_launch_intent(text)
        if result:
            result['platform'] = 'meteora'
        return result

    def detect_pump_intent(self, text: str) -> dict | None:
        """
        Return {"name": ..., "symbol": ..., "platform": "pumpfun", ...} if the tweet is a
        pump.fun launch request. Triggers: pump / pumpfun / pump.fun keywords.
        Examples:
          @wwwEREBUS pump PEPE $PEPE
          @wwwEREBUS pumpfun deploy: PEPE, symbol: PEPE
          @wwwEREBUS pump.fun FIRE FIRE share fees to @user2
        """
        if not _PUMP_TRIGGER.search(text):
            return None
        # Strip pump trigger word(s)
        cleaned = _PUMP_TRIGGER.sub('', text).strip()
        # Normalize "deploy: X" / "launch: X" / "create: X" → "launch X"
        # (colon after verb is valid user shorthand but breaks the parser)
        cleaned = re.sub(r'\b(launch|deploy|create|mint)\s*:', 'launch', cleaned, flags=re.IGNORECASE)
        # Strip bare @mentions (the agent handle, etc.)
        cleaned = re.sub(r'@\w+\s*', '', cleaned).strip()
        # Inject "launch" trigger if not already present
        if not _LAUNCH_TRIGGER.search(cleaned):
            cleaned = 'launch ' + cleaned
        result = _parse_launch_intent(cleaned)
        if result:
            result['platform'] = 'pumpfun'
            # Detect cashback keyword anywhere in the original text
            result['cashback'] = bool(re.search(r'\bcashback\b', text, re.IGNORECASE))
            # Detect fee_wallet: "share fees to <base58>" (Solana address, 32-44 chars)
            fw_m = re.search(
                r'share\s+fees?\s+to\s+([1-9A-HJ-NP-Za-km-z]{32,44})(?:\s|$)',
                text, re.IGNORECASE
            )
            result['fee_wallet'] = fw_m.group(1) if fw_m else None
        return result

    def pump_deploy(self, handle: str, name: str, symbol: str,
                    image_url: str | None = None,
                    tweet_url: str | None = None,
                    deployer_secret: list | None = None,
                    cashback: bool = False,
                    fee_wallet: str | None = None) -> dict:
        """
        Deploy a token on pump.fun via the launchpad /pump-from-agent endpoint.
        Returns {"success": True, "mint": ..., "pumpfun": ..., "solscan": ...}
        or {"success": False, "error": ...}.
        """
        try:
            image_data = None
            if image_url:
                image_data = self._fetch_image_bytes(image_url)

            payload = {
                "name":             name,
                "symbol":           symbol,
                "description":      f"launched via @wwwEREBUS by @{handle} on pump.fun",
                "twitter_url":      tweet_url or f"https://x.com/{handle}",
                "secret":           self.launchpad_secret,
                "deployer_handle":  handle,
                "cashback":         "true" if cashback else "false",
                "fee_wallet":       fee_wallet or "",
            }
            if deployer_secret:
                payload["deployer_secret"] = deployer_secret  # list of ints

            if image_data:
                import json as _json
                resp = requests.post(
                    f"{self.launchpad_url}/pump-from-agent",
                    data={**payload, "deployer_secret": _json.dumps(deployer_secret) if deployer_secret else ""},
                    files={"image": ("logo.jpg", image_data, "image/jpeg")},
                    timeout=90,
                )
            else:
                resp = requests.post(
                    f"{self.launchpad_url}/pump-from-agent",
                    json=payload,
                    timeout=90,
                )

            if resp.status_code != 200:
                err = resp.json().get("error", resp.text[:120])
                self.logs.log_error(f"pump_deploy failed {resp.status_code}: {err}")
                return {"success": False, "error": err}

            data = resp.json()
            mint = data.get("baseMint", "")
            self.logs.log_info(
                f"pump.fun token deployed: {name} ({symbol}) mint={mint[:12]}...",
                "bold green", "PumpLaunch"
            )
            self._award_points(handle, DEPLOY_POINTS)
            return {
                "success":     True,
                "mint":        mint,
                "solscan":     f"https://solscan.io/token/{mint}",
                "pumpfun":     f"https://pump.fun/coin/{mint}",
                "name":        name,
                "symbol":      symbol,
                "tweet_url":   tweet_url or "",
                "platform":    "pumpfun",
                "cashback":    cashback,
                "initialBuySol": data.get("initialBuySol", 0.001),
                "feeWallet":   data.get("feeWallet") or fee_wallet,
                "feeShareSig": data.get("feeShareSig"),
            }

        except Exception as e:
            self.logs.log_error(f"tokenLauncher.pump_deploy: {e}")
            return {"success": False, "error": str(e)}

    def build_pump_reply(self, handle: str, result: dict) -> str:
        """Build the reply tweet text after a pump.fun deploy attempt."""
        if result["success"]:
            cashback_tag = " | cashback enabled 💸" if result.get("cashback") else ""
            fee_tag = ""
            if result.get("feeShareSig") and result.get("feeWallet"):
                fw = result["feeWallet"]
                fee_tag = f" | fees split 50/50 → {fw[:8]}..."
            return (
                f"@{handle} "
                f"name: {result['name']} | "
                f"symbol: {result['symbol']} | "
                f"launched on pump.fun 🟢{cashback_tag}{fee_tag} | "
                f"{result['pumpfun']} | "
                f"{result.get('initialBuySol', 0.001)} SOL initial buy"
            )
        else:
            err = result.get("error", "unknown error")
            if "Connection refused" in err or "unreachable" in err or "localhost" in err:
                friendly = "launchpad offline. try again soon."
            elif "already exists" in err:
                friendly = "that name or symbol already exists on pump.fun."
            elif "forbidden" in err.lower() or "Unauthorized" in err:
                friendly = "pump.fun rejected the deploy."
            elif "API error" in err:
                friendly = "pump.fun API error. try again in a moment."
            else:
                friendly = err[:60] if len(err) <= 60 else "pump.fun deploy failed. try again."
            return f"@{handle} {friendly}"

    def deploy(self, handle: str, name: str, symbol: str,
               image_url: str | None = None,
               tweet_url: str | None = None,
               fee_wallet: str | None = None,
               fee_handle: str | None = None,
               pool_creator_wallet: str | None = None,
               pool_creator_secret: list | None = None,
               deployer_secret: list | None = None) -> dict:
        """
        Call the launchpad's /create-from-agent endpoint.
        deployer_secret     — payer's secret key bytes (user1 — pays gas, signs tx).
        pool_creator_wallet — user2's pubkey (earns creator fees as poolCreator).
        pool_creator_secret — user2's secret key bytes — REQUIRED when pool_creator_wallet
                              is set because Solana requires poolCreator to sign createPool.
                              Both user1 and user2 sign; user1 pays, user2 is fee recipient.
        fee_handle          — X handle of the fee recipient (DB display only).
        fee_wallet          — kept for DB storage only.
        Returns {"success": True, "mint": "...", "solscan": "..."} or {"success": False, "error": "..."}.
        """
        try:
            image_data = None
            if image_url:
                image_data = self._fetch_image_bytes(image_url)

            payload = {
                "name":                 name,
                "symbol":               symbol,
                "description":          f"launched via @wwwEREBUS by @{handle}",
                "twitter_url":          tweet_url or f"https://x.com/{handle}",
                "secret":               self.launchpad_secret,
                "deployer_handle":      handle,
                "fee_wallet":           fee_wallet or erebus_WALLET,
            }

            # "share fees to @user2" — pass user2's wallet as poolCreator
            # user2's secret is also required so launchpad can co-sign (Solana requires
            # poolCreator to sign the createPool instruction)
            if pool_creator_wallet:
                payload["pool_creator_wallet"] = pool_creator_wallet
            if pool_creator_secret:
                payload["pool_creator_secret"] = pool_creator_secret

            # Store fee_handle in DB for dashboard display
            if fee_handle:
                payload["fee_handle"] = fee_handle

            # If we have the user's server-wallet secret, pass it so the
            # launchpad uses their wallet as payer
            if deployer_secret:
                payload["deployer_secret"] = deployer_secret  # list of ints

            if image_data:
                resp = requests.post(
                    f"{self.launchpad_url}/create-from-agent",
                    data={**payload, "deployer_secret": json.dumps(deployer_secret) if deployer_secret else ""},
                    files={"image": ("logo.jpg", image_data, "image/jpeg")},
                    timeout=60
                )
            else:
                resp = requests.post(
                    f"{self.launchpad_url}/create-from-agent",
                    json=payload,
                    timeout=60
                )

            if resp.status_code != 200:
                err = resp.json().get("error", resp.text[:120])
                self.logs.log_error(f"tokenLauncher deploy failed {resp.status_code}: {err}")
                return {"success": False, "error": err}

            data = resp.json()
            mint = data.get("baseMint", "")
            self.logs.log_info(
                f"token deployed: {name} ({symbol}) mint={mint[:12]}...",
                "bold green", "Launch"
            )
            self._award_points(handle, DEPLOY_POINTS)
            return {
                "success":   True,
                "mint":      mint,
                "solscan":   f"https://solscan.io/token/{mint}",
                "name":      name,
                "symbol":    symbol,
                "tweet_url": tweet_url or "",
                "feeGift":   data.get("feeGift", False),
                "feeHandle": data.get("feeHandle") or fee_handle,
                "poolCreator": data.get("poolCreator", ""),
            }

        except Exception as e:
            self.logs.log_error(f"tokenLauncher.deploy: {e}")
            return {"success": False, "error": str(e)}

    def build_reply(self, handle: str, result: dict) -> str:
        """Build the reply tweet text after deploy attempt."""
        if result["success"]:
            base = (
                f"@{handle} "
                f"name: {result['name']} | "
                f"symbol: {result['symbol']} | "
                f"deployed. {result['solscan']} | "
                f"{result.get('initialBuySol', 0.001)} SOL initial buy"
            )
            # Add fee-gift notice if fees were directed to another user
            fee_handle = result.get("feeHandle") or result.get("fee_handle")
            if result.get("feeGift") and fee_handle:
                # "discord:username" → "discord:username"  |  "username" → "@username"
                if ":" in fee_handle:
                    display = fee_handle          # already "platform:identity"
                else:
                    display = f"@{fee_handle}"    # X handle
                base += f" | trading fees → {display}"
            return base
        else:
            err = result.get("error", "unknown error")
            # Sanitize — never post raw stack traces or full URLs to Twitter
            if "Connection refused" in err or "Max retries" in err or "localhost" in err:
                friendly = "launchpad offline. try again soon."
            elif "already exists" in err:
                friendly = "that name or symbol is already taken."
            elif "forbidden" in err.lower() or "Unauthorized" in err:
                friendly = "deploy rejected by launchpad."
            elif "vanity" in err.lower() or "keypair" in err.lower():
                friendly = "no mint keypairs available. contact @wwwEREBUS."
            else:
                friendly = err[:60] if len(err) <= 60 else "deploy failed. try again."
            return f"@{handle} {friendly}"

    def get_points(self, handle: str) -> int:
        """Return the current point total for an X handle."""
        db = self._load_points()
        return db.get(handle, {}).get("points", 0)

    def get_leaderboard(self, top_n: int = 10) -> list[dict]:
        """Return top N handles by deploy points."""
        db = self._load_points()
        ranked = sorted(db.items(), key=lambda x: x[1].get("points", 0), reverse=True)
        return [
            {"handle": h, "points": v.get("points", 0), "deploys": v.get("deploys", 0)}
            for h, v in ranked[:top_n]
        ]

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract(self, text: str, patterns: list[str]) -> str | None:
        for p in patterns:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return None

    def _fetch_image_bytes(self, url: str) -> bytes | None:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.content
        except Exception as e:
            self.logs.log_error(f"tokenLauncher image fetch: {e}")
            return None

    def _load_points(self) -> dict:
        try:
            if os.path.exists(POINTS_FILE):
                with open(POINTS_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_points(self, db: dict):
        os.makedirs(os.path.dirname(POINTS_FILE), exist_ok=True)
        with open(POINTS_FILE, "w") as f:
            json.dump(db, f, indent=2)

    def _award_points(self, handle: str, amount: int):
        db = self._load_points()
        entry = db.get(handle, {"points": 0, "deploys": 0})
        entry["points"]  += amount
        entry["deploys"] += 1
        entry["last_deploy"] = int(time.time())
        db[handle] = entry
        self._save_points(db)
        self.logs.log_info(
            f"+{amount} pts → @{handle} (total: {entry['points']})",
            "bold yellow", "Points"
        )
