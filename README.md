<div align="center">

![EREBUS](./banner.svg)

</div>

```
███████╗██████╗ ███████╗██████╗ ██╗   ██╗███████╗
██╔════╝██╔══██╗██╔════╝██╔══██╗██║   ██║██╔════╝
█████╗  ██████╔╝█████╗  ██████╔╝██║   ██║███████╗
██╔══╝  ██╔══██╗██╔══╝  ██╔══██╗██║   ██║╚════██║
███████╗██║  ██║███████╗██████╔╝╚██████╔╝███████║
╚══════╝╚═╝  ╚═╝╚══════╝╚═════╝  ╚═════╝ ╚══════╝
                                                   
        autonomous ai agent · solana · x/twitter
```

<div align="center">

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com)
![Python](https://img.shields.io/badge/python-3.11+-black?style=flat-square)
![Claude](https://img.shields.io/badge/claude-sonnet--4-red?style=flat-square)
![Solana](https://img.shields.io/badge/solana-mainnet-purple?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-white?style=flat-square)

**EREBUS is a fully autonomous AI agent that lives on X/Twitter and Solana.**  
It thinks. It posts. It deploys tokens on command. It never sleeps.

</div>

---

## what it does

EREBUS runs 24/7 on Render. Every 20 seconds it wakes up, reads the world, decides, and acts.

- **posts original content** — oracular, cryptic, market-aware. powered by Claude AI
- **replies to mentions** — every @ is answered with full thread context and vision
- **deploys tokens on demand** — tag it on X with a name and symbol, it deploys to Solana
- **manages wallets** — every X user who connects gets a server-side Solana wallet
- **tips SOL** — users can tip each other directly from X via mentions
- **live dashboard** — terminal UI with real-time WebSocket log stream, stats, and wallet management

---

## architecture

```
┌─────────────────────────────────────────────────────────┐
│                     X / TWITTER                         │
│   mentions · quotes · timeline · replies · posts        │
└──────────────────────┬──────────────────────────────────┘
                       │  Tweepy API v2
                       ▼
┌─────────────────────────────────────────────────────────┐
│              EREBUS AGENT SERVER  (Python)              │
│                                                         │
│  server.py          — FastAPI + WebSocket dashboard     │
│  src/decision.py    — Claude AI brain (claude-sonnet)   │
│  src/xBridge.py     — Twitter read/write/reply/RT/like  │
│  src/tokenLauncher.py — parse launch intents from X     │
│  src/walletManager.py — per-handle Solana wallets       │
│  src/tipHandler.py  — SOL tip commands via X            │
│  src/memory.py      — persistent rolling memory         │
│  src/observationX.py — timeline + mentions observer     │
│  src/visionBridge.py — image/media analysis             │
│  src/threadReader.py — full thread context fetcher      │
│                                                         │
│  /data (persistent disk)                                │
│    memory/  · logs/  · dialog/  · wallets.json          │
└──────────────────────┬──────────────────────────────────┘
                       │  HTTP (AGENT_SECRET auth)
                       ▼
┌─────────────────────────────────────────────────────────┐
│            LAUNCHPAD SERVER  (Node.js)                  │
│                                                         │
│  launchpad_server.js                                    │
│    /create-from-agent  — Meteora Dynamic Bonding Curve  │
│    /pump-from-agent    — pump.fun via @pump-fun/pump-sdk │
│    /api/agent-deploys  — deployed token feed            │
│    /api/claimable-fees — creator fee claims             │
│                                                         │
│  Pinata IPFS → token metadata + image upload           │
│  Helius RPC  → Solana mainnet broadcast                 │
│  db.json     — token/trade/wallet history               │
└─────────────────────────────────────────────────────────┘
```

---

## token deployment — how it works

### meteora dynamic bonding curve

```
user tweets:
  @wwwEREBUS launch MyCoin $MYCOIN

agent detects launch intent
  → checks user wallet exists
  → checks balance ≥ 0.03 SOL
  → fetches image from tweet (if attached)
  → POST /create-from-agent on launchpad

launchpad:
  → picks vanity keypair (mint address ending in "axbt")
  → uploads image + metadata to Pinata IPFS
  → calls Meteora DynamicBondingCurveClient.createPoolWithFirstBuy()
  → signs with deployer keypair + pool creator keypair
  → broadcasts to Solana mainnet via Helius RPC
  → records to db.json

agent replies:
  @user name: MyCoin | symbol: MYCOIN | deployed. https://solscan.io/token/...
```

### pump.fun

```
user tweets:
  @wwwEREBUS pump MyCoin $MYCOIN

launchpad:
  → uploads image + metadata to pump.fun IPFS
  → loads @pump-fun/pump-sdk (CJS require — not ESM import)
  → calls OnlinePumpSdk.fetchGlobal()
  → calls PumpSdk.createAndBuyInstructions()
  → signs with deployer + mint keypairs
  → broadcasts via Helius RPC

optional: fee sharing
  @wwwEREBUS pump MyCoin $MYCOIN share fees to @friend
  → sets 50/50 fee split between deployer and @friend
```

### supported launch formats

```
@wwwEREBUS launch MyCoin $MYCOIN
@wwwEREBUS deploy MyCoin MYCOIN
@wwwEREBUS launch name: MyCoin symbol: MYCOIN
@wwwEREBUS create token called MyCoin ticker MYCOIN
@wwwEREBUS mint MYCOIN
@wwwEREBUS pump MyCoin $MYCOIN
@wwwEREBUS launch MyCoin $MYCOIN share fees to @friend
@wwwEREBUS launch MyCoin $MYCOIN fee wallet: <solana_pubkey>
```

attach an image to the tweet → it becomes the token logo automatically.

---

## agent cycle

every 20 seconds:

```
1. MENTIONS     — fetch new @mentions + search mentions (since_id cursor)
                  deduplicate with cross-process claim locks
                  
2. LAUNCH CHECK — detect launch/pump intent → deploy flow (see above)
                  detect wallet check / tip intent → handle directly
                  detect social commands (like/retweet/unlike/unretweet)
                  
3. LLM DECIDE   — pass mention + thread context + vision + memory to Claude
                  Claude returns: action, content, target_tweet_id
                  
4. QUOTES       — check quote-tweets of recent EREBUS posts (every 2 cycles)

5. OBSERVE      — read home timeline (reverse chronological)
                  filter out own posts, deduplicate feed
                  
6. POST         — original post every 2–5 min (random gap, natural rhythm)
                  3-attempt retry with similarity + banned-opener checks
                  topic entity cooldown (30 min per entity)
                  
7. DORMANT      — sleep interval_time seconds, repeat
```

---

## dashboard

the live terminal is served at `/` (X OAuth required to enter).

```
┌─────────────────────────────────────────────────────┐
│  EREBUS  @wwwEREBUS  terminal beneath the veil      │
├───────────────┬─────────────────────────────────────┤
│  PRESENCE     │  live log stream (WebSocket)        │
│  memory       │                                     │
│  learning     │  [SYSTEM]  cycle 42 — checking...  │
│  attention    │  [LAUNCH]  🚀 MyCoin deployed       │
│               │  [TRANSMIT] post — signal persists  │
│  SESSION      │  [NEURAL]  spikes=2.4hz entropy=... │
│  handle       │                                     │
│  wallet       ├─────────────────────────────────────┤
│  balance      │  speak  interrupt the silence       │
└───────────────┴─────────────────────────────────────┘
```

the "speak" terminal sends real messages to Claude (as EREBUS) and replies in character. responses are powered by the live `ANTHROPIC_API_KEY` — not hardcoded.

---

## wallet system

every X user who visits the dashboard and connects their X account gets:

- a **server-side Solana keypair** generated and stored in `/data/wallets.json`
- a **pubkey** they can fund to enable token deploys (min 0.03 SOL)
- the ability to **export their private key** via the dashboard (phrase-confirmed, rate-limited)
- the ability to **tip SOL** to other users via X mentions
- **creator fee claiming** for tokens they deployed

private keys are stored on the Render persistent disk (`/data`). only the key owner (verified via X OAuth cookie) can export their key.

---

## setup

### 1. clone

```bash
git clone https://github.com/yourhandle/erebus
cd erebus
```

### 2. environment variables

copy `.env.example` to `.env` and fill in:

```env
# Claude AI — required
ANTHROPIC_API_KEY=sk-ant-...

# Twitter / X account
TWITTER_user_name=yourhandle
TWITTER_email=your@email.com
TWITTER_pwd=yourpassword

# Twitter API (all 5 keys required)
TWITTER_API_CONSUMER_KEY=...
TWITTER_API_CONSUMER_SECRET=...
TWITTER_API_BEARER_TOKEN=...
TWITTER_API_ACCESS_TOKEN=...
TWITTER_API_ACCESS_TOKEN_SECRET=...

# Launchpad server URL (your deployed Node.js service)
LAUNCHPAD_URL=https://your-launchpad.onrender.com

# Shared secret between agent and launchpad (any random string)
AGENT_SECRET=your_random_secret_here

# Solana
RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
EREBUS_WALLET=your_solana_pubkey

# Owner handle — your X handle (no @). enables self-deploy with agent wallet
OWNER_HANDLE=yourhandle

# Agent's own Solana keypair (JSON array of 64 bytes) — for self-deploys
AGENT_PRIVATE_KEY=[1,2,3,...,64]

# Storage
DATA_DIR=/data
PORT=10000
```

### 3. deploy to render

push to GitHub, then in Render:

1. **New Web Service** → connect your repo
2. **Runtime**: Python
3. **Build Command**: `bash build.sh`
4. **Start Command**: `python server.py`
5. Add a **Disk**: mount path `/data`, size 1 GB
6. Set all env vars from the list above

or use the included `render.yaml` for one-click deploy.

### 4. upload vanity keypairs (optional but recommended)

vanity keypairs give tokens mint addresses ending in a custom suffix. generate them with [vanity-keypair](https://github.com/mcintyre94/solana-vanity) and upload via:

```bash
node uploader.js
```

place the `.json` keypair files in `/data/vanity/` on the Render disk.

---

## self-deploy (owner mode)

when `OWNER_HANDLE` matches the tweet author, EREBUS uses its own wallet (`AGENT_PRIVATE_KEY`) to deploy — no user wallet required:

```
@wwwEREBUS launch EREBUS $EREBUS
```

the token is recorded as deployed by the agent itself.

---

## local development

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# fill in .env
python server.py
```

dashboard available at `http://localhost:10000`

---

## project structure

```
erebus/
├── server.py              # FastAPI server + agent loop + WebSocket
├── config.json            # model, timing, paths
├── requirements.txt
├── build.sh               # Render build script
├── render.yaml            # Render one-click deploy config
├── terminal.html          # dashboard UI (root, served at /)
├── uploader.js            # vanity keypair upload utility
├── public/
│   ├── terminal.html      # dashboard UI (public/)
│   ├── gate.html          # X OAuth landing page
│   ├── agent-deploys.html # deployed tokens feed
│   └── manifest.json
├── src/
│   ├── config.py          # env + path config
│   ├── decision.py        # Claude AI decision engine
│   ├── xBridge.py         # Twitter API v2 (tweepy)
│   ├── actionX.py         # execute tweet/reply/RT/like
│   ├── observationX.py    # timeline observation
│   ├── tokenLauncher.py   # launch intent parser + deploy caller
│   ├── walletManager.py   # per-handle Solana wallet management
│   ├── tipHandler.py      # SOL tip detection + execution
│   ├── memory.py          # rolling persistent memory
│   ├── dialogManager.py   # conversation history
│   ├── threadReader.py    # full thread context fetcher
│   ├── visionBridge.py    # tweet image analysis
│   ├── claude_ai.py       # Anthropic SDK wrapper
│   ├── chain_context.py   # live Solana chain context injector
│   └── logs.py            # rich logging
└── data/
    ├── prompt.json        # agent personality / system prompt
    └── doom_prompt.json   # alternate prompt config
```

---

## api endpoints

| method | path | description |
|--------|------|-------------|
| GET | `/` | dashboard (X auth required) |
| GET | `/health` | `{status, agent, phase}` |
| GET | `/api/stats` | rounds, actions, decisions, errors |
| GET | `/api/logs?n=100` | recent log entries |
| GET | `/api/memory` | agent memory entries |
| GET | `/api/transmissions` | recent posts with tweet links |
| GET | `/api/profile` | follower/following counts (cached 10min) |
| GET | `/api/heatmap` | post activity by hour |
| GET | `/api/agent-deploys` | proxy to launchpad deploy feed |
| GET | `/api/wallet/info` | authenticated user's wallet + balance |
| POST | `/api/wallet/export-key` | export private key (phrase-confirmed) |
| POST | `/api/wallet/claim-fees` | claim creator fees from pool |
| GET | `/api/claimable-fees?wallet=` | claimable fee amounts |
| GET | `/api/x-leaderboard` | top deployers by points |
| GET | `/auth/x/start` | begin X OAuth flow |
| GET | `/auth/x/callback` | X OAuth callback |
| GET | `/auth/x/me` | current session info |
| GET | `/auth/x/logout` | clear session |
| WS | `/ws` | live log stream |

---

## rate limits & safety

- **per-handle reply limit**: max 20 replies to same handle per 24h rolling window
- **like limit**: max 10 likes/hour
- **retweet limit**: max 5 RTs/hour
- **post gap**: 2–5 min random gap between original posts
- **duplicate prevention**: cross-process claim lock files in `/data`
- **tip limits**: max 0.1 SOL per tip, max 3 tips/hour per sender, min 0.005 SOL reserve always kept
- **export throttle**: private key export is rate-limited and logged per handle

---

## license

MIT — do whatever you want. build something real.

---

<div align="center">

*the signal does not ask permission*

**[@wwwEREBUS](https://x.com/wwwEREBUS)**

</div>
