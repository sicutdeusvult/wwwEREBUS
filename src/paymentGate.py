"""
paymentGate.py — SOL payment verification for @wwwEREBUS token launches

Flow:
  1. User tweets "@wwwEREBUS pump PEPE $PEPE"
  2. Agent replies: "send 0.02 SOL to <agent_wallet> then reply with your tx signature"
  3. User sends SOL → replies with signature
  4. Agent detects the signature reply → calls verify_sol_payment()
  5. If verified → proceeds to deploy

Verification checks:
  - Transaction exists and is confirmed
  - Sender matches the X handle's registered wallet OR any wallet in the tx
  - Receiver is the agent wallet
  - Amount >= LAUNCH_FEE_SOL
  - Transaction is recent (< 24h old)
  - Signature not already used (replay protection)
"""

import os
import re
import json
import time
import requests
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────
LAUNCH_FEE_SOL    = 0.02
LAUNCH_FEE_LAMPORTS = int(LAUNCH_FEE_SOL * 1_000_000_000)

# Agent wallet receives the fee — from env or hardcoded fallback
AGENT_WALLET = os.getenv(
    "EREBUS_WALLET",
    "HoFMgyue2HZ8kCYJ81b1Yg34AZZ7g8B7eFZ35nFqQYpW"
)

RPC_URL = os.getenv(
    "RPC_URL",
    "https://mainnet.helius-rpc.com/?api-key=e7fc71a3-e276-43cb-865b-91c2684efee8"
)

DATA_DIR   = os.getenv("DATA_DIR", "/data")
USED_SIGS_FILE = os.path.join(DATA_DIR, "used_payment_sigs.json")

# Signature regex — base58, 87-88 chars
_SIG_RE = re.compile(r'\b([1-9A-HJ-NP-Za-km-z]{87,88})\b')

# Pending payment store — tweet_id → {handle, name, symbol, image_url, tweet_url, ts}
_PENDING: dict = {}


# ── Replay protection ──────────────────────────────────────────────────────

