"""
tipHandler.py — Parses and executes tip + wallet-check commands from tweets.

Supported tweet formats (all require @wwwEREBUSmention):

  Wallet check (any user):
    "what's my wallet"
    "show my wallet"
    "my solana wallet"
    "wallet info"
    "@wwwEREBUS wallet"

  Tip (only tweet AUTHOR can spend — enforced by caller):
    "tip @user2 0.05"
    "tip @user2 0.05 sol"
    "send 0.01 sol to @user2"
    "send @user2 0.05"

Security:
  - Caller (server.py) MUST pass tweet_author_handle == the sender
  - tipHandler never reads sender from the tweet text itself
  - All validation delegated to WalletManager.validate_tip()
"""

import re
import os
import asyncio
from src.walletManager import wallet_manager, MIN_DEPLOY_SOL, MIN_RESERVE_SOL, LAMPORTS

# ── Regex patterns ─────────────────────────────────────────────────────────────

_WALLET_CHECK = re.compile(
    r'\b(what.?s\s+my\s+wallet|show\s+my\s+wallet|my\s+(solana\s+)?wallet|wallet\s+info|wallet\s+address|my\s+address)\b',
    re.IGNORECASE
)

_TIP_PATTERN = re.compile(
    r'\b(?:tip|send)\s+(?:@?(\w{1,50})\s+([\d.]+)|'   # tip @user 0.05
    r'([\d.]+)\s+(?:sol\s+)?to\s+@?(\w{1,50}))\b',    # send 0.05 sol to @user
    re.IGNORECASE
)


def detect_wallet_check(text: str) -> bool:
    return bool(_WALLET_CHECK.search(text))


def detect_tip_intent(text: str) -> dict | None:
    """
    Returns {receiver, amount_sol} if tip pattern found, else None.
    Does NOT validate — caller must still call validate_tip().
    """
    m = _TIP_PATTERN.search(text)
    if not m:
        return None

    if m.group(1) and m.group(2):
        receiver = m.group(1).lstrip("@").lower()
        try:
            amount = float(m.group(2))
        except ValueError:
            return None
    elif m.group(3) and m.group(4):
        receiver = m.group(4).lstrip("@").lower()
        try:
            amount = float(m.group(3))
        except ValueError:
            return None
    else:
        return None

    if amount <= 0 or amount > 100:
        return None

    return {"receiver": receiver, "amount_sol": amount}


async def handle_wallet_check(handle: str) -> str:
    """
    Returns a EREBUS-style reply about the user's wallet.
    Creates the wallet if it doesn't exist yet.
    """
    info = wallet_manager.get_or_create(handle)
    pubkey = info["pubkey"]
    # Use sync version — this runs in a thread via asyncio.run()
    balance = wallet_manager.get_balance_sol_sync(handle)

    short = pubkey[:4] + ".." + pubkey[-4:]
    bal_str = f"{balance:.4f}" if balance is not None else "unknown"
    needs_deposit = balance is not None and balance < MIN_DEPLOY_SOL

    replies = [
        f"@{handle} wallet: {pubkey} | balance: {bal_str} SOL"
        + (" | need ≥0.03 SOL to deploy" if needs_deposit else ""),

        f"@{handle} your erebuswallet → {pubkey} | {bal_str} SOL on chain"
        + (" | fund it before deploying" if needs_deposit else " | ready to deploy"),

        f"@{handle} signal wallet: {pubkey} | {bal_str} SOL"
        + (" | deposit SOL to unlock token launches" if needs_deposit else ""),
    ]

    import hashlib, time
    idx = int(hashlib.md5(f"{handle}{int(time.time()//3600)}".encode()).hexdigest(), 16) % len(replies)
    return replies[idx]


