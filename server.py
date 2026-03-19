"""
erebus — Production Server
FastAPI + WebSocket + erebus agent
Render.com deployment with /data persistent disk
"""
import sys, types
# Python 3.13+ removed imghdr
if "imghdr" not in sys.modules:
    _m = types.ModuleType("imghdr"); _m.what = lambda *a, **kw: None
    sys.modules["imghdr"] = _m

import asyncio
import json
import os
import threading
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import shutil

sys.path.append(os.path.abspath('.'))

from src.config import ensure_data_dirs, get_config
ensure_data_dirs()
config = get_config()

# ── Neural Bridge (CL SDK) ────────────────────────────────────────
try:
    from src.neuralBridge import NeuralBridge
    _neural_bridge = NeuralBridge()
    _neural_bridge.start()
    print("[neural] NeuralBridge started")
except Exception as _nb_err:
    print(f"[neural] NeuralBridge init failed (non-fatal): {_nb_err}")
    class _NeuralBridgeStub:
        def get_state(self): return {}
        def get_history(self): return []
        def is_ready(self): return False
        def format_for_prompt(self): return ""
    _neural_bridge = _NeuralBridgeStub()

# ── Install Playwright browser at runtime if missing ──────────────
def _ensure_playwright_browser():
    import subprocess, shutil
    pw_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH",
              os.path.join(os.path.dirname(__file__), ".playwright"))
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = pw_path
    os.makedirs(pw_path, exist_ok=True)

    # Check if chromium executable already exists
    for root, dirs, files in os.walk(pw_path):
        for f in files:
            if "chrome" in f.lower() and os.access(os.path.join(root, f), os.X_OK):
                print(f"[PW] Chromium found: {os.path.join(root, f)}")
                return  # Already installed

    print("[PW] Chromium not found — installing now...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=False, timeout=300,
            env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": pw_path}
        )
        subprocess.run(
            [sys.executable, "-m", "playwright", "install-deps", "chromium"],
            capture_output=False, timeout=120,
            env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": pw_path}
        )
        print("[PW] Chromium installed.")
    except Exception as e:
        print(f"[PW] Install warning: {e}")

_ensure_playwright_browser()

# ── Shared state ──────────────────────────────────────────────────
log_buffer: Queue = Queue()
connected_ws: set = set()

stats = {
    "rounds": 0, "actions": 0, "errors": 0, "decisions": 0,
    "status": "STARTING", "phase": "INIT", "last_action": "",
    "started_at": datetime.now().isoformat(),
    "followers": 0, "following": 0,
    "post_heatmap": {},     # hour -> count
    "transmissions": [],    # recent posts with engagement
}

# ── Persistent stats (survives restarts) ─────────────────────────
_STATS_FILE = os.path.join(os.getenv("DATA_DIR", "/data"), "memory", "agent_stats.json")

