"""
walletManager.py — Per-X-handle Solana wallet management for @wwwEREBUS

Each X handle that visits the dashboard gets exactly one Solana wallet
generated server-side and stored in /data/wallets.json.

Wallet record:
  {
    "handle":      "username",           # lowercase, no @
    "pubkey":      "base58 pubkey",
    "secret":      [1,2,...,64],         # raw secret key bytes (JSON array)
    "created_at":  "ISO timestamp",
    "last_seen":   "ISO timestamp"
  }

Security model:
  - Only the handle owner (verified via X OAuth cookie) can export private key
  - Only the handle owner can authorize tips FROM their wallet
  - Tips are rate-limited: max 0.1 SOL per tip, max 3 tips per hour per handle
  - Tweet IDs are logged to prevent replay attacks
  - Min 0.005 SOL always reserved (wallet never fully drained)
  - No self-tips, no tips to unknown handles
"""

import json
import os
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from solders.keypair import Keypair  # type: ignore
from solders.pubkey import Pubkey    # type: ignore

# ── Constants ──────────────────────────────────────────────────────────────────
# Must match src/config.py — uses DATA_DIR env var pointing to /data persistent disk on Render
DATA_DIR     = os.getenv("DATA_DIR", "/data")
WALLETS_FILE = os.path.join(DATA_DIR, "wallets.json")
TIP_LOG_FILE = os.path.join(DATA_DIR, "tip_log.json")
OWNED_WALLETS_FILE = os.path.join(DATA_DIR, "owned_wallets.json")
EXPORT_LOG_FILE = os.path.join(DATA_DIR, "wallet_export_log.json")

MIN_DEPLOY_SOL  = 0.03      # minimum balance required to deploy
MIN_RESERVE_SOL = 0.005     # always kept in wallet, never sent out
MAX_TIP_SOL     = 0.1       # max per single tip
MAX_TIPS_PER_HOUR = 3       # rate limit per sender handle

LAMPORTS = 1_000_000_000