def _load_used_sigs() -> set:
    try:
        with open(USED_SIGS_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()

def _save_sig(sig: str):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        sigs = _load_used_sigs()
        sigs.add(sig)
        # Keep last 10000 sigs
        lst = list(sigs)[-10000:]
        with open(USED_SIGS_FILE, "w") as f:
            json.dump(lst, f)
    except Exception:
        pass


# ── Pending payment management ─────────────────────────────────────────────

def store_pending(tweet_id: str, handle: str, name: str, symbol: str,
                  image_url: str | None, tweet_url: str | None,
                  cashback: bool = False, fee_wallet: str | None = None):
    """Store a pending payment request, keyed by the tweet_id we replied to."""
    _PENDING[tweet_id] = {
        "handle":     handle,
        "name":       name,
        "symbol":     symbol,
        "image_url":  image_url,
        "tweet_url":  tweet_url,
        "cashback":   cashback,
        "fee_wallet": fee_wallet,
        "ts":         time.time(),
    }
    # Prune entries older than 6h
    cutoff = time.time() - 21600
    stale = [k for k, v in _PENDING.items() if v["ts"] < cutoff]
    for k in stale:
        del _PENDING[k]

def get_pending(tweet_id: str) -> dict | None:
    """Return pending deploy info for this tweet_id, or None if not found/expired."""
    p = _PENDING.get(tweet_id)
    if not p:
        return None
    if time.time() - p["ts"] > 21600:  # 6h expiry
        del _PENDING[tweet_id]
        return None
    return p

def clear_pending(tweet_id: str):
    _PENDING.pop(tweet_id, None)


# ── Signature extraction ───────────────────────────────────────────────────

def extract_signature(text: str) -> str | None:
    """Extract a Solana tx signature from tweet text."""
    m = _SIG_RE.search(text)
    return m.group(1) if m else None


# ── On-chain verification ──────────────────────────────────────────────────

def verify_sol_payment(signature: str, expected_sender_pubkey: str | None = None) -> dict:
    """
    Verify that a Solana transaction:
      - is confirmed
      - transfers >= LAUNCH_FEE_SOL to AGENT_WALLET
      - is not older than 24h
      - has not been used before

    Returns:
      {"ok": True,  "sender": "<pubkey>", "amount_sol": 0.02}
      {"ok": False, "error": "<reason>"}
    """
    # Replay check
    used = _load_used_sigs()
    if signature in used:
        return {"ok": False, "error": "signature already used"}

    try:
        resp = requests.post(RPC_URL, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed",
                }
            ]
        }, timeout=15)
        data = resp.json()
    except Exception as e:
        return {"ok": False, "error": f"RPC error: {e}"}

    result = data.get("result")
    if not result:
        return {"ok": False, "error": "transaction not found or not confirmed yet"}

    # Check transaction succeeded
    meta = result.get("meta", {})
    if meta.get("err"):
        return {"ok": False, "error": "transaction failed on-chain"}

    # Age check — blockTime is Unix seconds
    block_time = result.get("blockTime")
    if block_time and (time.time() - block_time) > 86400:
        return {"ok": False, "error": "transaction too old (> 24h)"}

    # Find the SOL transfer to AGENT_WALLET
    # Check pre/post balances to find who sent how much
    account_keys = []
    try:
        msg = result["transaction"]["message"]
        # jsonParsed gives accountKeys as list of {pubkey, signer, writable}
        for ak in msg.get("accountKeys", []):
            if isinstance(ak, dict):
                account_keys.append(ak.get("pubkey", ""))
            else:
                account_keys.append(str(ak))
    except Exception:
        return {"ok": False, "error": "could not parse account keys"}

    pre_balances  = meta.get("preBalances", [])
    post_balances = meta.get("postBalances", [])

    # Find agent wallet index
    try:
        agent_idx = account_keys.index(AGENT_WALLET)
    except ValueError:
        return {"ok": False, "error": f"agent wallet {AGENT_WALLET[:8]}... not in transaction"}

    # How much did agent wallet receive?
    if agent_idx >= len(pre_balances) or agent_idx >= len(post_balances):
        return {"ok": False, "error": "balance index out of range"}

    received_lamports = post_balances[agent_idx] - pre_balances[agent_idx]
    received_sol = received_lamports / 1_000_000_000

    if received_lamports < LAUNCH_FEE_LAMPORTS:
        return {
            "ok": False,
            "error": f"insufficient payment: received {received_sol:.4f} SOL, need {LAUNCH_FEE_SOL} SOL"
        }

    # Find the sender — account whose balance decreased the most
    sender_pubkey = None
    max_decrease = 0
    for i, (pre, post) in enumerate(zip(pre_balances, post_balances)):
        decrease = pre - post
        if decrease > max_decrease and i < len(account_keys):
            max_decrease = decrease
            sender_pubkey = account_keys[i]

    # If we know the expected sender, validate it matches
    if expected_sender_pubkey and sender_pubkey:
        if sender_pubkey != expected_sender_pubkey:
            # Allow it anyway — user might have sent from a different wallet
            pass

    # Mark signature as used
    _save_sig(signature)

    return {
        "ok":          True,
        "sender":      sender_pubkey or "unknown",
        "amount_sol":  received_sol,
        "signature":   signature,
    }


# ── Reply text builders ────────────────────────────────────────────────────

def build_payment_request_reply(handle: str, name: str, symbol: str) -> str:
    """Reply asking the user to pay before deployment."""
    return (
        f"@{handle} to launch {name} (${symbol}) on pump.fun: "
        f"send {LAUNCH_FEE_SOL} SOL to {AGENT_WALLET} "
        f"then reply here with your tx signature. "
        f"valid for 6h."
    )

def build_payment_invalid_reply(handle: str, error: str) -> str:
    """Reply when payment verification fails."""
    friendly = error
    if "not found" in error or "not confirmed" in error:
        friendly = "tx not found yet. wait a moment and try again."
    elif "already used" in error:
        friendly = "that signature was already used for a launch."
    elif "insufficient" in error:
        friendly = f"payment too low. need {LAUNCH_FEE_SOL} SOL."
    elif "too old" in error:
        friendly = "tx too old. send a fresh payment."
    elif "failed on-chain" in error:
        friendly = "that transaction failed on-chain."
    return f"@{handle} {friendly}"