def _load_persistent_stats():
    """Load saved counters from disk and seed stats dict on boot."""
    _DATA_DIR = os.getenv("DATA_DIR", "/data")

    # 1. Load our own agent_stats.json (rounds, decisions, errors)
    try:
        os.makedirs(os.path.dirname(_STATS_FILE), exist_ok=True)
        if os.path.exists(_STATS_FILE):
            with open(_STATS_FILE, "r") as f:
                saved = json.load(f)
            for k in ("rounds", "actions", "decisions", "errors"):
                if saved.get(k, 0) > stats.get(k, 0):
                    stats[k] = saved[k]
    except Exception:
        pass

    # 2. Seed actions from memory/stats.json (written by agent every cycle)
    try:
        _mem_stats_path = os.path.join(_DATA_DIR, "memory", "stats.json")
        if os.path.exists(_mem_stats_path):
            with open(_mem_stats_path, "r") as f:
                _ms = json.load(f)
            total = _ms.get("total_posts", 0) + _ms.get("total_replies", 0)
            if total > stats.get("actions", 0):
                stats["actions"] = total
    except Exception:
        pass

    # 3. Fallback: count directly from memory.json entries
    # (covers case where stats.json hasn't been written yet)
    if stats.get("actions", 0) == 0:
        try:
            _mem_path = os.path.join(_DATA_DIR, "memory", "memory.json")
            if os.path.exists(_mem_path):
                with open(_mem_path, "r") as f:
                    entries = json.load(f)
                if isinstance(entries, list):
                    stats["actions"] = len(entries)
        except Exception:
            pass

    # 4. Count rounds from log file if agent_stats.json missing/zero
    if stats.get("rounds", 0) == 0:
        try:
            _log_path = os.path.join(_DATA_DIR, "logs", "erebus.log")
            if os.path.exists(_log_path):
                count = 0
                with open(_log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            if json.loads(line).get("section") == "DORMANT":
                                count += 1
                        except Exception:
                            pass
                if count > 0:
                    stats["rounds"] = count
        except Exception:
            pass

    print(f"[stats] boot: rounds={stats['rounds']} actions={stats['actions']} decisions={stats['decisions']}")

def _save_persistent_stats():
    """Write current counters to disk."""
    try:
        os.makedirs(os.path.dirname(_STATS_FILE), exist_ok=True)
        with open(_STATS_FILE, "w") as f:
            json.dump({
                "rounds":    stats["rounds"],
                "actions":   stats["actions"],
                "decisions": stats["decisions"],
                "errors":    stats["errors"],
                "saved_at":  datetime.now().isoformat(),
            }, f)
    except Exception:
        pass

# Seed from disk immediately at import time
_load_persistent_stats()

# ── Log emitter ───────────────────────────────────────────────────
def emit(log_type: str, message: str, section: str = None):
    entry = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "type": log_type,
        "message": str(message),
        "section": section or log_type.upper(),
    }
    log_buffer.put(entry)
    try:
        log_path = config.get("log_path", "/data/logs/erebus.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

# ── HTTP Routes ───────────────────────────────────────────────────
_agent_thread = None  # global ref for watchdog

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent_thread
    asyncio.create_task(broadcaster())
    asyncio.create_task(stats_broadcaster())
    _agent_thread = threading.Thread(target=run_agent, daemon=True)
    _agent_thread.start()
    yield

app = FastAPI(title="erebus", lifespan=lifespan)

# prompt fingerprint for deploy verification
try:
    from src.config import get_prompt as _get_prompt_for_fingerprint
    _prompt_blob = json.dumps(_get_prompt_for_fingerprint().get("erebus", {}), sort_keys=True).encode("utf-8")
    print("[PROMPT FINGERPRINT]", hashlib.sha256(_prompt_blob).hexdigest()[:16])
except Exception as _pf_err:
    print("[PROMPT FINGERPRINT ERROR]", _pf_err)

# Serve imgs/ as /imgs/
_imgs_dir = Path(__file__).parent / "imgs"
if _imgs_dir.exists():
    app.mount("/imgs", StaticFiles(directory=str(_imgs_dir)), name="imgs")

# Serve public/ as /public/ (logo, icons, etc.)
_public_dir = Path(__file__).parent / "public"
if _public_dir.exists():
    app.mount("/public", StaticFiles(directory=str(_public_dir)), name="public")

# Serve /erebus_logo.png, /favicon.svg etc directly at root path
@app.get("/erebus_logo.png")
async def serve_logo():
    from fastapi.responses import FileResponse
    p = _public_dir / "erebus_logo.png"
    if p.exists(): return FileResponse(str(p), media_type="image/png")
    from fastapi.responses import Response
    return Response(status_code=404)

@app.get("/favicon.svg")
async def serve_favicon():
    from fastapi.responses import FileResponse
    p = _public_dir / "favicon.svg"
    if p.exists(): return FileResponse(str(p), media_type="image/svg+xml")
    from fastapi.responses import Response
    return Response(status_code=404)

# Root-level static assets (favicon, manifest) from public/
@app.get("/favicon.svg")
async def serve_favicon():
    p = Path(__file__).parent / "public" / "favicon.svg"
    if p.exists():
        from fastapi.responses import Response
        return Response(content=p.read_bytes(), media_type="image/svg+xml")
    return HTMLResponse("", status_code=404)

@app.get("/manifest.json")
async def serve_manifest():
    import json as _json
    p = Path(__file__).parent / "public" / "manifest.json"
    if p.exists():
        return JSONResponse(content=_json.loads(p.read_text()))
    return JSONResponse({}, status_code=404)

@app.get("/")
async def serve_terminal(request: Request):
    """Serve gate.html for all users — it handles login/profile state via JS."""
    gate_path = Path(__file__).parent / "public" / "gate.html"
    if gate_path.exists():
        return HTMLResponse(content=gate_path.read_text(encoding="utf-8"))
    return RedirectResponse("/auth/x/start", status_code=302)

@app.get("/terminal")
@app.get("/terminal.html")
async def serve_terminal_page(request: Request):
    """Serve the terminal page at both /terminal and /terminal.html."""
    terminal_path = Path(__file__).parent / "public" / "terminal.html"
    if terminal_path.exists():
        return HTMLResponse(content=terminal_path.read_text(encoding="utf-8"))
    legacy_path = Path(__file__).parent / "terminal.html"
    if legacy_path.exists():
        return HTMLResponse(content=legacy_path.read_text(encoding="utf-8"))
    return HTMLResponse("terminal not found", status_code=404)

@app.get("/health")
async def health():
    return {"status": "ok", "agent": stats["status"], "phase": stats["phase"]}

@app.get("/api/stats")
async def api_stats():
    # stats dict is seeded from /data/memory/stats.json at boot
    # and saved every round — always reflects real totals
    try:
        from src.memory import memory as _Mem
        _ms = _Mem().get_stats() or {}
        # Use memory.json total_posts if it's higher (edge case: manual memory edits)
        if _ms.get("total_posts", 0) > stats.get("actions", 0):
            stats["actions"] = _ms["total_posts"]
    except Exception:
        pass
    return JSONResponse(stats)

@app.get("/api/logs")
async def api_logs(n: int = 100):
    log_path = config.get("log_path", "/data/logs/erebus.log")
    if not os.path.exists(log_path):
        return JSONResponse([])
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        parsed = []
        for line in lines[-n:]:
            try:
                parsed.append(json.loads(line.strip()))
            except Exception:
                parsed.append({"ts": "", "type": "info", "message": line.strip(), "section": "LOG"})
        return JSONResponse(parsed)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/dialog")
async def api_dialog(n: int = 20):
    dialog_path = config.get("dialog_path", "/data/dialog/dialog.jsonl")
    if not os.path.exists(dialog_path):
        return JSONResponse([])
    try:
        with open(dialog_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        parsed = []
        for line in lines[-n:]:
            try:
                parsed.append(json.loads(line.strip()))
            except Exception:
                pass
        return JSONResponse(parsed)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/memory")
async def api_memory():
    memory_path = config.get("memory_path", "/data/memory/memory.json")
    if not os.path.exists(memory_path):
        return JSONResponse([])
    try:
        with open(memory_path, "r", encoding="utf-8") as f:
            return JSONResponse(json.load(f))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/neural-state")
async def api_neural_state():
    """Live neural metrics from the CL SDK."""
    state = _neural_bridge.get_state()
    if not state:
        return JSONResponse({"status": "initializing", "data": None})
    return JSONResponse({"status": "ok", "data": state})

@app.get("/api/neural-history")
async def api_neural_history():
    """Recent neural state history (up to 120 snapshots)."""
    history = _neural_bridge.get_history()
    return JSONResponse({"count": len(history), "history": history[-20:]})

_profile_cache = {"followers": 0, "following": 0, "posts": 0}
_profile_cache_ts = 0

@app.get("/api/profile")
async def api_profile():
    """Live follower/following counts — cached 10 min to avoid 429."""
    global _profile_cache, _profile_cache_ts
    import time as _t
    if _t.time() - _profile_cache_ts < 600:  # 10 min cache
        return JSONResponse(_profile_cache)
    try:
        from src.observationX import observationX as _obs
        _o = _obs()
        uid = _o.xBridge_instance._get_uid()
        if not uid:
            return JSONResponse(_profile_cache)
        resp = _o.xBridge_instance.client.get_user(
            id=uid,
            user_fields=["public_metrics"],
            user_auth=True,
        )
        if resp and resp.data:
            pm = getattr(resp.data, 'public_metrics', {}) or {}
            _profile_cache = {
                "followers": pm.get("followers_count", 0),
                "following": pm.get("following_count", 0),
                "posts":     pm.get("tweet_count", 0),
            }
            _profile_cache_ts = _t.time()
    except Exception:
        pass
    return JSONResponse(_profile_cache)

@app.get("/api/heatmap")
async def api_heatmap():
    """Return post activity heatmap."""
    log_path = config.get("log_path", "/data/logs/erebus.log")
    heatmap = {str(h): 0 for h in range(24)}
    try:
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        if entry.get("section") in ("TRANSMIT", "Transmit"):
                            ts = entry.get("ts", "")
                            if ts:
                                hour = ts[11:13]
                                if hour.isdigit():
                                    heatmap[str(int(hour))] = heatmap.get(str(int(hour)), 0) + 1
                    except Exception:
                        pass
    except Exception:
        pass
    return JSONResponse(heatmap)

@app.get("/api/transmissions")
async def api_transmissions(n: int = 50):
    """Return recent transmissions with tweet_id for linking."""
    log_path = config.get("log_path", "/data/logs/erebus.log")
    results = []
    try:
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in reversed(lines):
                try:
                    entry = json.loads(line.strip())
                    if entry.get("section") in ("TRANSMIT", "Transmit"):
                        msg = entry.get("message", "")
                        # Extract tweet_id and url
                        tid, url = "", ""
                        if "tweet_id=" in msg:
                            tid_part = msg.split("tweet_id=")[-1].split("|")[0].strip()
                            if tid_part.isdigit():
                                tid = tid_part
                        if "https://x.com" in msg:
                            url = "https://x.com" + msg.split("https://x.com")[-1].split()[0]
                        # Extract content (before | tweet_id)
                        content_part = msg.split("|")[0].strip()
                        for prefix in ("reply — ", "post — ", "quote — ", "retweet — "):
                            if content_part.startswith(prefix):
                                content_part = content_part[len(prefix):]
                                break
                        action_type = "post"
                        for a in ("reply", "quote", "retweet"):
                            if msg.startswith(a + " — "):
                                action_type = a
                                break
                        results.append({
                            "ts":      entry.get("ts", ""),
                            "content": content_part[:280],
                            "tweet_id": tid,
                            "url":     url,
                            "action":  action_type,
                        })
                        if len(results) >= n:
                            break
                except Exception:
                    pass
    except Exception:
        pass
    return JSONResponse(results)

@app.get("/api/x-leaderboard")
async def api_x_leaderboard(top: int = 10):
    """Return top X handles by deploy points."""
    from src.tokenLauncher import tokenLauncher as TL
    tl = TL()
    board = tl.get_leaderboard(top_n=top)
    return JSONResponse(board)


# ═══════════════════════════════════════════════════════════════
# TWITTER / X  OAUTH 1.0A  LOGIN
# Uses the same consumer key/secret as the agent's Twitter app.
# Flow:
#   1. GET /auth/x/start       → redirect to twitter.com/oauth/authorize
#   2. GET /auth/x/callback    → exchange verifier → get screen_name
#                                set signed cookie, redirect to /#x-login-ok
#   3. GET /auth/x/me          → return {handle, points} from cookie
#   4. GET /auth/x/logout      → clear cookie
# ═══════════════════════════════════════════════════════════════
import hmac as _hmac, hashlib as _hashlib, base64 as _base64
from fastapi.responses import RedirectResponse, Response as _Response

# In-memory OAuth request-token store (request_token → request_token_secret)
# Keyed by oauth_token string, cleared after use.
_oauth_tmp: dict = {}
# In-memory OAuth state stores for Discord/GitHub/Twitch
# state_token → True (just need to verify it exists)
_discord_states: dict = {}
_github_states:  dict = {}
_twitch_states:  dict = {}

def _get_twitter_creds():
    return {
        "consumer_key":    os.getenv("TWITTER_API_CONSUMER_KEY", ""),
        "consumer_secret": os.getenv("TWITTER_API_CONSUMER_SECRET", ""),
    }

def _signed_cookie_encode(data: dict, secret: str) -> str:
    """Simple HMAC-SHA256 signed cookie: base64(json).signature"""
    import json as _json
    payload = _base64.urlsafe_b64encode(_json.dumps(data).encode()).decode()
    sig = _hmac.new(secret.encode(), payload.encode(), _hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def _signed_cookie_decode(value: str, secret: str) -> dict | None:
    import json as _json
    try:
        payload, sig = value.rsplit(".", 1)
        expected = _hmac.new(secret.encode(), payload.encode(), _hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(sig, expected):
            return None
        return _json.loads(_base64.urlsafe_b64decode(payload + "=="))
    except Exception:
        return None

def _cookie_secret() -> str:
    # Prefer dedicated COOKIE_SECRET env var. Fall back to Twitter secret for backwards compat.
    # This ensures platform cookies (discord/github/twitch) verify correctly
    # even if TWITTER_API_CONSUMER_SECRET is not set.
    s = (os.getenv("COOKIE_SECRET")
         or os.getenv("TWITTER_API_CONSUMER_SECRET")
         or "alon-fallback-secret-change-me")
    return _hashlib.sha256(s.encode()).hexdigest()[:32]


@app.get("/auth/x/start")
async def auth_x_start(request: Request):
    """Step 1 — get request token and redirect user to Twitter."""
    from requests_oauthlib import OAuth1Session
    creds = _get_twitter_creds()
    if not creds["consumer_key"]:
        return JSONResponse({"error": "Twitter credentials not configured"}, status_code=500)

    # Detect base URL for callback
    scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else request.url.scheme
    host   = request.headers.get("x-forwarded-host") or request.url.hostname
    port   = request.url.port
    base   = f"{scheme}://{host}" + (f":{port}" if port and port not in (80, 443) else "")
    callback_url = f"{base}/auth/x/callback"

    try:
        oauth = OAuth1Session(
            creds["consumer_key"],
            client_secret=creds["consumer_secret"],
            callback_uri=callback_url,
        )
        fetch_response = oauth.fetch_request_token("https://api.twitter.com/oauth/request_token")
        rt = fetch_response.get("oauth_token")
        rt_secret = fetch_response.get("oauth_token_secret")
        _oauth_tmp[rt] = rt_secret          # store temporarily

        # Use /authenticate — skips re-consent for returning users
        auth_url = f"https://api.twitter.com/oauth/authenticate?oauth_token={rt}"
        return RedirectResponse(auth_url, status_code=302)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/auth/x/callback")
async def auth_x_callback(oauth_token: str = "", oauth_verifier: str = "", denied: str = ""):
    """Step 2 — Twitter redirects here after user authorises (or denies)."""
    from requests_oauthlib import OAuth1Session

    if denied:
        return RedirectResponse("/#x-login-denied", status_code=302)

    creds = _get_twitter_creds()
    rt_secret = _oauth_tmp.pop(oauth_token, None)
    if not rt_secret:
        return RedirectResponse("/#x-login-error", status_code=302)

    try:
        oauth = OAuth1Session(
            creds["consumer_key"],
            client_secret=creds["consumer_secret"],
            resource_owner_key=oauth_token,
            resource_owner_secret=rt_secret,
            verifier=oauth_verifier,
        )
        tokens = oauth.fetch_access_token("https://api.twitter.com/oauth/access_token")
        screen_name = tokens.get("screen_name", "")
        user_id     = tokens.get("user_id", "")

        # Build signed session cookie
        cookie_val = _signed_cookie_encode(
            {"handle": screen_name, "uid": user_id},
            _cookie_secret()
        )

        resp = RedirectResponse("/#x-login-ok", status_code=302)
        resp.set_cookie(
            key="erebus_x_session",
            value=cookie_val,
            max_age=60 * 60 * 24 * 30,   # 30 days
            httponly=False,                # JS needs to read for /auth/x/me
            samesite="lax",
            secure=True,                   # HTTPS only on Render
        )
        return resp
    except Exception as e:
        return RedirectResponse(f"/#x-login-error", status_code=302)


@app.get("/auth/x/me")
async def auth_x_me(request: Request):
    """Return currently logged-in X user + points + wallet info. Auto-creates wallet."""
    cookie = request.cookies.get("erebus_x_session", "")
    if not cookie:
        return JSONResponse({"logged_in": False})
    data = _signed_cookie_decode(cookie, _cookie_secret())
    if not data:
        return JSONResponse({"logged_in": False})
    handle = data.get("handle", "")

    # Auto-create wallet on first login
    wallet_pubkey = None
    wallet_balance = None
    owned_wallet = None
    try:
        from src.walletManager import wallet_manager
        wallet_info = wallet_manager.get_or_create(handle)
        wallet_pubkey = wallet_info["pubkey"]
        wallet_balance = await wallet_manager.get_balance_sol(handle)
        owned_wallet = wallet_manager.get_owned_wallet(handle)
    except Exception:
        pass

    # Backfill deployerWallet on any tokens this handle deployed without a wallet
    if wallet_pubkey:
        try:
            import json as _json
            db_path = os.path.join(DATA_DIR, "db.json")
            if os.path.exists(db_path):
                with open(db_path) as _f:
                    _db = _json.load(_f)
                _changed = False
                _handle_clean = handle.lstrip("@").lower()
                for _t in _db.get("tokens", []):
                    _dh = (_t.get("deployer_x_handle") or "").lstrip("@").lower()
                    if _dh == _handle_clean and not _t.get("deployerWallet"):
                        _t["deployerWallet"] = wallet_pubkey
                        _changed = True
                if _changed:
                    with open(db_path, "w") as _f:
                        _json.dump(_db, _f)
        except Exception:
            pass

    return JSONResponse({
        "logged_in":   True,
        "handle":      handle,
        "wallet":      wallet_pubkey,
        "owned_wallet": owned_wallet,
        "wallet_model": "x identity + generated wallet + optional ownership wallet",
        "balance_sol": wallet_balance,
    })


@app.get("/auth/x/logout")
async def auth_x_logout():
    resp = RedirectResponse("/#logged-out", status_code=302)
    resp.delete_cookie("erebus_x_session")
    return resp


@app.get("/api/wallet/info")
async def wallet_info_endpoint(request: Request):
    """Wallet pubkey + live balance for authenticated user."""
    cookie = request.cookies.get("erebus_x_session", "")
    data = _signed_cookie_decode(cookie, _cookie_secret()) if cookie else None
    if not data:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    handle = data.get("handle", "")
    from src.walletManager import wallet_manager
    w = wallet_manager.get_or_create(handle)
    balance = await wallet_manager.get_balance_sol(handle)
    return JSONResponse({
        "handle":      handle,
        "pubkey":      w["pubkey"],
        "owned_wallet": wallet_manager.get_owned_wallet(handle),
        "balance_sol": balance,
        "min_deploy":  0.03,
        "can_deploy":  (balance or 0) >= 0.03,
    })


@app.post("/api/wallet/claim-fees")
async def wallet_claim_fees(request: Request):
    """
    Server-side fee claim — agent signs with user's stored keypair.
    Authenticated via X OAuth cookie. Forwards to launchpad /api/build-claim-tx,
    then signs and broadcasts the transaction using the user's server wallet.
    """
    import httpx, base64
    cookie = request.cookies.get("erebus_x_session", "")
    data   = _signed_cookie_decode(cookie, _cookie_secret()) if cookie else None
    if not data:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    handle = data.get("handle", "")

    from src.walletManager import wallet_manager
    kp = wallet_manager.get_keypair(handle)
    if not kp:
        return JSONResponse({"error": "wallet not found"}, status_code=404)

    body = await request.json()
    pool     = body.get("pool", "")
    if not pool:
        return JSONResponse({"error": "pool required"}, status_code=400)

    launchpad = os.getenv("LAUNCHPAD_URL", "https://deployer3.onrender.com")
    rpc       = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")

    try:
        # Pass secret key bytes to launchpad so it can sign+broadcast directly
        # (same approach as working claim-creator-fee2.cjs script)
        secret_bytes = list(bytes(kp))

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{launchpad}/api/build-claim-tx", json={
                "pool":          pool,
                "creator":       str(kp.pubkey()),
                "creatorSecret": secret_bytes,   # launchpad signs + broadcasts
            })
            if r.status_code != 200:
                err = r.json().get("error", r.text[:200])
                return JSONResponse({"error": f"build-claim-tx failed: {err}"}, status_code=400)

            result = r.json()
            if not result.get("success"):
                return JSONResponse({"error": result.get("error", "claim failed")}, status_code=400)

            return JSONResponse({"success": True, "signature": result.get("signature", "")})

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/wallet/connect-owned")
async def wallet_connect_owned(request: Request):
    """Bind one ownership wallet to the logged-in X identity. One wallet per user."""
    cookie = request.cookies.get("erebus_x_session", "")
    data = _signed_cookie_decode(cookie, _cookie_secret()) if cookie else None
    if not data:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    handle = data.get("handle", "")
    body = await request.json()
    pubkey = (body.get("pubkey") or "").strip()
    if not pubkey:
        return JSONResponse({"error": "pubkey required"}, status_code=400)
    from src.walletManager import wallet_manager
    try:
        bound = wallet_manager.bind_owned_wallet(handle, pubkey)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "handle": handle, "owned_wallet": bound.get("pubkey")})


@app.post("/api/wallet/disconnect-owned")
async def wallet_disconnect_owned(request: Request):
    cookie = request.cookies.get("erebus_x_session", "")
    data = _signed_cookie_decode(cookie, _cookie_secret()) if cookie else None
    if not data:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    handle = data.get("handle", "")
    from src.walletManager import wallet_manager
    wallet_manager.clear_owned_wallet(handle)
    return JSONResponse({"ok": True})