def _load_wallets() -> dict:
    try:
        with open(WALLETS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_wallets(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(WALLETS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _load_tip_log() -> dict:
    try:
        with open(TIP_LOG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"used_tweet_ids": [], "tips": []}


def _save_tip_log(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TIP_LOG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _normalize_handle(handle: str) -> str:
    """Lowercase, strip @ prefix."""
    return handle.lstrip("@").lower().strip()


def _load_owned_wallets() -> dict:
    try:
        with open(OWNED_WALLETS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_owned_wallets(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OWNED_WALLETS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _load_export_log() -> dict:
    try:
        with open(EXPORT_LOG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"events": []}


def _save_export_log(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(EXPORT_LOG_FILE, "w") as f:
        json.dump(data, f, indent=2)


class WalletManager:

    def get_or_create(self, handle: str) -> dict:
        """
        Return existing wallet record for handle, or create a new one.
        Returns dict with pubkey (and secret if newly created).
        """
        handle = _normalize_handle(handle)
        wallets = _load_wallets()

        if handle in wallets:
            w = wallets[handle]
            # Update last_seen
            wallets[handle]["last_seen"] = datetime.now(timezone.utc).isoformat()
            _save_wallets(wallets)
            return {
                "handle":     handle,
                "pubkey":     w["pubkey"],
                "created_at": w["created_at"],
                "new":        False,
            }

        # Generate new keypair
        kp = Keypair()
        pubkey = str(kp.pubkey())
        secret_bytes = list(bytes(kp))

        now = datetime.now(timezone.utc).isoformat()
        wallets[handle] = {
            "handle":     handle,
            "pubkey":     pubkey,
            "secret":     secret_bytes,
            "created_at": now,
            "last_seen":  now,
        }
        _save_wallets(wallets)

        return {
            "handle":     handle,
            "pubkey":     pubkey,
            "created_at": now,
            "new":        True,
        }

    def get_pubkey(self, handle: str) -> str | None:
        """Return pubkey for handle, or None if not registered."""
        handle = _normalize_handle(handle)
        wallets = _load_wallets()
        w = wallets.get(handle)
        return w["pubkey"] if w else None

    def get_keypair(self, handle: str) -> Keypair | None:
        """Return Keypair for handle (server-side use only)."""
        handle = _normalize_handle(handle)
        wallets = _load_wallets()
        w = wallets.get(handle)
        if not w:
            return None
        return Keypair.from_bytes(bytes(w["secret"]))

    def get_secret_array(self, handle: str) -> list | None:
        """Return raw secret key bytes array (for user export via authenticated API)."""
        handle = _normalize_handle(handle)
        wallets = _load_wallets()
        w = wallets.get(handle)
        return w["secret"] if w else None

    def has_wallet(self, handle: str) -> bool:
        handle = _normalize_handle(handle)
        return handle in _load_wallets()

    def get_balance_sol_sync(self, handle: str) -> float | None:
        """Synchronous balance fetch using requests (no async needed)."""
        import requests as _req
        pubkey = self.get_pubkey(handle)
        if not pubkey:
            return None
        rpc = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com").strip().strip('"').strip("'")
        # Guard against corrupted env var (e.g. two vars merged on one line)
        if " " in rpc or "=" in rpc:
            rpc = "https://api.mainnet-beta.solana.com"
        try:
            r = _req.post(rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method":  "getBalance",
                "params":  [pubkey, {"commitment": "confirmed"}]
            }, timeout=10)
            data = r.json()
            lamports = data["result"]["value"]
            return lamports / LAMPORTS
        except Exception as e:
            print(f"[walletManager] get_balance_sol_sync error: {e}")
            return None

    async def get_balance_sol(self, handle: str) -> float | None:
        """Fetch live SOL balance for handle's wallet (async, uses requests in thread)."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get_balance_sol_sync, handle)

    def get_owned_wallet(self, handle: str) -> str | None:
        handle = _normalize_handle(handle)
        entry = _load_owned_wallets().get(handle)
        return entry.get("pubkey") if entry else None

    def bind_owned_wallet(self, handle: str, pubkey: str) -> dict:
        handle = _normalize_handle(handle)
        pubkey = (pubkey or "").strip()
        if not pubkey:
            raise ValueError("pubkey required")
        owned = _load_owned_wallets()
        now = datetime.now(timezone.utc).isoformat()
        current = owned.get(handle)
        if current and current.get("pubkey") and current.get("pubkey") != pubkey:
            raise ValueError("ownership wallet already bound for this identity")
        owned[handle] = {
            "handle": handle,
            "pubkey": pubkey,
            "bound_at": current.get("bound_at", now) if current else now,
            "last_seen": now,
        }
        _save_owned_wallets(owned)
        return owned[handle]

    def clear_owned_wallet(self, handle: str):
        handle = _normalize_handle(handle)
        owned = _load_owned_wallets()
        if handle in owned:
            del owned[handle]
            _save_owned_wallets(owned)

    def can_export_secret(self, handle: str, cooldown_seconds: int = 600) -> tuple[bool, str]:
        handle = _normalize_handle(handle)
        log = _load_export_log()
        now = time.time()
        recent = [e for e in log.get("events", []) if e.get("handle") == handle]
        if recent and (now - recent[-1].get("ts", 0)) < cooldown_seconds:
            wait = int(cooldown_seconds - (now - recent[-1].get("ts", 0)))
            return False, f"export cooldown active. try again in {wait}s"
        return True, ""

    def record_secret_export(self, handle: str, owned_wallet: str | None = None):
        handle = _normalize_handle(handle)
        log = _load_export_log()
        events = log.get("events", [])
        events.append({
            "handle": handle,
            "owned_wallet": owned_wallet,
            "ts": time.time(),
            "at": datetime.now(timezone.utc).isoformat(),
        })
        if len(events) > 5000:
            events = events[-5000:]
        log["events"] = events
        _save_export_log(log)

    def validate_tip(
        self,
        sender_handle: str,
        receiver_handle: str,
        amount_sol: float,
        tweet_id: str,
    ) -> tuple[bool, str]:
        """
        Validate a tip request. Returns (ok, error_message).
        Security checks:
          - sender != receiver
          - receiver has a wallet
          - amount within limits
          - tweet ID not already used (replay protection)
          - sender not over hourly rate limit
          - amount > 0
        """
        sender   = _normalize_handle(sender_handle)
        receiver = _normalize_handle(receiver_handle)

        if sender == receiver:
            return False, "cannot tip yourself"

        if amount_sol <= 0:
            return False, "amount must be positive"

        if amount_sol > MAX_TIP_SOL:
            return False, f"max tip is {MAX_TIP_SOL} SOL"

        if not self.has_wallet(receiver):
            return False, f"@{receiver} has no wallet — they need to visit the dashboard first"

        tip_log = _load_tip_log()

        # Replay protection — tweet ID already processed
        if tweet_id in tip_log.get("used_tweet_ids", []):
            return False, "tip already processed"

        # Rate limit — count sender tips in last hour
        now = time.time()
        recent = [
            t for t in tip_log.get("tips", [])
            if t["sender"] == sender and now - t["ts"] < 3600
        ]
        if len(recent) >= MAX_TIPS_PER_HOUR:
            return False, f"rate limit: max {MAX_TIPS_PER_HOUR} tips per hour"

        return True, ""

    def record_tip(
        self,
        sender_handle: str,
        receiver_handle: str,
        amount_sol: float,
        tweet_id: str,
        signature: str,
    ):
        """Log a completed tip for replay protection and rate limiting."""
        tip_log = _load_tip_log()

        # Keep used tweet IDs (cap at 10000)
        ids = tip_log.get("used_tweet_ids", [])
        ids.append(tweet_id)
        if len(ids) > 10_000:
            ids = ids[-10_000:]
        tip_log["used_tweet_ids"] = ids

        # Log tip
        tips = tip_log.get("tips", [])
        tips.append({
            "sender":   _normalize_handle(sender_handle),
            "receiver": _normalize_handle(receiver_handle),
            "amount":   amount_sol,
            "tweet_id": tweet_id,
            "sig":      signature,
            "ts":       time.time(),
        })
        # Keep last 5000 tips
        if len(tips) > 5000:
            tips = tips[-5000:]
        tip_log["tips"] = tips

        _save_tip_log(tip_log)


# Module-level singleton
wallet_manager = WalletManager()