async def handle_tip(
    sender_handle: str,
    receiver_handle: str,
    amount_sol: float,
    tweet_id: str,
) -> dict:
    """
    Execute a tip from sender → receiver.
    Returns {success, reply, signature?, error?}

    SECURITY: sender_handle comes from the tweet author field (verified by Twitter),
    NOT from tweet text. This prevents impersonation.
    """
    # 1. Validate
    ok, err = wallet_manager.validate_tip(sender_handle, receiver_handle, amount_sol, tweet_id)
    if not ok:
        if "no wallet" in err:
            return {
                "success": False,
                "reply": _no_wallet_reply(receiver_handle),
                "error": err,
            }
        return {
            "success": False,
            "reply": _tip_error_reply(sender_handle, err),
            "error": err,
        }

    # 2. Check sender balance
    sender_balance = await wallet_manager.get_balance_sol(sender_handle)
    if sender_balance is None:
        return {"success": False, "reply": f"@{sender_handle} could not fetch your wallet balance. try again.", "error": "rpc error"}

    required = amount_sol + MIN_RESERVE_SOL + 0.000005  # tip + reserve + tx fee
    if sender_balance < required:
        short = f"{sender_balance:.4f}"
        return {
            "success": False,
            "reply": f"@{sender_handle} insufficient funds. balance: {short} SOL, need {required:.4f} SOL (tip + reserve)",
            "error": "insufficient balance",
        }

    # 3. Execute transfer
    receiver_pubkey = wallet_manager.get_pubkey(receiver_handle)
    sender_kp = wallet_manager.get_keypair(sender_handle)

    if not receiver_pubkey or not sender_kp:
        return {"success": False, "reply": f"@{sender_handle} wallet error. try again.", "error": "wallet lookup failed"}

    sig = await _send_sol(sender_kp, receiver_pubkey, amount_sol)
    if not sig:
        return {"success": False, "reply": f"@{sender_handle} transfer failed. try again.", "error": "transaction failed"}

    # 4. Record
    wallet_manager.record_tip(sender_handle, receiver_handle, amount_sol, tweet_id, sig)

    reply = _tip_success_reply(sender_handle, receiver_handle, amount_sol, sig)
    return {"success": True, "reply": reply, "signature": sig}


async def _send_sol(sender_kp, receiver_pubkey_str: str, amount_sol: float) -> str | None:
    """Execute SOL transfer. Returns signature or None on failure."""
    import httpx
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.system_program import transfer, TransferParams
    from solders.transaction import Transaction as SoldersTransaction
    from solders.message import Message
    from solders.hash import Hash

    rpc = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
    lamports_to_send = int(amount_sol * LAMPORTS)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Get recent blockhash
            bh_resp = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getLatestBlockhash",
                "params": [{"commitment": "confirmed"}]
            })
            bh_data = bh_resp.json()
            blockhash_str = bh_data["result"]["value"]["blockhash"]

            receiver_pk = Pubkey.from_string(receiver_pubkey_str)
            ix = transfer(TransferParams(
                from_pubkey=sender_kp.pubkey(),
                to_pubkey=receiver_pk,
                lamports=lamports_to_send,
            ))

            msg = Message.new_with_blockhash(
                [ix],
                sender_kp.pubkey(),
                Hash.from_string(blockhash_str),
            )
            tx = SoldersTransaction([sender_kp], msg)
            tx_bytes = bytes(tx)
            tx_b64 = __import__('base64').b64encode(tx_bytes).decode()

            send_resp = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "sendTransaction",
                "params": [tx_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}]
            })
            result = send_resp.json()
            if "error" in result:
                print(f"[tipHandler] send error: {result['error']}")
                return None
            return result.get("result")  # signature

    except Exception as e:
        print(f"[tipHandler] _send_sol exception: {e}")
        return None


# ── Reply generators (erebusstyle, varied) ─────────────────────────────────────

def _tip_success_reply(sender, receiver, amount, sig):
    short_sig = sig[:8] + ".." if sig else "??"
    options = [
        f"@{sender} sent {amount} SOL → @{receiver} | tx: {short_sig}",
        f"@{sender} transfer confirmed. {amount} SOL dispatched to @{receiver} | {short_sig}",
        f"@{sender} signal transferred. {amount} SOL → @{receiver} on chain | {short_sig}",
    ]
    import hashlib, time
    idx = int(hashlib.md5(f"{sender}{receiver}{int(time.time()//60)}".encode()).hexdigest(), 16) % len(options)
    return options[idx]


def _no_wallet_reply(receiver):
    options = [
        f"@{receiver} has no wallet. they need to visit erebus and connect their X account first.",
        f"@{receiver} hasn't registered yet. they must login at erebus to activate their wallet.",
        f"no wallet found for @{receiver}. tell them to connect at erebus",
    ]
    import hashlib, time
    idx = int(hashlib.md5(f"{receiver}{int(time.time()//3600)}".encode()).hexdigest(), 16) % len(options)
    return options[idx]


def _tip_error_reply(sender, error):
    if "rate limit" in error:
        return f"@{sender} tip rate limit hit. max 3 tips per hour."
    if "replay" in error or "already processed" in error:
        return f"@{sender} this tip was already processed."
    if "max tip" in error:
        return f"@{sender} max tip is 0.1 SOL per transaction."
    return f"@{sender} tip failed: {error}"