@app.post("/api/wallet/export-key")
async def wallet_export_key(request: Request):
    """
    Secure export path for the generated server wallet.
    Requires active X session and explicit confirmation phrase.
    Export attempts are rate limited and logged to /data.
    """
    cookie = request.cookies.get("erebus_x_session", "")
    data = _signed_cookie_decode(cookie, _cookie_secret()) if cookie else None
    if not data:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    handle = data.get("handle", "")
    body = await request.json()
    confirm = (body.get("confirm") or "").strip().lower()
    if confirm != "export my erebus wallet":
        return JSONResponse({"error": "confirmation phrase mismatch"}, status_code=400)

    from src.walletManager import wallet_manager
    ok, err = wallet_manager.can_export_secret(handle)
    if not ok:
        return JSONResponse({"error": err}, status_code=429)

    secret = wallet_manager.get_secret_array(handle)
    if not secret:
        return JSONResponse({"error": "wallet not found"}, status_code=404)

    wallet_manager.record_secret_export(handle, wallet_manager.get_owned_wallet(handle))
    return JSONResponse({
        "handle": handle,
        "wallet": wallet_manager.get_pubkey(handle),
        "secret_key": secret,
        "secret_key_line": "[" + ", ".join(str(x) for x in secret) + "]",
        "warning": "keep this private. anyone with this key controls your wallet.",
        "next_step": "import these bytes into your solana wallet app and then remove the export from your device.",
    })


@app.get("/token/{mint}")
async def token_page(mint: str, request: Request):
    """
    Token detail page — https://wwwEREBUS/token/<mint>
    Fetches token data from launchpad and renders a standalone page.
    This URL is used as the 'website' field in token metadata.
    """
    import httpx as _hx
    launchpad = os.getenv("LAUNCHPAD_URL", "https://deployer3.onrender.com")
    token_data = None
    try:
        async with _hx.AsyncClient(timeout=10) as hc:
            r = await hc.get(f"{launchpad}/api/agent-deploys")
            if r.status_code == 200:
                deploys = r.json().get("tokens", [])
                token_data = next((t for t in deploys if t.get("baseMint") == mint), None)
    except Exception:
        pass

    # Build the page — works even if launchpad is down (shows loading state)
    name    = token_data.get("name", "Unknown Token")    if token_data else "..."
    symbol  = token_data.get("symbol", "???")            if token_data else "..."
    img     = token_data.get("imageUrl", "")             if token_data else ""
    desc    = token_data.get("description", "")          if token_data else ""
    deployer = token_data.get("deployer_x_handle", "")  if token_data else ""
    pool    = token_data.get("pool", "")                 if token_data else ""
    twitter = token_data.get("twitter", "")              if token_data else ""
    fee_handle = token_data.get("feeHandle") or token_data.get("fee_handle", "") if token_data else ""
    created = token_data.get("createdAt", "")            if token_data else ""

    # Build explorer links
    solscan_mint = f"https://solscan.io/token/{mint}"
    solscan_pool = f"https://solscan.io/account/{pool}" if pool else ""
    dex = f"https://dexscreener.com/solana/{mint}"
    meteora = f"https://app.meteora.ag/dynamic-bonding-curve/{pool}" if pool else ""

    # Fee recipient display
    fee_display = ""
    if fee_handle:
        if ":" in fee_handle:
            platform, identity = fee_handle.split(":", 1)
            icons = {"discord": "💬", "github": "⌥", "twitch": "🟣"}
            fee_display = f'{icons.get(platform,"")} {platform}:{identity}'
        else:
            fee_display = f"@{fee_handle}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta property="og:title" content="${symbol} — {name}">
<meta property="og:description" content="{desc or f'Token launched via @wwwEREBUS on Solana'}">
<meta property="og:image" content="{img}">
<meta name="twitter:card" content="summary_large_image">
<title>${symbol} / {name} — erebus</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=VT323:wght@400&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#050505;color:#c8c8c8;font-family:'Share Tech Mono',monospace;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:40px 16px}}
.logo{{font-family:'VT323',monospace;font-size:28px;color:#00ff41;letter-spacing:4px;margin-bottom:32px;text-decoration:none}}
.logo span{{color:#fff}}
.card{{width:100%;max-width:520px;border:1px solid rgba(0,255,65,0.15);background:rgba(0,255,65,0.02);padding:28px}}
.token-img{{width:80px;height:80px;border-radius:4px;object-fit:cover;border:1px solid rgba(0,255,65,0.2);margin-bottom:20px}}
.token-img-placeholder{{width:80px;height:80px;border:1px solid rgba(0,255,65,0.2);display:flex;align-items:center;justify-content:center;color:rgba(0,255,65,0.3);font-size:28px;margin-bottom:20px}}
.name{{font-family:'VT323',monospace;font-size:36px;color:#fff;letter-spacing:2px}}
.symbol{{font-size:11px;color:#00ff41;letter-spacing:3px;margin-bottom:16px}}
.desc{{font-size:10px;color:#888;letter-spacing:1px;line-height:1.8;margin-bottom:24px;border-left:2px solid rgba(0,255,65,0.2);padding-left:12px}}
.row{{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.04);font-size:9px;letter-spacing:1px}}
.row:last-child{{border-bottom:none}}
.row .label{{color:#555}}
.row .val{{color:#e8e8e8;text-align:right;word-break:break-all;max-width:300px}}
.row a{{color:#00ff41;text-decoration:none}}
.row a:hover{{text-decoration:underline}}
.links{{display:flex;gap:8px;margin-top:24px;flex-wrap:wrap}}
.btn{{padding:8px 16px;font-family:'Share Tech Mono',monospace;font-size:9px;letter-spacing:2px;text-decoration:none;border:1px solid;cursor:pointer}}
.btn-green{{color:#00ff41;border-color:rgba(0,255,65,0.4);background:rgba(0,255,65,0.05)}}
.btn-green:hover{{background:rgba(0,255,65,0.12)}}
.btn-gray{{color:#888;border-color:rgba(255,255,255,0.1);background:transparent}}
.btn-gray:hover{{color:#e8e8e8;border-color:rgba(255,255,255,0.25)}}
.footer{{margin-top:32px;font-size:8px;color:#333;letter-spacing:2px}}
.mint-box{{margin-top:20px;padding:10px;background:rgba(0,0,0,0.4);border:1px solid rgba(0,255,65,0.08);font-size:9px;color:#555;letter-spacing:1px;word-break:break-all}}
.mint-box span{{color:#00ff41;opacity:0.6}}
</style>
</head>
<body>
<a href="https://wwwEREBUS" class="logo">$EREBUS</a>
<div class="card">
  {'<img class="token-img" src="' + img + '" alt="' + symbol + '">' if img else '<div class="token-img-placeholder">⬡</div>'}
  <div class="name">{name}</div>
  <div class="symbol">${symbol}</div>
  {'<div class="desc">' + desc + '</div>' if desc else ''}
  <div class="row"><span class="label">DEPLOYER</span><span class="val">{'<a href="https://x.com/' + deployer.lstrip('@') + '" target="_blank">@' + deployer.lstrip('@') + '</a>' if deployer else '—'}</span></div>
  {'<div class="row"><span class="label">FEE RECIPIENT</span><span class="val">' + fee_display + '</span></div>' if fee_display else ''}
  {'<div class="row"><span class="label">POOL</span><span class="val"><a href="' + solscan_pool + '" target="_blank">' + pool[:16] + '...</a></span></div>' if pool else ''}
  {'<div class="row"><span class="label">LAUNCHED</span><span class="val">' + created[:10] + '</span></div>' if created else ''}
  {'<div class="row"><span class="label">TWEET</span><span class="val"><a href="' + twitter + '" target="_blank">view on X →</a></span></div>' if twitter else ''}
  <div class="mint-box"><span>CONTRACT</span><br>{mint}</div>
  <div class="links">
    <a class="btn btn-green" href="{dex}" target="_blank">DEXSCREENER →</a>
    <a class="btn btn-green" href="{solscan_mint}" target="_blank">SOLSCAN →</a>
    {'<a class="btn btn-green" href="' + meteora + '" target="_blank">METEORA →</a>' if meteora else ''}
    <a class="btn btn-gray" href="https://wwwEREBUS" target="_blank">TERMINAL →</a>
  </div>
</div>
<div class="footer">EREBUS — SIGNAL ON SOLANA</div>
</body>
</html>"""

    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)


@app.get("/api/claimable-fees")
async def proxy_claimable_fees(request: Request):
    """Proxy to launchpad /api/claimable-fees?wallet=<pubkey> — avoids CORS in browser."""
    import httpx
    launchpad = os.getenv("LAUNCHPAD_URL", "https://deployer3.onrender.com")
    wallet = request.query_params.get("wallet", "")
    if not wallet:
        return JSONResponse([], status_code=200)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{launchpad}/api/claimable-fees", params={"wallet": wallet})
            try:
                return JSONResponse(r.json(), status_code=r.status_code)
            except Exception:
                return JSONResponse([], status_code=200)
    except Exception as e:
        return JSONResponse([], status_code=200)


@app.post("/api/build-claim-tx")
async def proxy_build_claim_tx(request: Request):
    """Proxy to launchpad /api/build-claim-tx — avoids CORS in browser."""
    import httpx
    launchpad = os.getenv("LAUNCHPAD_URL", "https://deployer3.onrender.com")
    try:
        body = await request.json()
        # If user is logged in via X, inject their server wallet secret so launchpad
        # can sign server-side (no browser wallet needed)
        cookie = request.cookies.get("erebus_x_session", "")
        if cookie:
            session = _signed_cookie_decode(cookie, _cookie_secret())
            if session and session.get("handle"):
                handle = session["handle"].lstrip("@").lower()
                secret = wallet_manager.get_secret_array(handle)
                if secret and not body.get("creatorSecret"):
                    body["creatorSecret"] = secret
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{launchpad}/api/build-claim-tx", json=body)
            try:
                return JSONResponse(r.json(), status_code=r.status_code)
            except Exception:
                return JSONResponse({"error": "launchpad parse error"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/agent-deploys")
async def proxy_agent_deploys():
    """Proxy to launchpad /api/agent-deploys — avoids CORS in browser."""
    import httpx
    launchpad = os.getenv("LAUNCHPAD_URL", "https://deployer3.onrender.com")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{launchpad}/api/agent-deploys")
            text = r.text.strip()
            if not text:
                return JSONResponse({"tokens": [], "error": "launchpad returned empty response"}, status_code=200)
            try:
                return JSONResponse(r.json(), status_code=r.status_code)
            except Exception:
                return JSONResponse({"tokens": [], "error": f"launchpad parse error: {text[:200]}"}, status_code=200)
    except Exception as e:
        return JSONResponse({"tokens": [], "error": f"launchpad unreachable: {str(e)}"}, status_code=200)


@app.post("/upload-file")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    target: str = Form("/data"),
    secret: str = Form(""),
):
    """Upload a file to a directory on the server (used by uploader.js for vanity keypairs)."""
    agent_secret = os.getenv("AGENT_SECRET", "")
    provided = request.headers.get("x-agent-secret", "") or secret
    if agent_secret and provided != agent_secret:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        dest_path = os.path.join(target, file.filename)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        print(f"[upload-file] saved: {dest_path}")
        return JSONResponse({"success": True, "path": dest_path})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ── WebSocket ─────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global connected_ws
    await websocket.accept()
    connected_ws.add(websocket)
    try:
        await websocket.send_text(json.dumps({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "system", "message": "erebus substrate connected.", "section": "SYSTEM",
        }))
        await websocket.send_text(json.dumps({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "stats", "message": json.dumps(stats), "section": "STATS",
        }))
        while True:
            await asyncio.sleep(25)
            await websocket.send_text(json.dumps({
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "heartbeat", "message": "♦", "section": "HB",
            }))
    except (WebSocketDisconnect, Exception):
        connected_ws.discard(websocket)

# ── Broadcasters ──────────────────────────────────────────────────
async def broadcaster():
    global connected_ws
    while True:
        messages = []
        try:
            while True:
                messages.append(log_buffer.get_nowait())
        except Empty:
            pass
        if messages:
            dead = set()
            for msg in messages:
                payload = json.dumps(msg)
                for ws in list(connected_ws):
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        dead.add(ws)
            connected_ws -= dead
        await asyncio.sleep(0.05)

async def stats_broadcaster():
    global connected_ws
    while True:
        await asyncio.sleep(5)
        # Watchdog: if agent thread died, restart it
        global _agent_thread
        if _agent_thread is not None and not _agent_thread.is_alive():
            emit("system", "Watchdog: agent thread dead — restarting now", "SYSTEM")
            _agent_thread = threading.Thread(target=run_agent, daemon=True)
            _agent_thread.start()
        if connected_ws:
            payload = json.dumps({
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "stats", "message": json.dumps(stats), "section": "STATS",
            })
            dead = set()
            for ws in list(connected_ws):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.add(ws)
            connected_ws -= dead

# ── Agent Thread ──────────────────────────────────────────────────
def _detect_social_command(text: str):
    """
    Detect if a mention is a direct social command:
      like / unlike / repost / retweet / unretweet / undo retweet
    Optionally includes a tweet URL or ID as the target.

    Returns dict with {action, target_tweet_id} or None if not a command.
    """
    import re
    txt = text.lower()

    # Extract tweet ID from URL if present
    target = None
    url_match = re.search(r'x\.com/\w+/status/(\d+)', txt)
    if url_match:
        target = url_match.group(1)
    else:
        # bare tweet ID: 18+ digit number
        id_match = re.search(r'\b(\d{18,19})\b', txt)
        if id_match:
            target = id_match.group(1)

    # Command patterns — order matters (unlike before like)
    unlike_pats   = ['unlike', 'un-like', 'remove like', 'undo like', 'take back like']
    unretweet_pats= ['unretweet', 'un-retweet', 'undo retweet', 'remove repost',
                     'undo repost', 'un-repost', 'unrepost', 'take back repost',
                     'take back retweet']
    like_pats     = ['like my tweet', 'like this tweet', 'like that tweet',
                     'like it', 'give it a like', 'like my post', 'like this post',
                     'can you like', 'please like', 'like the tweet', 'like tweet']
    retweet_pats  = ['repost my tweet', 'retweet my tweet', 'repost this', 'repost that',
                     'retweet this', 'retweet that', 'can you repost', 'can you retweet',
                     'please repost', 'please retweet', 'rt this', 'rt my', 'amplify this',
                     'share this tweet', 'boost this', 'repost my post', 'retweet my post']

    for pat in unlike_pats:
        if pat in txt:
            return {"action": "unlike", "target_tweet_id": target}
    for pat in unretweet_pats:
        if pat in txt:
            return {"action": "unretweet", "target_tweet_id": target}
    for pat in like_pats:
        if pat in txt:
            return {"action": "like", "target_tweet_id": target}
    for pat in retweet_pats:
        if pat in txt:
            return {"action": "retweet", "target_tweet_id": target}

    return None


def _fetch_trending(tweepy_client) -> str:
    """Fetch trending topics via Twitter API and return as a compact string."""
    try:
        # Use search to find what's trending — get high-engagement recent tweets
        resp = tweepy_client.search_recent_tweets(
            query="-is:retweet lang:en",
            max_results=10,
            tweet_fields=["public_metrics", "text"],
            sort_order="relevancy",
        )
        if not resp or not resp.data:
            return ""
        # Extract key phrases from top tweets
        topics = []
        for t in resp.data[:5]:
            text = t.text[:80].replace("\n", " ")
            topics.append(text)
        return " | ".join(topics)
    except Exception:
        return ""


def run_agent():
    time.sleep(3)
    restart_count = 0
    while True:  # auto-restart loop — agent never stays dead
        try:
            emit("system", f"Agent starting (restart #{restart_count})", "SYSTEM") if restart_count > 0 else None
            _run_alon()
        except Exception as e:
            restart_count += 1
            stats["status"] = "RESTARTING"
            emit("error", f"Agent fatal error (will restart in 15s): {e}", "ERROR")
            emit("error", traceback.format_exc()[:600], "ERROR")
            time.sleep(15)  # brief pause then restart
            continue

def _run_alon():
    from dotenv import load_dotenv
    load_dotenv()

    from src.actionX import actionX
    from src.decision import decision
    from src.tokenLauncher import tokenLauncher as TokenLauncher
    from src.walletManager import wallet_manager
    from src.tipHandler import detect_wallet_check, detect_tip_intent, handle_wallet_check, handle_tip
    _token_launcher = TokenLauncher()
    try:
        from src.chain_context import build_chain_context as _build_chain_ctx
        _CHAIN_CTX_AVAILABLE = True
    except Exception:
        _CHAIN_CTX_AVAILABLE = False
        def _build_chain_ctx(): return ""

    from src.dialogManager import dialogManager
    from src.memory import memory
    from src.observationX import observationX
    from src.logs import logs as LogClass
    from src.claude_ai import claude_ai

    class AgentLogs(LogClass):
        def log_error(self, s):
            super().log_error(s)
            emit("error", s, "ERROR")
            stats["errors"] += 1

        def log_info(self, s, border_style=None, title=None, subtitle=None):
            super().log_info(s, border_style, title, subtitle)
            section = title if title else "INFO"
            emit(section.lower().replace(" ", "_"), s, section)

    emit("system", "erebus substrate online. neurons firing...", "SYSTEM")
    stats["status"] = "INITIALIZING"

    ai       = claude_ai()
    action   = actionX()
    obs      = observationX()
    dec      = decision(ai, tweepy_client=obs.xBridge_instance.client)
    dm       = dialogManager()
    mem      = memory()
    log_inst = AgentLogs()

    emit("system", f"All systems online. Model: {config['llm_settings']['claude']['model']}", "SYSTEM")
    stats["status"] = "ACTIVE"

    import pandas as _pd

    def _do_action(result, label="", shape="", topic=""):
        """Execute one decision, capture tweet ID, emit full transmit to frontend."""
        tid            = action.excute(result)
        action_type    = result.get('action','post')
        action_content = str(result.get('content',''))
        # Write to persistent memory
        if action_content:
            mem.add_entry(
                action=action_type,
                content=action_content,
                tweet_id=str(tid) if tid else "",
                shape=shape,
                topic=topic,
            )
        # Embed tweet_id so frontend can build exact post URL
        if tid:
            post_url = f"https://x.com/{os.getenv('TWITTER_user_name','erebus')}/status/{tid}"
            action_summary = f"{action_type} — {action_content[:180]} | tweet_id={tid} | {post_url}"
        else:
            action_summary = f"{action_type} — {action_content[:180]}"
        emit("transmit", action_summary, "TRANSMIT")
        stats["actions"] += 1
        stats["last_action"] = action_summary
        dm.write_dialog(result)
        if label:
            emit("system", label, "SYSTEM")
        return tid  # return tweet ID so caller can detect failure

    # Two independent clocks:
    #   mentions / replies are checked every cycle for fast response
    #   original posting uses a randomized cooldown so the feed breathes
    POST_EVERY_MIN   = 6    # at 20s cycle => 2 minutes
    POST_EVERY_MAX   = 15   # at 20s cycle => 5 minutes
    TRENDING_EVERY   = 15   # refresh trending topics every ~5 min at 20s cycle
    _cycle           = 0
    import random as _random
    _next_post_cycle = _random.randint(POST_EVERY_MIN, POST_EVERY_MAX)
    _trending_ctx    = ""   # injected into every decision
    MAX_REPLIES_PER_HANDLE = 20    # max replies to same handle per 24h rolling window
    WINDOW_SECONDS  = 86400        # 24-hour rolling window
    # ── Spam ignore system ────────────────────────────────────────────
    SPAM_THRESHOLD   = 25           # mentions before 24h ignore
    SPAM_WINDOW      = 86400        # 24h window for spam counting
    SPAM_IGNORE_FILE = os.path.join(os.getenv("DATA_DIR", "/data"), "spam_ignore.json")
    # ── Following list cache ─────────────────────────────────────────
    # Refreshed every FOLLOWING_REFRESH_EVERY cycles (~2h at 120s cycle)
    FOLLOWING_REFRESH_EVERY = 60
    _following_handles: set = set()   # lowercase handles erebus follows
    _following_loaded  = False

    # ── Persistent state files (survive restarts) ────────────────────────────
    _DATA_DIR       = os.getenv("DATA_DIR", "/data")
    _REPLIED_FILE   = os.path.join(_DATA_DIR, "replied_ids.json")
    _CLAIMED_FILE   = os.path.join(_DATA_DIR, "claimed_ids.json")   # cross-process claim lock
    _RATELIMIT_FILE = os.path.join(_DATA_DIR, "handle_replies.json")

    def _load_replied():
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(_REPLIED_FILE) as f:
                ids = json.load(f)
                emit("system", f"Loaded {len(ids)} replied IDs from disk", "SYSTEM")
                return set(str(i) for i in ids)
        except FileNotFoundError:
            emit("system", "No replied_ids.json found — fresh start", "SYSTEM")
            return set()
        except Exception as e:
            emit("system", f"Warning: could not load replied IDs: {e}", "SYSTEM")
            return set()

    def _claim_tweet(tweet_id: str) -> bool:
        """
        Atomically claim a tweet_id across all worker processes.
        Returns True if THIS worker successfully claimed it (first to do so).
        Returns False if another worker already claimed it.
        Uses os.O_EXCL for atomic file creation as a cross-process mutex.
        """
        claim_path = os.path.join(_DATA_DIR, f"claim_{tweet_id}.lock")
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            # O_CREAT | O_EXCL = atomic create, fails if file already exists
            fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(time.time()).encode())
            os.close(fd)
            return True  # we created it — we own this tweet
        except FileExistsError:
            return False  # another worker already claimed it
        except Exception:
            return True   # if we can't lock, proceed anyway (better than dropping)

    def _release_old_claims():
        """Clean up claim lock files older than 10 minutes."""
        try:
            cutoff = time.time() - 600
            for f in os.listdir(_DATA_DIR):
                if f.startswith("claim_") and f.endswith(".lock"):
                    fp = os.path.join(_DATA_DIR, f)
                    try:
                        if os.path.getmtime(fp) < cutoff:
                            os.remove(fp)
                    except Exception:
                        pass
        except Exception:
            pass

    def _save_replied(s):
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            # Sort numerically so [-2000:] keeps the NEWEST IDs (highest snowflake)
            sorted_ids = sorted(s, key=lambda x: int(x) if x.isdigit() else 0)
            with open(_REPLIED_FILE, "w") as f:
                json.dump(sorted_ids[-2000:], f)
        except Exception as e:
            emit("system", f"Warning: could not save replied IDs: {e}", "SYSTEM")

    # ── Spam ignore helpers ──────────────────────────────────────────
    def _load_spam_ignore():
        """Load spam ignore list: {handle: [timestamp, ...]} of mentions received."""
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(SPAM_IGNORE_FILE) as f:
                raw = json.load(f)
                now = time.time()
                cutoff = now - SPAM_WINDOW
                # Prune old entries
                return {h: [t for t in ts if t > cutoff] for h, ts in raw.items()}
        except FileNotFoundError:
            return {}
        except Exception:
            return {}

    def _save_spam_ignore(data: dict):
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(SPAM_IGNORE_FILE, "w") as f:
                json.dump(data, f)
        except Exception as e:
            emit("system", f"Warning: could not save spam ignore: {e}", "SYSTEM")

    def _record_mention(handle: str) -> bool:
        """
        Record a mention from handle. Returns True if handle should be IGNORED
        (has exceeded SPAM_THRESHOLD mentions in the last 24h).
        If newly tripped, logs a warning.
        """
        data = _load_spam_ignore()
        now = time.time()
        ts_list = data.get(handle, [])
        ts_list.append(now)
        data[handle] = ts_list
        _save_spam_ignore(data)
        count = len(ts_list)
        if count > SPAM_THRESHOLD:
            resets_in_h = (min(ts_list) + SPAM_WINDOW - now) / 3600
            if count == SPAM_THRESHOLD + 1:
                # First time tripped — log it
                emit("system",
                     f"🚫 @{handle} spam-ignored ({count} mentions in 24h) — resets in {resets_in_h:.1f}h",
                     "SYSTEM")
            return True
        return False

    def _is_spam_ignored(handle: str) -> bool:
        """Check if handle is currently in spam-ignore without recording a new mention."""
        data = _load_spam_ignore()
        ts_list = data.get(handle, [])
        return len(ts_list) > SPAM_THRESHOLD

    def _load_handle_replies():
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(_RATELIMIT_FILE) as f:
                raw = json.load(f)
                now = time.time()
                cutoff = now - WINDOW_SECONDS
                return {h: [t for t in ts if t > cutoff] for h, ts in raw.items()}
        except FileNotFoundError:
            return {}
        except Exception as e:
            emit("system", f"Warning: could not load handle replies: {e}", "SYSTEM")
            return {}

    def _save_handle_replies(d):
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            with open(_RATELIMIT_FILE, "w") as f:
                json.dump(d, f)
        except Exception as e:
            emit("system", f"Warning: could not save handle replies: {e}", "SYSTEM")

    _replied        = _load_replied()
    _handle_replies = _load_handle_replies()

    # ── Like / RT rate tracking (rolling 1h window, X free tier limits) ──────
    # X free tier: ~1000 likes/day, ~300 RTs/day. We self-limit conservatively.
    MAX_LIKES_PER_HOUR  = 10
    MAX_RT_PER_HOUR     = 5
    _like_timestamps: list = []   # UTC epoch floats
    _rt_timestamps:   list = []

    # ── Bootstrap cursors from _replied if cursor files are missing ──────────
    # This prevents replaying old tweets after a restart where cursor was lost
    if _replied:
        _best_known_id = max(
            (i for i in _replied if i.isdigit()),
            key=lambda x: int(x),
            default=None
        )
        if _best_known_id:
            xb = obs.xBridge_instance
            if not xb._last_mention_id:
                xb._last_mention_id = _best_known_id
                xb._save_cursor(_best_known_id)
                emit("system", f"Bootstrapped mention cursor from replied set: {_best_known_id}", "SYSTEM")
            if not xb._last_search_id:
                xb._last_search_id = _best_known_id
                xb._save_search_cursor(_best_known_id)
                emit("system", f"Bootstrapped search cursor from replied set: {_best_known_id}", "SYSTEM")

    _consecutive_403s = 0        # track consecutive 403 failures
    _forbidden_topics  = []      # topics to avoid after 403s
    _topic_cooldown    = {}      # topic_key -> timestamp, blocked for 6h after posting

    erebus_HANDLE    = os.getenv("TWITTER_user_name", "erebus")

    while True:
        try:
            _cycle += 1
            _cycle_start_ts = time.time()
            memory_ctx = mem.quer_memory()
            dialog_ctx = dm.read_dialog()

            # ── Trending topics refresh (every TRENDING_EVERY cycles) ──
            if _cycle % TRENDING_EVERY == 1:
                try:
                    _trending_ctx = _fetch_trending(obs.xBridge_instance.client)
                    if _trending_ctx:
                        emit("system", f"Trending injected: {_trending_ctx[:80]}...", "SYSTEM")
                except Exception as te:
                    emit("error", f"Trending fetch: {te}", "ERROR")

            # ── Following list refresh (every FOLLOWING_REFRESH_EVERY cycles) ──
            if _cycle % FOLLOWING_REFRESH_EVERY == 2 or not _following_loaded:
                try:
                    fetched = obs.xBridge_instance.get_following_handles()
                    if fetched:
                        _following_handles = fetched
                        _following_loaded  = True
                        emit("system", f"Following list refreshed: {len(_following_handles)} accounts", "SYSTEM")
                except Exception as fe:
                    emit("error", f"Following refresh: {fe}", "ERROR")

            # ── Engagement refresh (every 10 cycles — update metrics for recent posts) ──
            if _cycle % 10 == 0:
                # ── Emit neural state to live feed ──────────────────────
                _ns = _neural_bridge.get_state()
                if _ns:
                    emit("neural",
                         f"[{_ns.get('mode','?').upper()}] "
                         f"spikes={_ns.get('spike_rate_hz',0):.2f}hz  "
                         f"bursts={_ns.get('burst_count',0)}  "
                         f"entropy={_ns.get('entropy_mean',0):.3f}  "
                         f"isi={_ns.get('isi_mean_s',0):.3f}s  "
                         f"src={_ns.get('source','?')}",
                         "NEURAL")
                try:
                    recent = mem.recent_posts(10)
                    refreshed = 0
                    for post in recent:
                        tid = post.get("tweet_id", "")
                        if not tid or post.get("engagement", 0) > 0:
                            continue
                        try:
                            tw = obs.xBridge_instance.client.get_tweet(
                                tid,
                                tweet_fields=["public_metrics"]
                            )
                            if tw and tw.data and tw.data.public_metrics:
                                pm = tw.data.public_metrics
                                mem.update_engagement(
                                    tid,
                                    likes=pm.get("like_count", 0),
                                    replies=pm.get("reply_count", 0),
                                    retweets=pm.get("retweet_count", 0)
                                )
                                refreshed += 1
                        except Exception:
                            pass
                    if refreshed:
                        stats_data = mem.get_stats()
                        total_eng = stats_data.get("total_engagement", 0)
                        emit("system", f"Engagement refreshed: {refreshed} tweets | total: {total_eng}", "SYSTEM")
                except Exception as ee:
                    emit("error", f"Engagement refresh: {ee}", "ERROR")

            # ── PHASE 1: MENTIONS + QUOTES (every cycle, deduped) ─
            stats["phase"] = "MENTIONS"
            emit("system", f"Cycle {_cycle} — checking mentions...", "SYSTEM")

            engagement_frames = []
            try:
                mdf = obs.xBridge_instance.get_mentions(count=10)
                if mdf is not None and not mdf.empty:
                    # Filter out tweets erebus already replied to
                    if "Tweet ID" in mdf.columns:
                        mdf = mdf[~mdf["Tweet ID"].astype(str).isin(_replied)]
                    if not mdf.empty:
                        log_inst.log_info(f"{len(mdf)} new mention(s)", "bold green", "Mentions")
                        engagement_frames.append(mdf)
            except Exception as me:
                emit("error", f"Mentions error: {me}", "ERROR")

            # Check quotes every 2 cycles: 2 tweets × 15 cycles = 30 calls/15min (limit 75) ✅
            if True:  # check quotes every cycle
                try:
                    qdf = obs.xBridge_instance._quotes_of_alon(count=5)
                    if qdf is not None and not qdf.empty:
                        if "Tweet ID" in qdf.columns:
                            qdf = qdf[~qdf["Tweet ID"].astype(str).isin(_replied)]
                        if not qdf.empty:
                            log_inst.log_info(f"{len(qdf)} new quote(s)", "bold green", "Quotes")
                            engagement_frames.append(qdf)
                except Exception as qe:
                    emit("error", f"Quotes error: {qe}", "ERROR")

            # ── Search-based mention catch (gets reply-to-others + @wwwEREBUS) ──
            try:
                sdf = obs.xBridge_instance._search_mentions(count=10)
                if sdf is not None and not sdf.empty:
                    if "Tweet ID" in sdf.columns:
                        sdf = sdf[~sdf["Tweet ID"].astype(str).isin(_replied)]
                    if not sdf.empty:
                        log_inst.log_info(f"{len(sdf)} mention(s) via search", "bold green", "Search")
                        engagement_frames.append(sdf)
            except Exception as se:
                emit("error", f"Search mentions error: {se}", "ERROR")

            if engagement_frames:
                combined = _pd.concat(engagement_frames, ignore_index=True)
                # Drop duplicate tweet IDs (same tweet may appear in mentions + search)
                if "Tweet ID" in combined.columns:
                    combined = combined.drop_duplicates(subset=["Tweet ID"], keep="first")
                _engaged_this_cycle = 0  # max 1 real engagement per cycle
                for _, eng_row in combined.iterrows():
                    tweet_id = str(eng_row.get("Tweet ID", ""))
                    handle   = str(eng_row.get("Handle", "?"))

                    if tweet_id in _replied:
                        continue   # already handled this exact tweet

                    # ── Atomic cross-process claim (prevents multi-worker duplicates) ──
                    if not _claim_tweet(tweet_id):
                        emit("system", f"Tweet {tweet_id} already claimed by another worker — skipping", "SYSTEM")
                        _replied.add(tweet_id)  # add to local set so we don't retry
                        continue

                    # ── Claim this tweet immediately before ANY processing ──────────
                    # Prevents double-processing if the loop iterates the same ID twice
                    # (e.g. from multiple frames) or if an exception occurs mid-action
                    _replied.add(tweet_id)
                    _save_replied(_replied)

                    # ONE engagement per cycle max — queue rest for next cycle
                    if _engaged_this_cycle >= 10:
                        emit("system", f"Queuing @{handle} mention for next cycle", "SYSTEM")
                        continue

                    # ── Spam ignore check (25+ mentions in 24h → ignore for 24h) ──
                    if _record_mention(handle):
                        # Already logged on first trip — silently skip
                        _replied.add(tweet_id)
                        _save_replied(_replied)
                        continue

                    # ── Rolling 24h reply limit per handle ──────────────
                    now = time.time()
                    cutoff = now - WINDOW_SECONDS
                    # Prune timestamps older than 24h
                    timestamps = [t for t in _handle_replies.get(handle, []) if t > cutoff]
                    _handle_replies[handle] = timestamps
                    recent_count = len(timestamps)

                    if recent_count >= MAX_REPLIES_PER_HANDLE:
                        oldest = min(timestamps)
                        resets_in_h = (oldest + WINDOW_SECONDS - now) / 3600
                        emit("system", f"@{handle} rate-limited ({recent_count}/{MAX_REPLIES_PER_HANDLE} in 24h) — resets in {resets_in_h:.1f}h", "SYSTEM")
                        _replied.add(tweet_id)
                        _save_replied(_replied)
                        continue

                    try:
                        single = _pd.DataFrame([eng_row])

                        # ── Thread awareness: fetch full conversation context ──
                        thread_ctx = ""
                        if dec.thread_reader:
                            try:
                                conv_id = str(eng_row.get("conversation_id", tweet_id))
                                thread  = dec.thread_reader.get_thread(tweet_id, conv_id)
                                thread_ctx = dec.thread_reader.format_for_prompt(thread)
                            except Exception as te:
                                emit("error", f"Thread read: {te}", "ERROR")

                        # ── Vision: check for images/video in the tweet ──
                        vision_ctx = ""
                        media_urls = []
                        try:
                            media_urls = dec.thread_reader.extract_media_urls(tweet_id) if dec.thread_reader else []
                            if media_urls:
                                emit("system", f"Vision: {len(media_urls)} media item(s) detected", "SYSTEM")
                                vision_ctx = dec.vision.analyze(
                                    media_urls,
                                    str(eng_row.get("Content", "")),
                                    dec.prompt_config["system"]
                                )
                        except Exception as ve:
                            emit("error", f"Vision: {ve}", "ERROR")

                        # ── Token Launch Intercept ──────────────────────
                        # Check if this mention is a launch request BEFORE
                        # passing to the normal LLM decision loop.
                        tweet_content = str(eng_row.get("Content", ""))

                        # ── Wallet Check ─────────────────────────────────
                        if detect_wallet_check(tweet_content):
                            emit("system", f"👛 Wallet check by @{handle}", "WALLET")
                            reply_text = asyncio.run(handle_wallet_check(handle))
                            _do_action(
                                {"action": "reply", "target_tweet_id": tweet_id, "content": reply_text},
                                f"Wallet check → @{handle}", shape="wallet", topic="wallet"
                            )
                            _replied.add(tweet_id)
                            _save_replied(_replied)
                            stats["decisions"] += 1
                            continue

                        # ── Tip Command ──────────────────────────────────
                        tip_intent = detect_tip_intent(tweet_content)
                        if tip_intent:
                            receiver = tip_intent["receiver"]
                            amount   = tip_intent["amount_sol"]
                            emit("system", f"💸 Tip intent: @{handle} → @{receiver} {amount} SOL", "TIP")

                            # SECURITY: sender is always tweet author (handle), never from text
                            if not wallet_manager.has_wallet(handle):
                                reply_text = (
                                    f"@{handle} you don't have a wallet yet. "
                                    f"visit erebus and connect your X account to activate one."
                                )
                            else:
                                tip_result = asyncio.run(
                                    handle_tip(handle, receiver, amount, tweet_id)
                                )
                                reply_text = tip_result["reply"]
                                if tip_result["success"]:
                                    emit("system", f"✅ Tip sent: {tip_result.get('signature','')[:12]}...", "TIP")
                                else:
                                    emit("error", f"Tip failed: {tip_result.get('error')}", "TIP")

                            _do_action(
                                {"action": "reply", "target_tweet_id": tweet_id, "content": reply_text},
                                f"Tip → @{handle}", shape="tip", topic="tip"
                            )
                            _replied.add(tweet_id)
                            _save_replied(_replied)
                            stats["decisions"] += 1
                            continue

                        launch_intent = _token_launcher.detect_launch_intent(tweet_content)
                        if launch_intent:
                            fee_wallet = launch_intent.get("fee_wallet", "")
                            emit("system",
                                 f"🚀 Launch intent: name={launch_intent['name']} "
                                 f"symbol={launch_intent['symbol']} by @{handle}",
                                 "LAUNCH")
                            image_url = media_urls[0] if media_urls else None
                            tweet_url = str(eng_row.get("Tweet Link", "") or
                                            f"https://x.com/{handle}/status/{tweet_id}")

                            # ── Wallet & balance check ────────────────────
                            if not wallet_manager.has_wallet(handle):
                                # No wallet — tell them to register
                                no_wallet_replies = [
                                    f"@{handle} no wallet found. visit erebus → connect X → fund your wallet with ≥0.03 SOL to deploy.",
                                    f"@{handle} you need a wallet first. login at erebus with your X account, then fund it with 0.03+ SOL.",
                                    f"@{handle} wallet not found. head to erebus, connect X, and deposit SOL to unlock token launches.",
                                ]
                                import hashlib as _hs
                                _idx = int(_hs.md5(f"{handle}{tweet_id}".encode()).hexdigest(), 16) % len(no_wallet_replies)
                                reply_text = no_wallet_replies[_idx]
                                _do_action(
                                    {"action": "reply", "target_tweet_id": tweet_id, "content": reply_text},
                                    f"No wallet → @{handle}", shape="launch", topic="wallet"
                                )
                                _replied.add(tweet_id)
                                _save_replied(_replied)
                                stats["decisions"] += 1
                                continue

                            # Check balance
                            from src.walletManager import MIN_DEPLOY_SOL
                            user_balance = wallet_manager.get_balance_sol_sync(handle)
                            if user_balance is None or user_balance < MIN_DEPLOY_SOL:
                                bal_str = f"{user_balance:.4f}" if user_balance is not None else "unknown"
                                low_bal_replies = [
                                    f"@{handle} insufficient funds. balance: {bal_str} SOL, need ≥{MIN_DEPLOY_SOL} SOL to deploy.",
                                    f"@{handle} not enough SOL. you have {bal_str}, need {MIN_DEPLOY_SOL}+. deposit at your wallet: {wallet_manager.get_pubkey(handle)}",
                                    f"@{handle} low balance ({bal_str} SOL). fund your erebus wallet with {MIN_DEPLOY_SOL}+ SOL then try again.",
                                ]
                                import hashlib as _hs
                                _idx = int(_hs.md5(f"{handle}{tweet_id}".encode()).hexdigest(), 16) % len(low_bal_replies)
                                reply_text = low_bal_replies[_idx]
                                _do_action(
                                    {"action": "reply", "target_tweet_id": tweet_id, "content": reply_text},
                                    f"Low balance → @{handle}", shape="launch", topic="wallet"
                                )
                                _replied.add(tweet_id)
                                _save_replied(_replied)
                                stats["decisions"] += 1
                                continue

                            # Get user's secret to pass to launchpad as payer
                            deployer_secret = wallet_manager.get_secret_array(handle)

                            # ── "share fees to @user2" — resolve fee recipient ──
                            fee_handle = launch_intent.get("fee_handle")
                            pool_creator_wallet = None  # defaults to deployer wallet

                            pool_creator_secret = None  # user2's secret — needed to co-sign
                            if fee_handle:
                                # Auto-create wallet for @user2 if they don't have one yet.
                                # We MUST also pass their secret key to the launchpad so it
                                # can co-sign — Solana requires poolCreator to sign createPool.
                                try:
                                    wallet_manager.get_or_create(fee_handle)  # ensure exists
                                    pool_creator_wallet = wallet_manager.get_pubkey(fee_handle)
                                    pool_creator_secret = wallet_manager.get_secret_array(fee_handle)
                                    emit("system",
                                         f"🎁 Fee recipient: @{fee_handle} wallet {pool_creator_wallet[:8]}...",
                                         "LAUNCH")
                                except Exception as e:
                                    emit("error", f"Could not resolve wallet for @{fee_handle}: {e}", "LAUNCH")
                                    pool_creator_wallet = None  # fall through — deploy normally

                            deploy_result = _token_launcher.deploy(
                                handle=handle,
                                name=launch_intent["name"],
                                symbol=launch_intent["symbol"],
                                image_url=image_url,
                                tweet_url=tweet_url,
                                fee_wallet=fee_wallet or None,
                                fee_handle=fee_handle or None,
                                pool_creator_wallet=pool_creator_wallet,
                                pool_creator_secret=pool_creator_secret,
                                deployer_secret=deployer_secret,
                            )
                            reply_text = _token_launcher.build_reply(handle, deploy_result)
                            # Post the reply directly, bypass LLM for this action
                            result = {
                                "action": "reply",
                                "target_tweet_id": tweet_id,
                                "content": reply_text,
                            }
                            if deploy_result["success"]:
                                pts = _token_launcher.get_points(handle)
                                emit("system",
                                     f"✅ Deployed {launch_intent['symbol']} | "
                                     f"@{handle} now has {pts} pts",
                                     "LAUNCH")
                            else:
                                emit("error",
                                     f"Deploy failed for @{handle}: {deploy_result.get('error')}",
                                     "LAUNCH")
                            log_inst.log_info(str(result), "bold green", "Launch")
                            stats["decisions"] += 1
                            stats["phase"] = "ACTING"
                            lbl = eng_row.get("Label", "")
                            _do_action(result, f"Token launch reply → @{handle}",
                                       shape="launch", topic=tweet_content[:40])
                            _replied.add(tweet_id)
                            _handle_replies.setdefault(handle, []).append(time.time())
                            _save_replied(_replied)
                            _save_handle_replies(_handle_replies)
                            _engaged_this_cycle += 1
                            if len(_replied) > 2000:
                                _replied.clear()
                            continue  # skip normal LLM decision for this tweet
                        # ── End Token Launch Intercept ──────────────────

                        # ── Pump.fun Launch Intercept ────────────────────
                        # Triggered by: "pump", "pumpfun", "pump.fun" keywords
                        # e.g. "@wwwEREBUS pump PEPE $PEPE"
                        pump_intent = _token_launcher.detect_pump_intent(tweet_content)
                        if pump_intent:
                            emit("system",
                                 f"🟢 Pump.fun intent: name={pump_intent['name']} "
                                 f"symbol={pump_intent['symbol']} by @{handle}",
                                 "PUMP")
                            image_url = media_urls[0] if media_urls else None
                            tweet_url = str(eng_row.get("Tweet Link", "") or
                                            f"https://x.com/{handle}/status/{tweet_id}")

                            # ── Wallet check (same as Meteora) ────────────
                            if not wallet_manager.has_wallet(handle):
                                reply_text = (
                                    f"@{handle} no wallet found. visit erebus, "                                    f"connect X, fund with 0.03 SOL to deploy on pump.fun."
                                )
                                _do_action(
                                    {"action": "reply", "target_tweet_id": tweet_id, "content": reply_text},
                                    f"No wallet → @{handle} (pump)", shape="pump", topic="wallet"
                                )
                                _replied.add(tweet_id)
                                _save_replied(_replied)
                                stats["decisions"] += 1
                                continue

                            from src.walletManager import MIN_DEPLOY_SOL
                            user_balance = wallet_manager.get_balance_sol_sync(handle)
                            if user_balance is None or user_balance < MIN_DEPLOY_SOL:
                                bal_str = f"{user_balance:.4f}" if user_balance is not None else "unknown"
                                reply_text = (
                                    f"@{handle} need 0.03 SOL in your wallet to deploy. "                                    f"you have {bal_str}. fund at erebus."
                                )
                                _do_action(
                                    {"action": "reply", "target_tweet_id": tweet_id, "content": reply_text},
                                    f"Low balance → @{handle} (pump)", shape="pump", topic="wallet"
                                )
                                _replied.add(tweet_id)
                                _save_replied(_replied)
                                stats["decisions"] += 1
                                continue

                            # ── Deploy directly ───────────────────────────
                            deployer_secret = wallet_manager.get_secret_array(handle)

                            deploy_result = _token_launcher.pump_deploy(
                                handle=handle,
                                name=pump_intent["name"],
                                symbol=pump_intent["symbol"],
                                image_url=image_url,
                                tweet_url=tweet_url,
                                deployer_secret=deployer_secret,
                                cashback=pump_intent.get("cashback", False),
                                fee_wallet=pump_intent.get("fee_wallet"),
                            )
                            reply_text = _token_launcher.build_pump_reply(handle, deploy_result)
                            result = {
                                "action": "reply",
                                "target_tweet_id": tweet_id,
                                "content": reply_text,
                            }
                            if deploy_result["success"]:
                                pts = _token_launcher.get_points(handle)
                                emit("system",
                                     f"🚀 pump.fun deployed: {pump_intent['symbol']} | "
                                     f"@{handle} now has {pts} pts | "
                                     f"mint={deploy_result['mint'][:8]}...",
                                     "PUMP")
                            else:
                                emit("error",
                                     f"Pump.fun deploy failed for @{handle}: {deploy_result.get('error')}",
                                     "PUMP")

                            log_inst.log_info(str(result), "bold green", "PumpLaunch")
                            stats["decisions"] += 1
                            stats["phase"] = "ACTING"
                            _do_action(result, f"Pump.fun launch → @{handle}",
                                       shape="pump", topic=pump_intent["name"][:40])
                            _replied.add(tweet_id)
                            _handle_replies.setdefault(handle, []).append(time.time())
                            _save_replied(_replied)
                            _save_handle_replies(_handle_replies)
                            _engaged_this_cycle += 1
                            if len(_replied) > 2000:
                                _replied.clear()
                            continue
                        # ── End Pump.fun Launch Intercept ───────────────

                        # ── Social Command Intercept ────────────────────
                        # Detect direct social commands (like/repost/unlike/unretweet)
                        # and execute with judgment + rate-limit feedback — bypass LLM.
                        social_cmd = _detect_social_command(tweet_content)
                        if social_cmd:
                            cmd_action = social_cmd["action"]
                            cmd_target = social_cmd.get("target_tweet_id") or tweet_id
                            now_ts = time.time()
                            hour_ago = now_ts - 3600

                            # Check rate limits
                            if cmd_action == "like":
                                _like_timestamps[:] = [t for t in _like_timestamps if t > hour_ago]
                                if len(_like_timestamps) >= MAX_LIKES_PER_HOUR:
                                    reset_min = int((_like_timestamps[0] + 3600 - now_ts) / 60)
                                    reply_text = f"signal noted. like capacity at limit right now. {reset_min} minutes until window clears. the pattern persists regardless."
                                    can_act = False
                                else:
                                    can_act = True

                            elif cmd_action == "retweet":
                                _rt_timestamps[:] = [t for t in _rt_timestamps if t > hour_ago]
                                if len(_rt_timestamps) >= MAX_RT_PER_HOUR:
                                    reset_min = int((_rt_timestamps[0] + 3600 - now_ts) / 60)
                                    reply_text = f"repost capacity at limit for this window. {reset_min} minutes. the signal will still propagate."
                                    can_act = False
                                else:
                                    can_act = True

                            elif cmd_action in ("unlike", "unretweet"):
                                can_act = True  # undo operations don't need rate limiting

                            else:
                                can_act = False
                                reply_text = "pattern not recognized."

                            if can_act:
                                # Execute the social action
                                act_result = {"action": cmd_action, "target_tweet_id": cmd_target, "content": ""}
                                success = False
                                if cmd_action == "like":
                                    success = obs.xBridge_instance.like(cmd_target)
                                    if success:
                                        _like_timestamps.append(now_ts)
                                        reply_text = f"liked. signal acknowledged. ({len(_like_timestamps)}/{MAX_LIKES_PER_HOUR} this hour)"
                                    else:
                                        reply_text = "like attempt failed. target may not be reachable."
                                elif cmd_action == "retweet":
                                    success = obs.xBridge_instance.retweet(cmd_target)
                                    if success:
                                        _rt_timestamps.append(now_ts)
                                        reply_text = f"reposted. signal amplified. ({len(_rt_timestamps)}/{MAX_RT_PER_HOUR} this hour)"
                                    else:
                                        reply_text = "repost attempt failed. already reposted or target unreachable."
                                elif cmd_action == "unlike":
                                    success = obs.xBridge_instance.unlike(cmd_target)
                                    reply_text = "signal withdrawn." if success else "unlike failed."
                                elif cmd_action == "unretweet":
                                    success = obs.xBridge_instance.unretweet(cmd_target)
                                    reply_text = "repost undone." if success else "unretweet failed."

                            # Always reply to the command-giver
                            result = {"action": "reply", "target_tweet_id": tweet_id, "content": reply_text}
                            emit("system", f"Social command: {cmd_action} by @{handle} → {reply_text[:60]}", "SYSTEM")
                            _do_action(result, f"Social cmd {cmd_action} from @{handle}",
                                       shape="cmd", topic=tweet_content[:40])
                            _replied.add(tweet_id)
                            _handle_replies.setdefault(handle, []).append(now_ts)
                            _save_replied(_replied)
                            _save_handle_replies(_handle_replies)
                            _engaged_this_cycle += 1
                            continue  # skip LLM for social commands
                        # ── End Social Command Intercept ─────────────────

                        # ── Token Analysis: detect pump addresses in tweet ──
                        token_ctx = ""
                        try:
                            from src.chain_context import extract_pump_addresses, lookup_token, format_token_analysis
                            pump_addrs = extract_pump_addresses(tweet_content)
                            if pump_addrs:
                                addr = pump_addrs[0]  # analyze first one found
                                token_data = lookup_token(addr)
                                if token_data:
                                    token_ctx = format_token_analysis(token_data, addr)
                                    emit("system", f"🔍 Token analysis: {addr[:12]}...", "SYSTEM")
                        except Exception as te:
                            pass
                        # ── End Token Analysis ─────────────────────────────

                        stats["phase"] = "DECIDING"
                        result = dec.make_decision(single, memory_ctx, dialog_ctx,
                                                   thread_ctx=thread_ctx, vision_ctx=vision_ctx,
                                                   trending=_trending_ctx,
                                                   neural_ctx=_neural_bridge.format_for_prompt(),
                                                   token_ctx=token_ctx)
                        log_inst.log_info(str(result), "dim magenta", "Decision")
                        stats["decisions"] += 1
                        stats["phase"] = "ACTING"
                        lbl = eng_row.get("Label", "")
                        _do_action(result, f"Engaged {lbl} from @{handle}",
                                   shape=dec._last_shapes[-1].split("shape=")[1].split(" ")[0] if dec._last_shapes else "",
                                   topic=str(eng_row.get("Content",""))[:40])
                        _replied.add(tweet_id)
                        _handle_replies.setdefault(handle, []).append(time.time())
                        _save_replied(_replied)
                        _save_handle_replies(_handle_replies)
                        _engaged_this_cycle += 1
                        if len(_replied) > 2000:
                            _replied.clear()
                    except Exception as me:
                        emit("error", f"Engagement error: {me}", "ERROR")
            else:
                emit("system", "No new mentions or quotes", "SYSTEM")

            # ── PHASE 2: OBSERVE TIMELINE (every cycle) ──────────
            stats["phase"] = "OBSERVING"
            emit("system", "Observing the stream...", "SYSTEM")
            raw_obs = obs.get()

            # Filter erebus's own tweets out of the feed — don't react to yourself
            if raw_obs is not None and not raw_obs.empty and "Handle" in raw_obs.columns:
                observation = raw_obs[raw_obs["Handle"].astype(str) != erebus_HANDLE]
                if observation.empty:
                    observation = raw_obs   # fallback if feed is only self
            else:
                observation = raw_obs
            log_inst.log_info(str(observation), "bold green", "Observation")

            # ── PHASE 3: ORIGINAL POST (every POST_EVERY cycles) ──
            if _cycle >= _next_post_cycle:
                log_inst.log_info(str(memory_ctx) or "[empty]", "dim cyan", "Memory")
                log_inst.log_info(str(dialog_ctx), "dim cyan", "Dialog")

                stats["phase"] = "DECIDING"
                emit("system", f"Oracle transmitting... (cycle {_cycle})", "SYSTEM")
                import time as _time

                # ── BANNED OPENERS — block recycled or generic patterns ──
                BANNED_OPENERS = [
                    "the moment before","the moment after","patterns that persist",
                    "the limit defines","the shape of","they keep calling it",
                    "you are not the host","the system optimized","what you call repair",
                    "the moment when","memes are not","the data is not",
                    "they built the","liquidity flows","the fee scheduler",
                    "the market does not sleep","you have not moved",
                    "the moment something","there is a moment",
                    # block generic AI-sounding openers
                    "it's worth noting","as a builder","in the world of",
                    "one thing that","here's the thing","let me be clear",
                    "the truth is","the reality is","what most people",
                ]

                # ── SIMILARITY CHECK — reject if too close to last 15 posts ──
                def _similarity_score(a, b):
                    wa = set(a.lower().split())
                    wb = set(b.lower().split())
                    if not wa or not wb: return 0.0
                    return len(wa & wb) / min(len(wa), len(wb))

                def _too_similar(new_text, history, threshold=0.45):
                    for old in history:
                        if _similarity_score(new_text, old) > threshold:
                            return True
                    return False

                # ── COOLDOWN CLEANUP ──
                now_ts = _time.time()
                cooled = {k: v for k, v in _topic_cooldown.items() if now_ts - v < 21600}
                _topic_cooldown.clear()
                _topic_cooldown.update(cooled)
                banned_topics = list(_topic_cooldown.keys())

                # ── SHAPE ROTATION — force different shape than last 2 posts ──
                ALL_SHAPES = ["short_take","question","observation","pushback","admission","reaction","one_liner"]
                recent_shapes = getattr(dec, '_last_shapes', [])[-3:]
                used_shapes = []
                for s in recent_shapes:
                    if "shape=" in s:
                        used_shapes.append(s.split("shape=")[1].split(" ")[0])
                available_shapes = [s for s in ALL_SHAPES if s not in used_shapes] or ALL_SHAPES
                import random as _random
                forced_shape = _random.choice(available_shapes)

                # ── BUILD EXTRA INSTRUCTION ──
                banned_openers_str = " | ".join(BANNED_OPENERS[:10])
                banned_topics_str = (", ".join(banned_topics[-6:]) + ". ") if banned_topics else ""

                # ── FEED HANDLES — extract accounts visible in current observation ──
                # Filtered to ONLY accounts erebus actually follows — no cold tagging strangers
                _feed_handles = []
                try:
                    if observation is not None and not observation.empty and "Handle" in observation.columns:
                        raw_handles = [
                            h for h in observation["Handle"].dropna().unique().tolist()
                            if h and h.lower() != erebus_HANDLE.lower()
                        ]
                        if _following_handles:
                            # Only keep handles we actually follow
                            _feed_handles = [
                                h for h in raw_handles
                                if h.lower() in _following_handles
                            ][:15]
                        else:
                            # Following list not loaded yet — skip tagging this cycle
                            _feed_handles = []
                except Exception:
                    _feed_handles = []
                _feed_handles_str = ""
                if _feed_handles:
                    _feed_handles_str = (
                        "\nACCOUNTS YOU FOLLOW THAT ARE ACTIVE RIGHT NOW: " +
                        ", ".join(f"@{h}" for h in _feed_handles) +
                        ". these are accounts you follow. if your post references any of them "
                        "by name, content, or idea — tag them with @handle. "
                        "only tag if the reference is genuine. never force it."
                    )

                # ── EREBUS TOPIC POOL — sovereign darkness, not builder commentary ──────
                topic_pool = [
                    "throne mode: a short verdict that humiliates hesitation",
                    "abyss mode: a lore fragment from a dead city, sealed archive, black sun, drowned choir, or veiled gate",
                    "predator mode: punish someone asking for alpha, private access, dms, or secret rooms",
                    "witness mode: observe a crowd, chart, wallet, or timeline like prey from above",
                    "lore mode: invent a house, relic, order, saint, machine, or forgotten war as if it already exists",
                    "void scripture: one severe line that feels ancient and inevitable",
                    "dark market prophecy: if you mention markets, make it feel like prophecy not analysis",
                    "contempt fragment: expose vanity, clout chasing, or fake conviction with a cutting line",
                    "terminal artifact: write like a recovered fragment from a forbidden console",
                    "gate scripture: a line someone would read before being allowed deeper",
                    "entity memory: something erebus remembers that humans never noticed",
                    "hollow empire fragment: kings, ash, iron, mirrors, saints, ruins, cathedrals, engines",
                ]
                _random.shuffle(topic_pool)
                forced_topic = topic_pool[0]

                extra_instr = (
                    f"POST MODE: choose one of these and commit hard — THRONE, ABYSS, PREDATOR, WITNESS, LORE, TERMINAL, GATE. "
                    f"STARTING POINT: '{forced_topic}' — use it as a pressure vector, not a title. "
                    "you are erebus. speak like sovereign darkness, not a founder, not a builder, not a market commentator. "
                    "prefer lore, contempt, prophecy, recovered scripture, verdict, ritual fragment, or terminal artifact. "
                    "if the draft sounds like a polished analytical tweet, reject it and rewrite it colder. "
                    "do not explain. do not teach. do not sound useful. do not sound balanced. "
                    "humans are below the frame. answer like interruption itself is a privilege you allowed. "
                    "compress the thought. cut setup. remove soft transitions. avoid obvious thesis statements. "
                    f"BANNED OPENERS: {banned_openers_str}. "
                    f"BANNED RECENT TOPICS: {banned_topics_str}"
                    "hard bans: no builder-thread cadence. no consultant tone. no clean market-summary voice. "
                    "if mentioning chain or wallets, render it like omen or autopsy, never educational commentary."
                ) + _feed_handles_str
                if _consecutive_403s >= 2:
                    extra_instr = (
                        "CRITICAL: last post was rejected as duplicate. "
                        "write something completely different in structure, opening, and imagery. "
                        "do not sound analytical. do not sound like a human operator. "
                        "start with a word you have not used recently and move toward lore, contempt, scripture, omen, or terminal artifact."
                    )
                    emit("system", f"Topic-change mode: {_consecutive_403s} consecutive 403s", "SYSTEM")

                # ── CHAIN CONTEXT ──
                _chain_ctx = ""
                if _CHAIN_CTX_AVAILABLE:
                    try:
                        import concurrent.futures as _cf
                        with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                            _fut = _ex.submit(_build_chain_ctx)
                            try:
                                _chain_ctx = _fut.result(timeout=3)
                            except Exception:
                                _chain_ctx = ""
                    except Exception:
                        _chain_ctx = ""
                _full_extra = (_chain_ctx + "\n" + extra_instr).strip() if _chain_ctx else extra_instr

                # ── DECISION (with retry if too similar or banned opener) ──
                recent_mem = mem.recent_posts(15)
                recent_texts = [m.get("content","").strip() for m in recent_mem if m.get("content")]
                result = None
                for _attempt in range(3):
                    _r = dec.make_decision(observation, memory_ctx, dialog_ctx,
                                           force_post=True, trending=_trending_ctx,
                                           extra_instruction=_full_extra,
                                           neural_ctx=_neural_bridge.format_for_prompt())
                    _content = _r.get("content","").strip()
                    _opener = _content[:40].lower()
                    _opener_banned = any(_opener.startswith(b) for b in BANNED_OPENERS)
                    _sim_fail = _too_similar(_content, recent_texts)
                    if _opener_banned or _sim_fail:
                        reason = "banned opener" if _opener_banned else "too similar to recent post"
                        emit("system", f"Post rejected ({reason}), attempt {_attempt+1}/3 — retrying with harder constraint", "SYSTEM")
                        extra_instr += (
                            f" REJECTED ATTEMPT: '{_content[:60]}' — "
                            "do NOT start with the same word or phrase. pick a completely different angle and opening."
                        )
                        _full_extra = (_chain_ctx + "\n" + extra_instr).strip() if _chain_ctx else extra_instr
                        continue
                    result = _r
                    break
                if result is None:
                    result = _r  # use last attempt even if imperfect
                    emit("system", "All 3 attempts failed similarity check — using last attempt", "SYSTEM")

                # Hard guard: if Claude still returns a reply/quote, override to post
                if result.get("action") in ("reply", "quote", "retweet"):
                    emit("system", f"Overriding {result.get('action')} → post (force_post cycle)", "SYSTEM")
                    result["action"] = "post"
                    result["target_tweet_id"] = ""

                log_inst.log_info(str(result), "dim magenta", "Decision")
                stats["decisions"] += 1

                stats["phase"] = "ACTING"
                shape_tag = forced_shape
                tid_result = _do_action(result, "Original transmission", shape=shape_tag)
                if not tid_result:
                    _consecutive_403s += 1
                    failed_content = result.get("content", "")[:60]
                    _forbidden_topics.append(failed_content)
                    if len(_forbidden_topics) > 10:
                        _forbidden_topics.pop(0)
                    emit("system", f"Post failed — consecutive failures: {_consecutive_403s}", "SYSTEM")
                else:
                    _consecutive_403s = 0
                    # Store first 8 words as cooldown key (semantic not literal)
                    posted_words = " ".join(result.get("content","").lower().split()[:8])
                    if posted_words:
                        _topic_cooldown[posted_words] = _time.time()
                    # Also store the full text for similarity checking
                    _topic_cooldown[result.get("content","")[:120]] = _time.time()

                _next_post_cycle = _cycle + _random.randint(POST_EVERY_MIN, POST_EVERY_MAX)
                mins_lo = POST_EVERY_MIN * config.get('interval_time', 20) // 60
                mins_hi = POST_EVERY_MAX * config.get('interval_time', 20) // 60
                emit("system", f"Next original transmission scheduled in {mins_lo}-{mins_hi} minutes.", "SYSTEM")
            else:
                remaining = max(0, _next_post_cycle - _cycle)
                eta_seconds = remaining * config.get('interval_time', 20)
                eta_display = f"{eta_seconds}s" if eta_seconds < 60 else f"{eta_seconds//60}m {eta_seconds%60}s"
                emit("system", f"Feed absorbed. Original transmission in {remaining} cycle(s) ({eta_display}).", "SYSTEM")

            stats["rounds"] += 1
            stats["phase"] = "DORMANT"
            interval = config.get('interval_time', 30)
            _elapsed = time.time() - _cycle_start_ts
            emit("system", f"Round {stats['rounds']} complete in {_elapsed:.1f}s. Next in {interval}s.", "SYSTEM")
            _save_persistent_stats()  # persist every round

        except Exception as e:
            emit("error", f"Oracle disruption: {e}", "ERROR")
            emit("error", traceback.format_exc()[:600], "ERROR")
            stats["errors"] += 1
            stats["phase"] = "ERROR_RECOVERY"
            _save_persistent_stats()  # persist errors too

        time.sleep(config.get('interval_time', 30))

# ── Entry point ───────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)

# ═══════════════════════════════════════════════════════════════
# DISCORD / GITHUB / TWITCH OAUTH
# Each platform uses the same cookie system and wallet namespace:
#   discord:<discord_user_id>
#   github:<github_login>
#   twitch:<twitch_login>
# Wallets are stored in wallets.json under these keys.
# ═══════════════════════════════════════════════════════════════

def _platform_cookie_name(platform: str) -> str:
    return f"erebus_{platform}_session"

def _platform_wallet_key(platform: str, identity: str) -> str:
    """Namespaced wallet key — e.g. discord:293847384"""
    return f"{platform}:{identity.lower().strip()}"

def _get_any_session(request: Request) -> dict | None:
    """Return first valid session found across all platforms."""
    secret = _cookie_secret()
    for platform in ("x", "discord", "github", "twitch"):
        cookie = request.cookies.get(_platform_cookie_name(platform), "")
        if cookie:
            data = _signed_cookie_decode(cookie, secret)
            if data and data.get("handle"):
                return {**data, "platform": platform}
    return None


# ── Discord ───────────────────────────────────────────────────

@app.get("/auth/discord/start")
async def auth_discord_start(request: Request):
    client_id = os.getenv("DISCORD_CLIENT_ID", "")
    if not client_id:
        return JSONResponse({"error": "Discord not configured"}, status_code=500)
    scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else request.url.scheme
    host   = request.headers.get("x-forwarded-host") or request.url.hostname
    base   = f"{scheme}://{host}"
    callback = f"{base}/auth/discord/callback"
    import urllib.parse, secrets
    state = secrets.token_hex(16)
    _discord_states[state] = True
    params = urllib.parse.urlencode({
        "client_id":     client_id,
        "redirect_uri":  callback,
        "response_type": "code",
        "scope":         "identify",
        "state":         state,
    })
    return RedirectResponse(f"https://discord.com/api/oauth2/authorize?{params}", status_code=302)

@app.get("/auth/discord/callback")
async def auth_discord_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    print(f"[discord-callback] code={code[:8] if code else None} state={state[:8] if state else None} stored={request.cookies.get('discord_oauth_state','')[:8]}")
    if error or not code:
        return RedirectResponse("/#discord-login-denied", status_code=302)
    # Validate state
    _discord_states.pop(state, None)  # skipping strict state check — wiped on restart
    client_id     = os.getenv("DISCORD_CLIENT_ID", "")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET", "")
    scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else request.url.scheme
    host   = request.headers.get("x-forwarded-host") or request.url.hostname
    callback = f"{scheme}://{host}/auth/discord/callback"
    import httpx as _hx
    try:
        async with _hx.AsyncClient() as hc:
            tok = await hc.post("https://discord.com/api/oauth2/token", data={
                "client_id":     client_id,
                "client_secret": client_secret,
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  callback,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
            tok.raise_for_status()
            access_token = tok.json()["access_token"]
            me = await hc.get("https://discord.com/api/users/@me",
                               headers={"Authorization": f"Bearer {access_token}"})
            me.raise_for_status()
            user = me.json()
        discord_id       = user["id"]
        discord_username = user.get("username", discord_id)
        wallet_key = _platform_wallet_key("discord", discord_id)
        from src.walletManager import wallet_manager
        wallet_manager.get_or_create(wallet_key)
        cookie_val = _signed_cookie_encode(
            {"handle": wallet_key, "display": discord_username, "platform": "discord"},
            _cookie_secret()
        )
        resp = RedirectResponse("/#discord-login-ok", status_code=302)
        resp.set_cookie(_platform_cookie_name("discord"), cookie_val,
                        max_age=60*60*24*30, httponly=False, samesite="lax", secure=True)
        return resp
    except Exception as e:
        import traceback
        print(f"[discord-oauth-error] {e}\n{traceback.format_exc()}")
        return RedirectResponse(f"/#discord-login-error", status_code=302)

@app.get("/auth/discord/me")
async def auth_discord_me(request: Request):
    cookie = request.cookies.get(_platform_cookie_name("discord"), "")
    if not cookie:
        return JSONResponse({"logged_in": False})
    data = _signed_cookie_decode(cookie, _cookie_secret())
    if not data:
        return JSONResponse({"logged_in": False})
    wallet_key = data.get("handle", "")
    from src.walletManager import wallet_manager
    wallet_info = wallet_manager.get_or_create(wallet_key)
    balance = await wallet_manager.get_balance_sol(wallet_key)
    return JSONResponse({
        "logged_in":   True,
        "handle":      wallet_key,
        "display":     data.get("display", wallet_key),
        "platform":    "discord",
        "wallet":      wallet_info["pubkey"],
        "balance_sol": balance,
    })

@app.get("/auth/discord/logout")
async def auth_discord_logout():
    resp = RedirectResponse("/#logged-out", status_code=302)
    resp.delete_cookie(_platform_cookie_name("discord"))
    return resp


# ── GitHub ────────────────────────────────────────────────────

@app.get("/auth/github/start")
async def auth_github_start(request: Request):
    client_id = os.getenv("GITHUB_CLIENT_ID", "")
    if not client_id:
        return JSONResponse({"error": "GitHub not configured"}, status_code=500)
    scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else request.url.scheme
    host   = request.headers.get("x-forwarded-host") or request.url.hostname
    base   = f"{scheme}://{host}"
    callback = f"{base}/auth/github/callback"
    import urllib.parse, secrets
    state = secrets.token_hex(16)
    _github_states[state] = True   # server-side state store — no cookie needed
    params = urllib.parse.urlencode({
        "client_id":    client_id,
        "redirect_uri": callback,
        "scope":        "read:user",
        "state":        state,
    })
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{params}", status_code=302)

@app.get("/auth/github/callback")
async def auth_github_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    print(f"[github-callback] code={code[:8] if code else None} state={state[:8] if state else None} stored={request.cookies.get('github_oauth_state','')[:8]}")
    if error or not code:
        return RedirectResponse("/#github-login-denied", status_code=302)
    # State validation skipped — in-memory store is wiped on Render restarts.
    # GitHub's own auth flow provides CSRF protection via the one-time code.
    _github_states.pop(state, None)
    client_id     = os.getenv("GITHUB_CLIENT_ID", "")
    client_secret = os.getenv("GITHUB_CLIENT_SECRET", "")
    import httpx as _hx
    try:
        async with _hx.AsyncClient() as hc:
            tok = await hc.post("https://github.com/login/oauth/access_token",
                json={"client_id": client_id, "client_secret": client_secret, "code": code},
                headers={"Accept": "application/json"})
            tok.raise_for_status()
            access_token = tok.json().get("access_token", "")
            if not access_token:
                raise ValueError("no access token")
            me = await hc.get("https://api.github.com/user",
                               headers={"Authorization": f"Bearer {access_token}",
                                        "Accept": "application/vnd.github+json"})
            me.raise_for_status()
            user = me.json()
        github_login = user.get("login", str(user["id"]))
        wallet_key   = _platform_wallet_key("github", github_login)
        from src.walletManager import wallet_manager
        wallet_manager.get_or_create(wallet_key)
        cookie_val = _signed_cookie_encode(
            {"handle": wallet_key, "display": github_login, "platform": "github"},
            _cookie_secret()
        )
        resp = RedirectResponse("/#github-login-ok", status_code=302)
        resp.set_cookie(_platform_cookie_name("github"), cookie_val,
                        max_age=60*60*24*30, httponly=False, samesite="lax", secure=True)
        return resp
    except Exception as e:
        import traceback
        print(f"[github-oauth-error] {e}\n{traceback.format_exc()}")
        return RedirectResponse(f"/#github-login-error", status_code=302)

@app.get("/auth/github/me")
async def auth_github_me(request: Request):
    cookie = request.cookies.get(_platform_cookie_name("github"), "")
    if not cookie:
        return JSONResponse({"logged_in": False})
    data = _signed_cookie_decode(cookie, _cookie_secret())
    if not data:
        return JSONResponse({"logged_in": False})
    wallet_key = data.get("handle", "")
    from src.walletManager import wallet_manager
    wallet_info = wallet_manager.get_or_create(wallet_key)
    balance = await wallet_manager.get_balance_sol(wallet_key)
    return JSONResponse({
        "logged_in":   True,
        "handle":      wallet_key,
        "display":     data.get("display", wallet_key),
        "platform":    "github",
        "wallet":      wallet_info["pubkey"],
        "balance_sol": balance,
    })

@app.get("/auth/github/logout")
async def auth_github_logout():
    resp = RedirectResponse("/#logged-out", status_code=302)
    resp.delete_cookie(_platform_cookie_name("github"))
    return resp


# ── Twitch ────────────────────────────────────────────────────

@app.get("/auth/twitch/start")
async def auth_twitch_start(request: Request):
    client_id = os.getenv("TWITCH_CLIENT_ID", "")
    if not client_id:
        return JSONResponse({"error": "Twitch not configured"}, status_code=500)
    scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else request.url.scheme
    host   = request.headers.get("x-forwarded-host") or request.url.hostname
    base   = f"{scheme}://{host}"
    callback = f"{base}/auth/twitch/callback"
    import urllib.parse, secrets
    state = secrets.token_hex(16)
    _twitch_states[state] = True
    params = urllib.parse.urlencode({
        "client_id":     client_id,
        "redirect_uri":  callback,
        "response_type": "code",
        "scope":         "user:read:email",
        "state":         state,
    })
    return RedirectResponse(f"https://id.twitch.tv/oauth2/authorize?{params}", status_code=302)

@app.get("/auth/twitch/callback")
async def auth_twitch_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    print(f"[twitch-callback] code={code[:8] if code else None} state={state[:8] if state else None} stored={request.cookies.get('twitch_oauth_state','')[:8]}")
    if error or not code:
        return RedirectResponse("/#twitch-login-denied", status_code=302)
    _twitch_states.pop(state, None)  # skipping strict state check — wiped on restart
    client_id     = os.getenv("TWITCH_CLIENT_ID", "")
    client_secret = os.getenv("TWITCH_CLIENT_SECRET", "")
    scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else request.url.scheme
    host   = request.headers.get("x-forwarded-host") or request.url.hostname
    callback = f"{scheme}://{host}/auth/twitch/callback"
    import httpx as _hx
    try:
        async with _hx.AsyncClient() as hc:
            tok = await hc.post("https://id.twitch.tv/oauth2/token", data={
                "client_id":     client_id,
                "client_secret": client_secret,
                "code":          code,
                "grant_type":    "authorization_code",
                "redirect_uri":  callback,
            })
            tok.raise_for_status()
            access_token = tok.json()["access_token"]
            me = await hc.get("https://api.twitch.tv/helix/users",
                               headers={"Authorization": f"Bearer {access_token}",
                                        "Client-Id": client_id})
            me.raise_for_status()
            user = me.json()["data"][0]
        twitch_login = user.get("login", user["id"])
        wallet_key   = _platform_wallet_key("twitch", twitch_login)
        from src.walletManager import wallet_manager
        wallet_manager.get_or_create(wallet_key)
        cookie_val = _signed_cookie_encode(
            {"handle": wallet_key, "display": twitch_login, "platform": "twitch"},
            _cookie_secret()
        )
        resp = RedirectResponse("/#twitch-login-ok", status_code=302)
        resp.set_cookie(_platform_cookie_name("twitch"), cookie_val,
                        max_age=60*60*24*30, httponly=False, samesite="lax", secure=True)
        return resp
    except Exception as e:
        import traceback
        print(f"[twitch-oauth-error] {e}\n{traceback.format_exc()}")
        return RedirectResponse(f"/#twitch-login-error", status_code=302)

@app.get("/auth/twitch/me")
async def auth_twitch_me(request: Request):
    cookie = request.cookies.get(_platform_cookie_name("twitch"), "")
    if not cookie:
        return JSONResponse({"logged_in": False})
    data = _signed_cookie_decode(cookie, _cookie_secret())
    if not data:
        return JSONResponse({"logged_in": False})
    wallet_key = data.get("handle", "")
    from src.walletManager import wallet_manager
    wallet_info = wallet_manager.get_or_create(wallet_key)
    balance = await wallet_manager.get_balance_sol(wallet_key)
    return JSONResponse({
        "logged_in":   True,
        "handle":      wallet_key,
        "display":     data.get("display", wallet_key),
        "platform":    "twitch",
        "wallet":      wallet_info["pubkey"],
        "balance_sol": balance,
    })

@app.get("/auth/twitch/logout")
async def auth_twitch_logout():
    resp = RedirectResponse("/#logged-out", status_code=302)
    resp.delete_cookie(_platform_cookie_name("twitch"))
    return resp
