require('dotenv').config();
const express = require('express');
const http = require('http');
const multer = require('multer');
const fs = require('fs');
const path = require('path');
const { PinataSDK } = require('pinata');
const low = require('lowdb');
const FileSync = require('lowdb/adapters/FileSync');
const { Connection, Keypair, PublicKey, SystemProgram, Transaction, LAMPORTS_PER_SOL, VersionedTransaction } = require('@solana/web3.js');
const { DynamicBondingCurveClient } = require('@meteora-ag/dynamic-bonding-curve-sdk');
const bs58 = require('bs58');
const BN = require('bn.js');
const { WebSocketServer } = require('ws');
const chatRooms = new Map();
const clientRooms = new Map();
const jupiterCache = new Map();
const CACHE_DURATION_MS = 2 * 60 * 1000;
const crypto = require('crypto');


// --- Basic Setup ---
const app = express();
const server = http.createServer(app);
const upload = multer({ dest: process.env.RENDER ? '/tmp/uploads/' : 'uploads/' });
const pinata = new PinataSDK({ pinataJwt: process.env.PINATA_JWT });

// --- Database Setup ---
const dbPath = process.env.RENDER ? '/data/db.json' : 'db.json';
const adapter = new FileSync(dbPath);
const db = low(adapter);
db.defaults({
    tokens: [],
    trades: [],
    wallets: [],
    quests: [],
    comments: []
}).write();

// ONE-TIME MIGRATION: copy points -> eggPoints for all existing wallets on startup
(function migratePointsToEggPoints(){
    var wallets=db.get("wallets").value();
    var migrated=0;
    wallets.forEach(function(w){
        if((w.eggPoints===undefined||w.eggPoints===0)&&(w.points||0)>0){
            db.get("wallets").find({address:w.address}).assign({eggPoints:w.points}).write();
            migrated++;
        }
    });
    if(migrated>0) console.log("Migrated eggPoints for "+migrated+" existing wallets");
})();

// ── $EGG Reward constants (defined early so addEggPoints is available everywhere) ──
const EGG_PER_TRADE      = 10;
const EGG_SNIPER_BONUS   = 40;
const EGG_PER_LAUNCH     = 100;
const EGG_PER_COMMENT    = 2;
const EGG_TO_SOL_RATE    = 0.0001;  // 1 EGG = 0.0001 SOL → 1000 EGG = 0.1 SOL
const EGG_MIN_CLAIM      = 1000;
const EGG_CLAIM_COOLDOWN = 10 * 60 * 1000; // 10 min in ms

// Helper: atomically add egg points to a wallet profile (creates profile if missing)
function addEggPoints(address, amount) {
    if (!address || amount <= 0) return;
    let profile = db.get("wallets").find({ address });
    if (!profile.value()) {
        db.get("wallets").push({
            address, points: 0, eggPoints: 0, totalVolumeSol: 0,
            completedQuests: [], profitableFlips: 0, deployedCount: 0,
            snipeCount: 0, profitableFlipStreak: 0, lastEggClaim: 0,
            lastHolderClaim: 0, holderPoolSnapshotAtClaim: 0
        }).write();
        profile = db.get("wallets").find({ address });
    }
    const currentEgg = profile.value().eggPoints || 0;
    const currentPts = profile.value().points || 0;
    // Write both fields: eggPoints = claimable SOL balance, points = leaderboard score
    profile.assign({
        eggPoints: currentEgg + amount,
        points:    currentPts + amount
    }).write();
    console.log('+' + amount + ' $EGG -> ' + address.slice(0,6) + ' (claimable: ' + (currentEgg + amount) + ')');
}

// --- Load Master Quest List ---
let masterQuests = [];

const ENABLE_QUEST_LOADING = false;

if (ENABLE_QUEST_LOADING) {
    try {
        const questsPath = process.env.RENDER ? '/data/quests5.json' : 'quests5.json';
        masterQuests = JSON.parse(fs.readFileSync(questsPath, 'utf-8'));

        if (db.get('quests').isEmpty().value()) {
            db.set('quests', masterQuests).write();
        }

        console.log(`✅ Successfully loaded ${masterQuests.length} quests from ${questsPath}.`);
    } catch (error) {
        console.error("❌ CRITICAL ERROR: Could not load 'quests5.json'. Make sure it's uploaded to the '/data' directory on Render.");
        process.exit(1);
    }
} else {
    console.log("⚠️ Quest loading is **temporarily disabled** for maintenance.");
    
    masterQuests = [
        { id: 'FIRST_STEPS', title: 'First Steps', points: 10, description: 'Make your first trade on the platform' },
        { id: 'APPRENTICE_TRADER', title: 'Apprentice Trader', points: 25, description: 'Trade 10 SOL in total volume' },
        { id: 'JOURNEYMAN_TRADER', title: 'Journeyman Trader', points: 50, description: 'Trade 100 SOL in total volume' },
        { id: 'MARKET_MAKER', title: 'Market Maker', points: 100, description: 'Trade 1,000 SOL in total volume' },
        { id: 'KINGPIN_TRADER', title: 'Kingpin Trader', points: 200, description: 'Trade 5,000 SOL in total volume' },
        { id: 'TYCOON', title: 'Tycoon', points: 500, description: 'Trade 15,000 SOL in total volume' },
        { id: 'FIRST_LAUNCH', title: 'The Creator', points: 100, description: 'Launch your first token' },
        { id: 'SERIAL_LAUNCHER', title: 'Serial Launcher', points: 250, description: 'Launch 5 or more tokens' },
        { id: 'WHALE_TRADE', title: 'Whale Trade', points: 75, description: 'Make a single trade of 25+ SOL' },
        { id: 'PIONEER_TRADER', title: 'Pioneer Trader', points: 50, description: 'Be among the first 10 buyers of a token' },
        { id: 'THE_REGULAR', title: 'The Regular', points: 30, description: 'Trade 5 different tokens' },
        { id: 'DIVERSIFIER', title: 'Diversifier', points: 100, description: 'Trade 25 different tokens' },
        { id: 'SNIPER', title: 'Sniper', points: 40, description: 'Buy a token within 30 seconds of launch' },
        { id: 'ALPHA_SNIPER', title: 'Alpha Sniper', points: 150, description: 'Snipe 5 tokens within 30 seconds of launch' },
        { id: 'SOCIALITE', title: 'Socialite', points: 15, description: 'Post your first comment' },
        { id: 'COMMUNITY_PILLAR', title: 'Community Pillar', points: 75, description: 'Post 25 comments' },
        { id: 'TOP_TEN_TRADER', title: 'Top Ten Trader', points: 300, description: 'Reach the top 10 on the leaderboard' },
    ];
    
    console.log(`✅ Loaded ${masterQuests.length} fallback quests for basic functionality.`);
}

// --- Solana Setup ---
const walletSecretBase58 = process.env.WALLET_SECRET;
if (!walletSecretBase58) throw new Error("WALLET_SECRET not found in .env file.");
const wallet = Keypair.fromSecretKey(bs58.decode(walletSecretBase58));
const connection = new Connection('https://mainnet.helius-rpc.com/?api-key=e7fc71a3-e276-43cb-865b-91c2684efee8', 'confirmed');
const client = new DynamicBondingCurveClient(connection, 'confirmed');
const PROGRAM_ID = new PublicKey('dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN');
const PLATFORM_FEE = 0.001 * LAMPORTS_PER_SOL;

const ZEC_MINT = 'A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS';
let cachedZecPrice = null;
let lastZecFetch = 0;
const ZEC_CACHE_DURATION = 10000;

async function getZecPrice() {
    const now = Date.now();
    if (cachedZecPrice && (now - lastZecFetch < ZEC_CACHE_DURATION)) {
        return cachedZecPrice;
    }
    
    try {
        const response = await fetch(`https://datapi.jup.ag/v2/search?query=${ZEC_MINT}&tokenExactCaseInsensitive=false`);
        if (!response.ok) throw new Error('Failed to fetch ZEC price');
        
        const data = await response.json();
        const zecToken = data.find(t => t.token?.id === ZEC_MINT);
        
        if (zecToken && zecToken.token?.usdPrice) {
            cachedZecPrice = zecToken.token.usdPrice;
            lastZecFetch = now;
            console.log(`💰 ZEC Price Updated: $${cachedZecPrice}`);
            return cachedZecPrice;
        }
        
        throw new Error('ZEC price not found in response');
    } catch (error) {
        console.error('Error fetching ZEC price:', error);
        return cachedZecPrice || 577;
    }
}

function convertUsdcToZec(usdcAmount, zecPrice) {
    if (!zecPrice || zecPrice <= 0) return 0;
    const adjustedUsdc = Math.max(0, usdcAmount - 3);
    return adjustedUsdc / zecPrice;
}

// --- Image Cache Setup ---
const cacheDir = path.join(__dirname, 'image_cache');
if (!fs.existsSync(cacheDir)) {
    fs.mkdirSync(cacheDir, { recursive: true });
    console.log("Image cache directory created.");
}

// --- Constants ---
const quoteMints = {
    ZEC: new PublicKey('A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS'),
    USD1: new PublicKey('USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB'),
    SOL: new PublicKey('So11111111111111111111111111111111111111112'),
    MET: new PublicKey('METvsvVRapdj9cFLzq4Tr43xK4tAjQfwX76z3n6mWQL'),
};

// --- DYNAMICALLY LOAD YOUR CUSTOM CONFIGS ---
let configs = {};
try {
    const configsRaw = fs.readFileSync(path.join(__dirname, 'configs.json'), 'utf-8');
    const configsJson = JSON.parse(configsRaw);
    for (const key in configsJson) {
        if (configsJson.hasOwnProperty(key)) {
            configs[key] = new PublicKey(configsJson[key]);
        }
    }
    console.log("✅ Successfully loaded your custom launchpad blueprints from configs.json");
    console.log(configs);
} catch (error) {
    console.error("❌ CRITICAL ERROR: Could not load 'configs.json'.");
    console.error("Please make sure the file is uploaded in the same directory as server.js");
    process.exit(1);
}

// --- Middleware & Routes ---
app.use(express.static('public'));
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

app.post('/upload-file', upload.single('file'), (req, res) => {
  try {
    // Auth check — same AGENT_SECRET used by tokenLauncher
    const agentSecret = process.env.AGENT_SECRET || '';
    const providedSecret = req.headers['x-agent-secret'] || req.body.secret || '';
    if (agentSecret && providedSecret !== agentSecret) {
      return res.status(401).json({ error: 'Unauthorized' });
    }
    if (!req.file) return res.status(400).json({ error: 'No file' });
    const targetDir = req.body.target || '/data';
    const destPath = path.join(targetDir, req.file.originalname);
    if (!fs.existsSync(path.dirname(destPath))) fs.mkdirSync(path.dirname(destPath), { recursive: true });
    fs.copyFileSync(req.file.path, destPath);
    fs.unlinkSync(req.file.path);
    console.log(`[upload-file] saved: ${destPath}`);
    res.json({ success: true, path: destPath });
  } catch (err) {
    console.error('Upload error:', err);
    res.status(500).json({ error: err.message });
  }
});

app.post('/create', upload.single('image'), async (req, res) => {
    try {
        const { name, symbol, description, website, twitter, quote, deployer, initialBuyAmount } = req.body;
        
        const ADMIN_WALLET = 'DtDrwr7qXqWoXikXndmENBZBjjagMjEt1wnZBTVPPser';
        const isAdmin = deployer === ADMIN_WALLET;
        
        if (!isAdmin) {
            const FORBIDDEN_WORDS = ['jeffy', 'jeff yu', 'jeffyu', 'jeffy yu'];
            const lowerName = name.toLowerCase();
            const lowerSymbol = symbol.toLowerCase();

            const isForbidden = FORBIDDEN_WORDS.some(word => 
                lowerName.includes(word) || lowerSymbol.includes(word)
            );

            if (isForbidden) {
                return res.status(403).json({ 
                    error: 'Token name or symbol contains forbidden keywords. Please choose another.' 
                });
            }
        }

        if (!isAdmin) {
            const lowerName = name.toLowerCase();
            const lowerSymbol = symbol.toLowerCase();
            
            const existingToken = db.get('tokens')
                .find(t => 
                    t.name.toLowerCase() === lowerName || 
                    t.symbol.toLowerCase() === lowerSymbol
                )
                .value();

            if (existingToken) {
                return res.status(403).json({ 
                    error: `A token with this name or symbol already exists. Please choose a unique name and ticker.`,
                    existingToken: {
                        name: existingToken.name,
                        symbol: existingToken.symbol,
                        baseMint: existingToken.baseMint
                    }
                });
            }
        }

        if (!name || !symbol || !quote || !deployer) {
            return res.status(400).json({ error: 'Missing required fields: name, symbol, quote, or deployer.' });
        }
        const vanityDir = path.join(process.env.RENDER ? '/data' : __dirname, 'vanity');
        const keypairFiles = fs.readdirSync(vanityDir).filter(f => f.endsWith('.json'));
        if (keypairFiles.length === 0) { return res.status(500).json({ error: "No available vanity keypairs left!" }); }
        const keypairFile = keypairFiles[0];
        const keypairPath = path.join(vanityDir, keypairFile);
        const secretKey = JSON.parse(fs.readFileSync(keypairPath, 'utf-8'));
        const mintKeypair = Keypair.fromSecretKey(new Uint8Array(secretKey));
        const baseMint = mintKeypair.publicKey;
        let imageUrl = 'https://arweave.net/WCM5h_34E8m3y_k-h1i59Q_P5I54k-H2d_s4b-C3xZM';
        if (req.file) {
            const fileBuffer = fs.readFileSync(req.file.path);
            const imageBlob = new Blob([fileBuffer]);
            const options = { pinataMetadata: { name: req.file.originalname } };
            const imageUploadResult = await pinata.upload.public.file(imageBlob, options);
            imageUrl = `https://lom.mypinata.cloud/ipfs/${imageUploadResult.cid}`;
            fs.unlinkSync(req.file.path);
        }
        const metadata = { name, symbol, description, image: imageUrl, website, twitter, createdOn: "by erebus" };
        const jsonUploadResult = await pinata.upload.public.json(metadata, { pinataMetadata: { name: `${symbol}-metadata.json` } });
        const uri = `https://lom.mypinata.cloud/ipfs/${jsonUploadResult.cid}`;
        const deployerPubkey = new PublicKey(deployer);
        const configPubkey = configs[quote];
        const createPoolParam = {
            baseMint: baseMint,
            config: configPubkey,
            name: name,
            symbol: symbol,
            uri: uri,
            payer: deployerPubkey,
            poolCreator: deployerPubkey,
        };
        let firstBuyParam = undefined;
        const buyAmount = parseFloat(initialBuyAmount);
        if (buyAmount && buyAmount > 0) {
            const quoteDecimals = quote === 'SOL' ? 9 : 6;
            const buyAmountInSmallestUnit = new BN(buyAmount * Math.pow(10, quoteDecimals));
            firstBuyParam = {
                buyer: deployerPubkey,
                buyAmount: buyAmountInSmallestUnit,
                minimumAmountOut: new BN(0),
                referralTokenAccount: null,
            };
        }
        const { createPoolTx, swapBuyTx } = await client.pool.createPoolWithFirstBuy({
            createPoolParam,
            firstBuyParam
        });
        const transaction = new Transaction();
        transaction.add(SystemProgram.transfer({ fromPubkey: deployerPubkey, toPubkey: wallet.publicKey, lamports: PLATFORM_FEE }));
        transaction.add(...createPoolTx.instructions);
        if (swapBuyTx) {
            transaction.add(...swapBuyTx.instructions);
        }
        transaction.feePayer = deployerPubkey;
        transaction.recentBlockhash = (await connection.getLatestBlockhash('confirmed')).blockhash;
        const serializedTransaction = transaction.serialize({ requireAllSignatures: false });
        const base64Transaction = serializedTransaction.toString('base64');
        res.status(200).json({ transaction: base64Transaction, baseMint: baseMint.toString(), keypairFile, uri, imageUrl });
    } catch (err) {
        console.error("Error in /create endpoint:", err);
        res.status(500).json({ error: err.message });
    }
});

// =====================================================================
// 🤖 /create-from-agent — called by @wwwEREBUS Python agent when a user
//    tweets "launch name: X symbol: Y" with an optional image attached.
//    The agent signs nothing — the deployer wallet (WALLET_SECRET) pays
//    and signs the entire transaction server-side.
//
//    Body (multipart/form-data):
//      name              — token name
//      symbol            — token ticker
//      description       — optional description
//      twitter_url       — full URL of the requester's tweet (used as token twitter link)
//      deployer_handle   — X handle of requesting user (for points + attribution)
//      secret            — must match AGENT_SECRET env var
//      image             — optional image file (multipart)
//
//    Always does a 0.01 SOL initial buy using the platform WALLET_SECRET.
// =====================================================================
app.post('/create-from-agent', upload.single('image'), async (req, res) => {
    try {
        // ── Auth check ──────────────────────────────────────────────
        const agentSecret = process.env.AGENT_SECRET || '';
        if (agentSecret && req.body.secret !== agentSecret) {
            return res.status(401).json({ error: 'Unauthorized' });
        }

        const { name, symbol, description, twitter_url, deployer_handle } = req.body;

        if (!name || !symbol) {
            return res.status(400).json({ error: 'Missing required fields: name, symbol' });
        }

        // ── Forbidden word check ─────────────────────────────────────
        const FORBIDDEN_WORDS = ['jeffy', 'jeff yu', 'jeffyu', 'jeffy yu'];
        const lowerName   = name.toLowerCase();
        const lowerSymbol = symbol.toLowerCase();
        if (FORBIDDEN_WORDS.some(w => lowerName.includes(w) || lowerSymbol.includes(w))) {
            return res.status(403).json({ error: 'Token name or symbol contains forbidden keywords.' });
        }

        // ── Duplicate check ──────────────────────────────────────────
        const existing = db.get('tokens')
            .find(t => t.name.toLowerCase() === lowerName || t.symbol.toLowerCase() === lowerSymbol)
            .value();
        if (existing) {
            return res.status(403).json({
                error: `Token "${existing.name}" (${existing.symbol}) already exists.`,
                existingMint: existing.baseMint
            });
        }

        // ── Pick vanity keypair ──────────────────────────────────────
        const vanityDir = path.join(process.env.RENDER ? '/data' : __dirname, 'vanity');
        const keypairFiles = fs.readdirSync(vanityDir).filter(f => f.endsWith('.json'));
        if (keypairFiles.length === 0) {
            return res.status(500).json({ error: 'No vanity keypairs available.' });
        }
        const keypairFile = keypairFiles[0];
        const keypairPath = path.join(vanityDir, keypairFile);
        const secretKey   = JSON.parse(fs.readFileSync(keypairPath, 'utf-8'));
        const mintKeypair = Keypair.fromSecretKey(new Uint8Array(secretKey));
        const baseMint    = mintKeypair.publicKey;

        // ── Upload image to Pinata (if provided) ─────────────────────
        let imageUrl = 'https://arweave.net/WCM5h_34E8m3y_k-h1i59Q_P5I54k-H2d_s4b-C3xZM';
        if (req.file) {
            const fileBuffer  = fs.readFileSync(req.file.path);
            const imageBlob   = new Blob([fileBuffer]);
            const imgResult   = await pinata.upload.public.file(imageBlob, {
                pinataMetadata: { name: req.file.originalname }
            });
            imageUrl = `https://lom.mypinata.cloud/ipfs/${imgResult.cid}`;
            fs.unlinkSync(req.file.path);
        }

        // ── Build metadata — twitter field = deployer's original tweet URL ──
        // This links the token page back to the tweet that triggered the launch.
        const tweetLink = twitter_url || `https://x.com/${deployer_handle || 'erebus'}`;
        const metadata  = {
            name,
            symbol,
            description: description || `launched via @wwwEREBUS by @${deployer_handle || 'anon'}`,
            image:       imageUrl,
            website:     tweetLink,   // website = the original X post
            twitter:     tweetLink,   // twitter = same link for token explorers
            createdOn:   'by erebus'
        };
        const jsonResult = await pinata.upload.public.json(metadata, {
            pinataMetadata: { name: `${symbol}-metadata.json` }
        });
        const uri = `https://lom.mypinata.cloud/ipfs/${jsonResult.cid}`;

        // ── Build pool params ─────────────────────────────────────────
        const configPubkey = configs['SOL'];
        if (!configPubkey) {
            return res.status(500).json({ error: 'configs.json missing SOL key — add your Meteora partner config pubkey' });
        }
        const createPoolParam = {
            baseMint:    baseMint,
            config:      configPubkey,
            name,
            symbol,
            uri,
            payer:       wallet.publicKey,
            poolCreator: wallet.publicKey,
        };

        // ── Always do 0.01 SOL initial buy (platform wallet buys) ────
        const INITIAL_BUY_SOL    = 0.01;
        const INITIAL_BUY_LAMPORTS = new BN(INITIAL_BUY_SOL * LAMPORTS_PER_SOL); // 10_000_000 lamports
        const firstBuyParam = {
            buyer:               wallet.publicKey,
            buyAmount:           INITIAL_BUY_LAMPORTS,
            minimumAmountOut:    new BN(0),
            referralTokenAccount: null,
        };

        let poolResult;
        try {
            poolResult = await client.pool.createPoolWithFirstBuy({
                createPoolParam,
                firstBuyParam
            });
        } catch (sdkErr) {
            console.error('[create-from-agent] SDK createPoolWithFirstBuy threw:', sdkErr?.message || sdkErr);
            return res.status(500).json({ error: `SDK error: ${sdkErr?.message || sdkErr}` });
        }

        console.log('[create-from-agent] poolResult keys:', poolResult ? Object.keys(poolResult) : 'undefined/null');

        // SDK may return { createPoolTx, swapBuyTx } OR the Transaction directly
        let createPoolTx, swapBuyTx;
        if (poolResult && poolResult.createPoolTx) {
            // Newer SDK shape: { createPoolTx, swapBuyTx }
            console.log('[create-from-agent] SDK returned wrapped {createPoolTx, swapBuyTx}');
            createPoolTx = poolResult.createPoolTx;
            swapBuyTx    = poolResult.swapBuyTx;
        } else if (poolResult && poolResult.instructions) {
            // Legacy SDK shape: returns Transaction directly (all instructions combined incl. first buy)
            console.log('[create-from-agent] SDK returned Transaction directly');
            createPoolTx = poolResult;
            swapBuyTx    = undefined;
        } else {
            console.error('[create-from-agent] Unexpected poolResult:', JSON.stringify(Object.keys(poolResult || {})));
            return res.status(500).json({ error: 'SDK returned unexpected result shape' });
        }
        console.log('[create-from-agent] createPoolTx instruction count:', createPoolTx?.instructions?.length);

        // ── Detect if SDK returned VersionedTransaction or legacy Transaction ──
        const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash('confirmed');

        let signature;

        const isVersioned = (tx) => tx && typeof tx.version !== 'undefined';

        if (isVersioned(createPoolTx)) {
            // SDK v1.4.4+ returns VersionedTransactions — sign and send separately
            console.log('[create-from-agent] Using VersionedTransaction path');
            createPoolTx.message.recentBlockhash = blockhash;
            createPoolTx.sign([wallet, mintKeypair]);
            signature = await connection.sendRawTransaction(createPoolTx.serialize(), {
                skipPreflight: false, preflightCommitment: 'confirmed', maxRetries: 5
            });
            await connection.confirmTransaction({ signature, blockhash, lastValidBlockHeight }, 'confirmed');

            if (swapBuyTx) {
                const { blockhash: bh2, lastValidBlockHeight: lv2 } = await connection.getLatestBlockhash('confirmed');
                swapBuyTx.message.recentBlockhash = bh2;
                swapBuyTx.sign([wallet]);
                const sig2 = await connection.sendRawTransaction(swapBuyTx.serialize(), {
                    skipPreflight: false, preflightCommitment: 'confirmed', maxRetries: 5
                });
                await connection.confirmTransaction({ signature: sig2, blockhash: bh2, lastValidBlockHeight: lv2 }, 'confirmed');
                console.log('[create-from-agent] swapBuyTx confirmed:', sig2);
            }
        } else {
            // Legacy Transaction path
            console.log('[create-from-agent] Using legacy Transaction path');
            const transaction = new Transaction();
            transaction.add(...createPoolTx.instructions);
            if (swapBuyTx && swapBuyTx.instructions) {
                transaction.add(...swapBuyTx.instructions);
            }
            transaction.feePayer        = wallet.publicKey;
            transaction.recentBlockhash = blockhash;
            // Sign with both — mintKeypair signs as baseMint, wallet signs as payer/poolCreator
            transaction.sign(wallet, mintKeypair);
            signature = await connection.sendRawTransaction(transaction.serialize(), {
                skipPreflight: false, preflightCommitment: 'confirmed', maxRetries: 5
            });
            await connection.confirmTransaction(signature, 'confirmed');
        }

        console.log(`[create-from-agent] tx confirmed: ${signature.slice(0,12)}...`);

        // ── Confirm pool address from on-chain tx ─────────────────────
        let onchainTx = null;
        let attempts  = 0;
        while (!onchainTx && attempts < 5) {
            try { onchainTx = await connection.getTransaction(signature, { maxSupportedTransactionVersion: 0 }); } catch(e) {}
            if (!onchainTx) { attempts++; await new Promise(r => setTimeout(r, 2000)); }
        }

        let poolAddress = null;
        if (onchainTx) {
            const accountKeys = onchainTx.transaction.message.accountKeys.map(k => k.toString());
            const initIx = onchainTx.transaction.message.instructions.find(ix =>
                accountKeys[ix.programIdIndex] === 'dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN' && ix.accounts.length > 10
            );
            if (initIx) poolAddress = accountKeys[initIx.accounts[5]];
        }

        // ── Retire vanity keypair ─────────────────────────────────────
        const usedDir = path.join(vanityDir, 'used');
        if (!fs.existsSync(usedDir)) fs.mkdirSync(usedDir, { recursive: true });
        if (fs.existsSync(keypairPath)) fs.renameSync(keypairPath, path.join(usedDir, keypairFile));

        // ── Save to DB ────────────────────────────────────────────────
        const deployerHandle = deployer_handle || 'anon';
        db.get('tokens').push({
            baseMint:          baseMint.toString(),
            quote:             'SOL',
            deployer:          wallet.publicKey.toString(),
            deployer_x_handle: deployerHandle,
            name,
            symbol,
            description:       metadata.description,
            pool:              poolAddress || '',
            uri,
            website:           tweetLink,
            twitter:           tweetLink,
            imageUrl,
            createdAt:         new Date().toISOString(),
            migrated:          false,
            launchedViaX:      true,
            initialBuySol:     INITIAL_BUY_SOL,
        }).write();

        // Award EGG points (platform wallet gets the on-chain credit,
        // x_points.json tracks the X handle — managed by Python agent).
        addEggPoints(wallet.publicKey.toString(), EGG_PER_LAUNCH);

        const mintStr = baseMint.toString();
        console.log(`🚀 [agent-launch] ${name} (${symbol}) by @${deployerHandle} | mint=${mintStr.slice(0,8)}... | pool=${(poolAddress||'?').slice(0,8)}...`);

        res.status(200).json({
            success:        true,
            baseMint:       mintStr,
            signature,
            solscan:        `https://solscan.io/token/${mintStr}`,
            pool:           poolAddress || '',
            imageUrl,
            uri,
            twitterUrl:     tweetLink,
            initialBuySol:  INITIAL_BUY_SOL,
        });

    } catch (err) {
        console.error('[create-from-agent] error:', err);
        res.status(500).json({ error: err.message });
    }
});

// =====================================================================
// 🟢 /pump-from-agent — pump.fun token launch triggered by X mention.
//    Trigger words: "pump", "pumpfun", "pump.fun"
//    Examples:
//      @wwwEREBUS pump PEPE $PEPE
//      @wwwEREBUS pump deploy: PEPE, symbol: PEPE
//      @wwwEREBUS pumpfun launch DOGE DOGE share fees to @user2
//
//    Flow:
//      1. Upload image → Pinata IPFS
//      2. Upload metadata JSON → Pinata IPFS
//      3. POST /coins/sign-create-tx → pump.fun API returns unsigned tx bytes
//      4. Deserialize → sign with deployer keypair + mint keypair
//      5. POST /send-transaction → pump.fun broadcasts the signed tx
//      6. Optional: 0.01 SOL initial buy via pump.fun buy API
//      7. Save to DB with platform: "pumpfun"
//
//    Auth: requires AGENT_SECRET header matching env var
//    Env required: PUMP_FUN_AUTH_TOKEN (from pump.fun account cookie auth_token)
//                  WALLET_SECRET (same deployer wallet)
// =====================================================================
app.post('/pump-from-agent', upload.single('image'), async (req, res) => {
    try {
        // ── Auth ─────────────────────────────────────────────────────
        const agentSecret = process.env.AGENT_SECRET || '';
        if (agentSecret && req.body.secret !== agentSecret) {
            return res.status(401).json({ error: 'Unauthorized' });
        }

        let { name, symbol, description, twitter_url, deployer_handle, deployer_secret } = req.body;

        if (!name || !symbol) {
            return res.status(400).json({ error: 'Missing required fields: name, symbol' });
        }

        // ── Forbidden word check ────────────────────────────────────
        const FORBIDDEN_WORDS = ['jeffy', 'jeff yu', 'jeffyu', 'jeffy yu'];
        const lowerName   = name.toLowerCase();
        const lowerSymbol = symbol.toLowerCase();
        if (FORBIDDEN_WORDS.some(w => lowerName.includes(w) || lowerSymbol.includes(w))) {
            return res.status(403).json({ error: 'Token name or symbol contains forbidden keywords.' });
        }

        // ── Duplicate check ─────────────────────────────────────────
        const existing = db.get('tokens')
            .find(t => t.name.toLowerCase() === lowerName && t.platform === 'pumpfun')
            .value();
        if (existing) {
            return res.status(403).json({
                error: `Pump.fun token "${existing.name}" (${existing.symbol}) already exists.`,
                existingMint: existing.baseMint
            });
        }

        // ── Resolve deployer keypair ────────────────────────────────
        // Prefer user's server wallet secret (passed as JSON int array)
        // Fallback to platform WALLET_SECRET
        let deployerKeypair = wallet; // default: platform wallet
        if (deployer_secret) {
            try {
                const secretArr = typeof deployer_secret === 'string'
                    ? JSON.parse(deployer_secret)
                    : deployer_secret;
                deployerKeypair = Keypair.fromSecretKey(new Uint8Array(secretArr));
                console.log(`[pump-from-agent] Using deployer wallet: ${deployerKeypair.publicKey.toString().slice(0,8)}...`);
            } catch (kErr) {
                console.warn('[pump-from-agent] Invalid deployer_secret, falling back to platform wallet:', kErr.message);
            }
        }

        // ── Generate fresh mint keypair ─────────────────────────────
        const mintKeypair = Keypair.generate();
        const mintPubkey  = mintKeypair.publicKey.toString();
        console.log(`[pump-from-agent] mint: ${mintPubkey.slice(0,8)}...`);

        // ── Upload image → Pinata ───────────────────────────────────
        let imageUrl = 'https://arweave.net/WCM5h_34E8m3y_k-h1i59Q_P5I54k-H2d_s4b-C3xZM';
        if (req.file) {
            try {
                const fileBuffer = fs.readFileSync(req.file.path);
                const imageBlob  = new Blob([fileBuffer]);
                const imgResult  = await pinata.upload.public.file(imageBlob, {
                    pinataMetadata: { name: req.file.originalname }
                });
                imageUrl = `https://lom.mypinata.cloud/ipfs/${imgResult.cid}`;
                fs.unlinkSync(req.file.path);
                console.log(`[pump-from-agent] image uploaded: ${imgResult.cid}`);
            } catch (imgErr) {
                console.warn('[pump-from-agent] Image upload failed, using default:', imgErr.message);
                if (req.file && fs.existsSync(req.file.path)) fs.unlinkSync(req.file.path);
            }
        }

        // ── Upload metadata → Pinata ────────────────────────────────
        const tweetLink = twitter_url || `https://x.com/${deployer_handle || 'erebus'}`;
        const metadata = {
            name,
            symbol,
            description: description || `launched via @wwwEREBUS by @${deployer_handle || 'anon'} on pump.fun`,
            image:       imageUrl,
            website:     tweetLink,
            twitter:     tweetLink,
            showName:    true,
            createdOn:   'https://pump.fun'
        };

        let metadataUri;
        try {
            const jsonResult = await pinata.upload.public.json(metadata, {
                pinataMetadata: { name: `${symbol}-pumpfun-metadata.json` }
            });
            metadataUri = `https://ipfs.io/ipfs/${jsonResult.cid}`;
            console.log(`[pump-from-agent] metadata uploaded: ${jsonResult.cid}`);
        } catch (metaErr) {
            return res.status(500).json({ error: `Metadata upload failed: ${metaErr.message}` });
        }

        // ── Call pump.fun API: sign-create-tx ───────────────────────
        // pump.fun's API builds the create instruction and returns a
        // serialized transaction (base64) that we sign server-side.
        const PUMP_API = 'https://frontend-api-v3.pump.fun';
        const pumpAuthToken = process.env.PUMP_FUN_AUTH_TOKEN || '';

        const signCreateBody = {
            publicKey:   deployerKeypair.publicKey.toString(),
            action:      'create',
            tokenMetadata: {
                name,
                symbol,
                uri: metadataUri,
            },
            mint:        mintPubkey,
            denominatedInSol: 'true',
            amount:      0.01,   // 0.01 SOL initial buy
            slippage:    10,
            priorityFee: 0.0005,
            pool:        'pump',
        };

        console.log('[pump-from-agent] Calling pump.fun sign-create-tx...', { name, symbol, mint: mintPubkey.slice(0,8) });

        let signCreateResp;
        try {
            const fetchResp = await fetch(`${PUMP_API}/coins/sign-create-tx`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': '*/*',
                    'Origin': 'https://pump.fun',
                    'Referer': 'https://pump.fun/',
                    'User-Agent': 'Mozilla/5.0 (compatible; erebus-agent/1.0)',
                    ...(pumpAuthToken ? { 'Cookie': `auth_token=${pumpAuthToken}` } : {}),
                },
                body: JSON.stringify(signCreateBody),
            });

            if (!fetchResp.ok) {
                const errText = await fetchResp.text();
                console.error('[pump-from-agent] sign-create-tx HTTP error:', fetchResp.status, errText.slice(0, 200));
                return res.status(500).json({ error: `pump.fun API error ${fetchResp.status}: ${errText.slice(0, 120)}` });
            }

            signCreateResp = await fetchResp.json();
            console.log('[pump-from-agent] sign-create-tx response keys:', Object.keys(signCreateResp));
        } catch (apiErr) {
            console.error('[pump-from-agent] sign-create-tx fetch error:', apiErr.message);
            return res.status(500).json({ error: `pump.fun API unreachable: ${apiErr.message}` });
        }

        // ── Deserialize, sign, send ────────────────────────────────
        // pump.fun returns the tx in response.tx (base64 or array of numbers)
        let txBytes;
        if (signCreateResp.tx) {
            if (typeof signCreateResp.tx === 'string') {
                // base64 encoded
                txBytes = Buffer.from(signCreateResp.tx, 'base64');
            } else if (Array.isArray(signCreateResp.tx)) {
                txBytes = Buffer.from(signCreateResp.tx);
            } else if (signCreateResp.tx.data) {
                // {type: 'Buffer', data: [...]}
                txBytes = Buffer.from(signCreateResp.tx.data);
            }
        }

        if (!txBytes || txBytes.length === 0) {
            console.error('[pump-from-agent] No tx bytes from pump.fun:', JSON.stringify(signCreateResp).slice(0, 300));
            return res.status(500).json({ error: 'pump.fun returned no transaction bytes' });
        }

        // ── Deserialize as VersionedTransaction ────────────────────
        let versionedTx;
        try {
            versionedTx = VersionedTransaction.deserialize(txBytes);
        } catch (deErr) {
            console.error('[pump-from-agent] VersionedTransaction.deserialize failed:', deErr.message);
            return res.status(500).json({ error: `Tx deserialize failed: ${deErr.message}` });
        }

        // ── Refresh blockhash & sign with both keypairs ─────────────
        const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash('confirmed');
        versionedTx.message.recentBlockhash = blockhash;

        // Must sign with deployer (payer/creator) AND mint keypair
        versionedTx.sign([deployerKeypair, mintKeypair]);
        console.log('[pump-from-agent] Tx signed. Sending to pump.fun /send-transaction...');

        // ── Send tx via pump.fun send-transaction endpoint ──────────
        // (also try RPC directly as fallback)
        const serializedTx  = Buffer.from(versionedTx.serialize()).toString('base64');
        let signature = null;

        // Primary: pump.fun send-transaction
        try {
            const sendResp = await fetch(`${PUMP_API}/send-transaction`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': '*/*',
                    'Origin': 'https://pump.fun',
                    'Referer': 'https://pump.fun/',
                    ...(pumpAuthToken ? { 'Cookie': `auth_token=${pumpAuthToken}` } : {}),
                },
                body: JSON.stringify({
                    signedTransaction: serializedTx,
                    txType:            'create',
                }),
            });

            if (sendResp.ok) {
                const sendData = await sendResp.json();
                signature = sendData.signature || sendData.tx || sendData.txid || null;
                console.log('[pump-from-agent] pump.fun send-transaction response:', JSON.stringify(sendData).slice(0, 120));
            } else {
                const errText = await sendResp.text();
                console.warn('[pump-from-agent] pump.fun send-transaction failed, falling back to RPC:', sendResp.status, errText.slice(0, 120));
            }
        } catch (sendErr) {
            console.warn('[pump-from-agent] pump.fun send-transaction error, falling back to RPC:', sendErr.message);
        }

        // Fallback: broadcast directly via Solana RPC
        if (!signature) {
            try {
                console.log('[pump-from-agent] Attempting direct RPC broadcast...');
                signature = await connection.sendRawTransaction(versionedTx.serialize(), {
                    skipPreflight: false,
                    preflightCommitment: 'confirmed',
                    maxRetries: 5,
                });
                console.log('[pump-from-agent] RPC broadcast succeeded:', signature.slice(0, 12));
            } catch (rpcErr) {
                console.error('[pump-from-agent] RPC broadcast failed:', rpcErr.message);
                return res.status(500).json({ error: `Broadcast failed: ${rpcErr.message}` });
            }
        }

        // ── Confirm tx ──────────────────────────────────────────────
        try {
            await connection.confirmTransaction({ signature, blockhash, lastValidBlockHeight }, 'confirmed');
            console.log(`[pump-from-agent] tx confirmed: ${signature.slice(0, 12)}...`);
        } catch (confErr) {
            // Don't fail — tx may be confirmed even if confirmTransaction throws
            console.warn('[pump-from-agent] confirm warning (tx may still be ok):', confErr.message);
        }

        // ── Save to DB ──────────────────────────────────────────────
        const deployerHandle = deployer_handle || 'anon';
        const tokenRecord = {
            baseMint:          mintPubkey,
            quote:             'SOL',
            deployer:          deployerKeypair.publicKey.toString(),
            deployer_x_handle: deployerHandle,
            name,
            symbol,
            description:       metadata.description,
            pool:              '',   // pump.fun manages pool internally (bonding curve)
            uri:               metadataUri,
            website:           tweetLink,
            twitter:           tweetLink,
            imageUrl,
            createdAt:         new Date().toISOString(),
            migrated:          false,
            launchedViaX:      true,
            platform:          'pumpfun',
            initialBuySol:     0.01,
            signature,
        };
        db.get('tokens').push(tokenRecord).write();

        addEggPoints(deployerKeypair.publicKey.toString(), EGG_PER_LAUNCH);

        console.log(`🟢 [pump-launch] ${name} (${symbol}) by @${deployerHandle} | mint=${mintPubkey.slice(0,8)}... | sig=${signature.slice(0,12)}...`);

        res.status(200).json({
            success:       true,
            baseMint:      mintPubkey,
            signature,
            solscan:       `https://solscan.io/token/${mintPubkey}`,
            pumpfun:       `https://pump.fun/coin/${mintPubkey}`,
            imageUrl,
            uri:           metadataUri,
            twitterUrl:    tweetLink,
            initialBuySol: 0.01,
            platform:      'pumpfun',
        });

    } catch (err) {
        console.error('[pump-from-agent] error:', err);
        res.status(500).json({ error: err.message });
    }
});

// =====================================================================
// ✅ FIXED /sign-and-send — removed manual signature extraction loop
//    The user sends their already-signed tx; server co-signs with mint
//    keypair via partialSign, then serializes and broadcasts.
// =====================================================================
app.post('/sign-and-send', async (req, res) => {
    try {
        const { userSignedTransaction, keypairFile } = req.body;
        if (!userSignedTransaction || !keypairFile) {
            return res.status(400).json({ error: 'Missing required fields' });
        }
        const vanityDir = path.join(process.env.RENDER ? '/data' : __dirname, 'vanity');
        const keypairPath = path.join(vanityDir, keypairFile);
        if (!fs.existsSync(keypairPath)) {
            throw new Error(`Keypair file not found: ${keypairFile}`);
        }
        const secretKey = JSON.parse(fs.readFileSync(keypairPath, 'utf-8'));
        const mintKeypair = Keypair.fromSecretKey(new Uint8Array(secretKey));

        // Deserialize the already-user-signed transaction
        const transaction = Transaction.from(Buffer.from(userSignedTransaction, 'base64'));

        // Mint keypair co-signs — partialSign adds its signature without
        // disturbing the user's existing signature slot
        transaction.partialSign(mintKeypair);

        // Both signatures now present — serialize and send
        const serializedTx = transaction.serialize();
        const signature = await connection.sendRawTransaction(serializedTx, {
            skipPreflight: false,
            preflightCommitment: 'confirmed',
            maxRetries: 5
        });
        await connection.confirmTransaction(signature, 'confirmed');
        console.log(`[SIGN-AND-SEND] Successfully sent tx with signature: ${signature}`);
        res.status(200).json({ signature });
    } catch (err) {
        console.error("Error in /sign-and-send endpoint:", err);
        res.status(500).json({ error: err.message || 'An unknown error occurred' });
    }
});

app.get('/token-image', async (req, res) => {
    try {
        const { url } = req.query;
        if (!url || !String(url).startsWith('http')) {
            return res.status(400).send('Invalid URL');
        }
        const hash = crypto.createHash('md5').update(url).digest('hex');
        const extension = path.extname(new URL(url).pathname) || '.png';
        const filePath = path.join(cacheDir, `${hash}${extension}`);
        if (fs.existsSync(filePath)) {
            return res.sendFile(filePath);
        }
        const response = await fetch(url);
        if (!response.ok) {
            throw new Error(`Failed to fetch image: ${response.statusText}`);
        }
        const imageBuffer = Buffer.from(await response.arrayBuffer());
        fs.writeFileSync(filePath, imageBuffer);
        res.sendFile(filePath);
    } catch (error) {
        console.error(`Image cache error for url ${req.query.url}:`, error.message);
        res.sendFile(path.join(__dirname, 'public', 'orby.png'));
    }
});

// --- COMMENTS & NICKNAME API ENDPOINTS ---

app.get('/api/comments/:tokenMint', (req, res) => {
    const { tokenMint } = req.params;
    const twentyFourHoursAgo = new Date(Date.now() - 24 * 60 * 60 * 1000);

    try {
        const recentComments = db.get('comments')
            .filter(c => c.tokenMint === tokenMint && new Date(c.timestamp) > twentyFourHoursAgo)
            .orderBy('timestamp', 'asc')
            .value();

        const enrichedComments = recentComments.map(comment => {
            const walletProfile = db.get('wallets').find({ address: comment.wallet }).value();
            return {
                ...comment,
                nickname: walletProfile?.nickname || null
            };
        });

        res.json(enrichedComments);
    } catch (err) {
        console.error("Error fetching comments:", err);
        res.status(500).json({ error: 'Failed to fetch comments.' });
    }
});

app.post('/api/comments', (req, res) => {
    const { tokenMint, wallet, text } = req.body;

    if (!tokenMint || !wallet || !text || text.trim().length === 0) {
        return res.status(400).json({ error: 'Missing required fields (tokenMint, wallet, text).' });
    }
    if (text.trim().length > 500) {
        return res.status(400).json({ error: 'Comment is too long (max 500 characters).' });
    }

    try {
        const newComment = {
            id: Date.now().toString() + Math.random().toString(36).substring(2, 9),
            tokenMint,
            wallet,
            text: text.trim(),
            timestamp: new Date().toISOString()
        };

        db.get('comments').push(newComment).write();
        addEggPoints(wallet, EGG_PER_COMMENT); // +2 $EGG per comment
        
        let walletProfile = db.get('wallets').find({ address: wallet });
        
        if (!walletProfile.value()) {
            db.get('wallets').push({ address: wallet, points: 0, completedQuests: [], nicknameChanges: [] }).write();
            walletProfile = db.get('wallets').find({ address: wallet });
        }

        if (walletProfile.value()) {
            const userCommentCount = db.get('comments').filter({ wallet }).size().value();
            const userQuests = walletProfile.value().completedQuests || [];

            const completeQuest = (questId) => {
                const quest = masterQuests.find(q => q.id === questId);
                if (quest && !userQuests.includes(questId) && walletProfile.value()) {
                    walletProfile.get('completedQuests').push(questId).write();
                    walletProfile.update('points', p => (p || 0) + quest.points).write();
                    console.log(`🎉 Quest Complete! ${wallet.slice(0,6)} unlocked '${quest.title}'!`);
                }
            };

            if (userCommentCount === 1) {
                 completeQuest('SOCIALITE');
            }

            if (userCommentCount >= 25) {
                completeQuest('COMMUNITY_PILLAR');
            }
        }

        const updatedWalletProfile = db.get('wallets').find({ address: wallet }).value();
        const enrichedComment = {
            ...newComment,
            nickname: updatedWalletProfile?.nickname || null
        };

        res.status(201).json(enrichedComment);

    } catch (err) {
        console.error("Error posting comment:", err);
        res.status(500).json({ error: 'Failed to post comment due to a server error.' });
    }
});

app.get('/api/top-traders', async (req, res) => {
    try {
        const tradersRes = await fetch(`http://localhost:${process.env.PORT || 3000}/api/leaderboard?page=1&limit=50`);
        if (!tradersRes.ok) throw new Error('Failed to fetch traders');
        const { leaderboard: traders } = await tradersRes.json();

        const TRADER_EARNINGS_PER = 50.97;
        const tradersList = traders.map(trader => ({
            address: trader.walletAddress,
            earnings: TRADER_EARNINGS_PER
        }));

        res.setHeader('Content-Type', 'application/json');
        res.setHeader('Content-Disposition', 'attachment; filename="top-50-traders-usdc.json"');
        
        res.status(200).json(tradersList);

    } catch (err) {
        console.error("Error in /api/top-traders:", err);
        res.status(500).json({ error: 'Failed to generate traders list.' });
    }
});

app.get('/api/top-holders', async (req, res) => {
    try {
        const ZCOIN_MINT = 'DQSmLyJgGyw83J3WhuVBzaFBRT2xaqF4mwkC9QD4o2AU';

        const holdersRes = await fetch(`http://localhost:${process.env.PORT || 3000}/api/top-holders/${ZCOIN_MINT}`);
        if (!holdersRes.ok) throw new Error('Failed to fetch holders');
        const holders = await holdersRes.json();

        const holdersList = holders.map(holder => ({
            address: holder.address,
            earnings: holder.estimatedEarnings || 0
        }));

        res.setHeader('Content-Type', 'application/json');
        res.setHeader('Content-Disposition', 'attachment; filename="top-100-holders-usdc.json"');
        
        res.status(200).json(holdersList);

    } catch (err) {
        console.error("Error in /api/top-holders:", err);
        res.status(500).json({ error: 'Failed to generate holders list.' });
    }
});

app.get('/api/recent-trades', (req, res) => {
    try {
        const limit = parseInt(req.query.limit) || 20;
        const tokens = db.get('tokens').value();
        
        const recentTrades = db.get('trades')
            .orderBy('timestamp', 'desc')
            .take(limit)
            .value();
        
        const enrichedTrades = recentTrades.map(trade => {
            const token = tokens.find(t => t.baseMint === trade.tokenMint);
            return {
                signature: trade.signature,
                type: trade.type,
                tokenMint: trade.tokenMint,
                tokenName: token?.name || 'Unknown',
                tokenSymbol: token?.symbol || '???',
                tokenLogo: token?.imageUrl || 'orby.png',
                solAmount: trade.solVolume || 0,
                usdAmount: trade.usdVolume || 0,
                wallet: trade.traderAddress,
                timestamp: new Date(trade.timestamp).getTime()
            };
        });
        
        res.json(enrichedTrades);
    } catch (err) {
        console.error("Error fetching recent trades:", err);
        res.status(500).json({ error: 'Failed to fetch recent trades.' });
    }
});

function broadcastTrade(trade) {
    const tokens = db.get('tokens').value();
    const token = tokens.find(t => t.baseMint === trade.tokenMint);
    
    const tradeData = {
        type: 'newTrade',
        trade: {
            signature: trade.signature,
            tradeType: trade.type,
            tokenMint: trade.tokenMint,
            tokenName: token?.name || 'Unknown',
            tokenSymbol: token?.symbol || '???',
            tokenLogo: token?.imageUrl ? `/token-image?url=${encodeURIComponent(token.imageUrl)}` : 'orby.png',
            solAmount: trade.solVolume || 0,
            wallet: trade.traderAddress,
            timestamp: Date.now()
        }
    };
    
    wss.clients.forEach(client => {
        if (client.readyState === 1) {
            client.send(JSON.stringify(tradeData));
        }
    });
    
    console.log(`📡 Broadcasted ${trade.type} trade: ${token?.symbol || trade.tokenMint.slice(0,6)}`);
}

app.get('/api/export-rewards', async (req, res) => {
    try {
        const ZCOIN_MINT = 'DQSmLyJgGyw83J3WhuVBzaFBRT2xaqF4mwkC9QD4o2AU';

        const tradersRes = await fetch(`http://localhost:${process.env.PORT || 3000}/api/leaderboard?page=1&limit=50`);
        if (!tradersRes.ok) throw new Error('Failed to fetch traders');
        const { leaderboard: traders } = await tradersRes.json();

        const TRADER_EARNINGS_PER = 50.97;
        traders.forEach(trader => {
            trader.earnings = TRADER_EARNINGS_PER;
            trader.source = 'trader';
        });

        const holdersRes = await fetch(`http://localhost:${process.env.PORT || 3000}/api/top-holders/${ZCOIN_MINT}`);
        if (!holdersRes.ok) throw new Error('Failed to fetch holders');
        const holders = await holdersRes.json();

        holders.forEach(holder => {
            holder.earnings = holder.estimatedEarnings || 0;
            holder.source = 'holder';
        });

        const allWallets = [...traders, ...holders];
        const walletMap = new Map();

        allWallets.forEach(wallet => {
            const key = wallet.walletAddress || wallet.address;
            if (!key) return;

            if (walletMap.has(key)) {
                const existing = walletMap.get(key);
                existing.earnings += wallet.earnings;
                existing.sources = [...(existing.sources || []), wallet.source];
            } else {
                walletMap.set(key, {
                    address: key,
                    nickname: wallet.nickname || null,
                    earnings: wallet.earnings,
                    sources: [wallet.source],
                    amount: wallet.amount || 0,
                    points: wallet.totalPoints || 0
                });
            }
        });

        let mergedWallets = Array.from(walletMap.values()).sort((a, b) => b.earnings - a.earnings);

        const csvHeader = 'Address,Nickname,Earnings (USD),Sources,Amount Held,Points\n';
        const csvRows = mergedWallets.map(w => 
            `"${w.address}","${w.nickname || ''}","${w.earnings.toFixed(2)}","${w.sources.join(', ')}","${w.amount.toLocaleString() || 'N/A'}","${w.points.toLocaleString() || 'N/A'}"\n`
        ).join('');

        const csvContent = csvHeader + csvRows;

        res.setHeader('Content-Type', 'text/csv');
        res.setHeader('Content-Disposition', 'attachment; filename="top-traders-holders-rewards.csv"');

        res.status(200).send(csvContent);

    } catch (err) {
        console.error("Error in /api/export-rewards:", err);
        res.status(500).json({ error: 'Failed to generate export data.' });
    }
});

app.get('/api/all-pool-addresses', (req, res) => {
    console.log('[API] Request received for /api/all-pool-addresses');
    try {
        const tokens = db.get('tokens').value();
        
        const poolAddresses = tokens
            .filter(token => token.pool)
            .map(token => token.pool);

        res.status(200).json(poolAddresses);
    } catch (err) {
        console.error("Error in /api/all-pool-addresses:", err);
        res.status(500).json({ error: 'Failed to retrieve pool addresses.' });
    }
});

app.post('/api/nickname', (req, res) => {
    const { walletAddress, nickname } = req.body;
    const NICKNAME_CHANGE_LIMIT = 5;

    if (!walletAddress || !nickname || nickname.trim().length === 0) {
        return res.status(400).json({ error: 'Wallet address and nickname are required.' });
    }
    if (nickname.trim().length > 20) {
        return res.status(400).json({ error: 'Nickname is too long (max 20 characters).' });
    }

    try {
        let walletProfile = db.get('wallets').find({ address: walletAddress });
        
        if (!walletProfile.value()) {
            db.get('wallets').push({ address: walletAddress, points: 0, completedQuests: [], nicknameChanges: [] }).write();
            walletProfile = db.get('wallets').find({ address: walletAddress });
        }

        const currentMonth = new Date().toISOString().slice(0, 7);
        let monthlyChanges = walletProfile.get('nicknameChanges').find({ month: currentMonth }).value();

        if (monthlyChanges && monthlyChanges.count >= NICKNAME_CHANGE_LIMIT) {
            return res.status(429).json({ error: `You have reached your limit of ${NICKNAME_CHANGE_LIMIT} nickname changes for this month.` });
        }

        if (monthlyChanges) {
            db.get('wallets').find({ address: walletAddress }).get('nicknameChanges').find({ month: currentMonth }).assign({ count: monthlyChanges.count + 1 }).write();
        } else {
            db.get('wallets').find({ address: walletAddress }).get('nicknameChanges').push({ month: currentMonth, count: 1 }).write();
        }

        db.get('wallets').find({ address: walletAddress }).assign({ nickname: nickname.trim() }).write();

        res.json({ success: true, message: 'Nickname updated successfully.' });

    } catch (err) {
        console.error("Error updating nickname:", err);
        res.status(500).json({ error: 'Failed to update nickname.' });
    }
});

app.get('/api/leaderboard', async (req, res) => {
    try {
        const page = parseInt(req.query.page) || 1;
        const limit = parseInt(req.query.limit) || 10;
        const searchTerm = (req.query.search || '').toLowerCase();

        const wallets = db.get('wallets').value();
        
        let sortedWallets = wallets
            .filter(w => w.points && w.points > 0)
            .sort((a, b) => b.points - a.points);
        
        if (searchTerm) {
            sortedWallets = sortedWallets.filter(w => 
                w.address.toLowerCase().includes(searchTerm) ||
                (w.nickname && w.nickname.toLowerCase().includes(searchTerm))
            );
        }

        const totalWallets = sortedWallets.length;
        const totalPages = Math.ceil(totalWallets / limit);
        const startIndex = (page - 1) * limit;
        const endIndex = startIndex + limit;
        const paginatedWallets = sortedWallets.slice(startIndex, endIndex);

        const leaderboardData = paginatedWallets.map((wallet, index) => ({
            rank: startIndex + index + 1,
            walletAddress: wallet.address,
            totalPoints: wallet.points,
            nickname: wallet.nickname || null
        }));
        
        const zecPrice = await getZecPrice();
        let traderEarningsUsdc = 0;
        let traderEarningsZec = 0;
        
        try {
            const holdersData = await fetchTopHoldersWithEarnings('DQSmLyJgGyw83J3WhuVBzaFBRT2xaqF4mwkC9QD4o2AU');
            traderEarningsUsdc = holdersData.traderEarningsUsdc || 0;
            traderEarningsZec = holdersData.traderEarningsZec || 0;
        } catch (e) {
            console.error('Error fetching trader earnings for leaderboard:', e);
        }

        res.status(200).json({
            leaderboard: leaderboardData,
            pagination: {
                currentPage: page,
                totalPages,
                totalWallets
            },
            traderEarnings: {
                usdc: traderEarningsUsdc,
                zec: traderEarningsZec,
                zecPrice: zecPrice
            }
        });

    } catch (err) {
        console.error("Error in /api/leaderboard:", err);
        res.status(500).json({ error: 'Failed to get leaderboard data.' });
    }
});

async function fetchTop10Holders(tokenMint) {
    try {
        const response = await fetch(`https://datapi.jup.ag/v1/holders/${tokenMint}`);
        if (!response.ok) {
            throw new Error(`Jupiter Holders API failed with status ${response.status}`);
        }
        const data = await response.json();
        
        let rawArray = [];
        if (data && Array.isArray(data.holders)) {
            rawArray = data.holders;
        }
        
        const top10 = rawArray.slice(0, 10);

        return top10.map((holder, index) => {
            return {
                rank: index + 1,
                address: holder.address || `Holder${index + 1}`,
                amount: holder.amount || 0,
                amountDisplay: holder.amountDisplay || '0',
                isPool: (holder.name && holder.name.toLowerCase().includes('pool')) || false
            };
        });

    } catch (error) {
        console.error("Error fetching top 10 holders:", error.message);
        return [];
    }
}

app.get('/api/top-token-holders/:tokenMint', async (req, res) => {
    try {
        const { tokenMint } = req.params;
        const holders = await fetchTop10Holders(tokenMint);
        res.status(200).json(holders);
    } catch (err) {
        console.error("Error fetching top holders:", err);
        res.status(500).json({ error: 'Failed to fetch top holders data.' });
    }
});

app.get('/api/top-tokens', (req, res) => {
    try {
        const tokens = db.get('tokens').value();
        const trades = db.get('trades').value();
        
        const FORBIDDEN_WORDS = ['jeffy', 'jeff yu', 'jeffyu', 'jeffy yu'];
        
        const filteredTokens = tokens.filter(t => {
            const lowerName = (t.name || '').toLowerCase();
            const lowerSymbol = (t.symbol || '').toLowerCase();
            return !FORBIDDEN_WORDS.some(word => 
                lowerName.includes(word) || lowerSymbol.includes(word)
            );
        });
        
        const tokenVolumes = {};
        for (const trade of trades) {
            tokenVolumes[trade.tokenMint] = (tokenVolumes[trade.tokenMint] || 0) + trade.solVolume;
        }
        
        const topTokens = filteredTokens
            .map(token => {
                const volume = tokenVolumes[token.baseMint] || 0;
                return {
                    baseMint: token.baseMint,
                    symbol: token.symbol,
                    name: token.name,
                    volume: volume
                };
            })
            .sort((a, b) => b.volume - a.volume)
            .slice(0, 10);
            
        res.json(topTokens);
    } catch (err) {
        console.error("Error generating top tokens list:", err);
        res.status(500).json({ error: 'Failed to get top tokens.' });
    }
});

app.get('/api/bounties/:walletAddress', (req, res) => {
    const { walletAddress } = req.params;
    console.log(`GET /api/bounties for wallet: ${walletAddress}`);
    const allBounties = [
        { id: 1, name: 'First Contact', description: 'Make your very first trade on a orby token.', unlocked: false },
        { id: 2, name: 'Creator', description: 'Launch your first virtual token.', unlocked: false },
        { id: 3, name: 'Launch Artisan', description: 'Launch 5 or more virtual tokens.', unlocked: false },
        { id: 4, name: 'Pioneer', description: 'Trade a token when it has less than 50 holders.', unlocked: false },
        { id: 5, name: 'Whale Trader', description: 'Trade over 100 SOL in total volume.', unlocked: false },
    ];
    if (walletAddress.startsWith('DDx')) {
        allBounties[0].unlocked = true;
        allBounties[1].unlocked = true;
    }
    res.json(allBounties);
});

app.post('/confirm-creation', async (req, res) => {
    try {
        const { signature, baseMint, quote, deployer, name, symbol, description, keypairFile, uri, website, twitter, imageUrl } = req.body;
        if (!signature) {
            return res.status(400).json({ error: 'Missing transaction signature' });
        }

        let tx = null;
        let attempts = 0;
        while (!tx && attempts < 5) {
            console.log(`[CONFIRM] Attempt ${attempts + 1}: Fetching transaction ${signature}`);
            try {
                tx = await connection.getTransaction(signature, { maxSupportedTransactionVersion: 0 });
            } catch (error) {
                console.warn(`[CONFIRM] Attempt ${attempts + 1} failed to fetch tx, retrying...`, error.message);
            }
            if (!tx) {
                attempts++;
                await new Promise(resolve => setTimeout(resolve, 2000));
            }
        }
        
        if (!tx) {
            console.error(`[CONFIRM] FAILED: Transaction ${signature} not found after multiple attempts.`);
            throw new Error("Transaction not found on-chain after multiple retries. Could not confirm pool creation.");
        }

        const accountKeys = tx.transaction.message.accountKeys.map(key => key.toString());
        let correctPoolAddress = null;
        const initInstruction = tx.transaction.message.instructions.find(ix => {
            const programId = accountKeys[ix.programIdIndex];
            return programId === 'dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN' && ix.accounts.length > 10;
        });

        if (initInstruction) {
            correctPoolAddress = accountKeys[initInstruction.accounts[5]];
        }

        if (!correctPoolAddress) {
            throw new Error("Could not find pool address in transaction instructions.");
        }

        console.log(`[CONFIRM] Found correct pool address from instructions: ${correctPoolAddress}`);
        let walletProfile = db.get('wallets').find({ address: deployer });
        if (!walletProfile.value()) {
            db.get('wallets').push({ address: deployer, points: 0, totalVolumeSol: 0, completedQuests: [], profitableFlips: 0, deployedCount: 0 }).write();
            walletProfile = db.get('wallets').find({ address: deployer });
        }

        const newDeployedCount = (walletProfile.value().deployedCount || 0) + 1;
        addEggPoints(deployer, EGG_PER_LAUNCH); // +100 $EGG per token launch
        walletProfile.assign({ deployedCount: newDeployedCount }).write();

        const creatorQuest = masterQuests.find(q => q.id === 'FIRST_LAUNCH');
        if (creatorQuest && !walletProfile.value().completedQuests.includes('FIRST_LAUNCH')) {
            walletProfile.get('completedQuests').push('FIRST_LAUNCH').write();
            walletProfile.update('points', p => (p || 0) + creatorQuest.points).write();
            console.log(`🎉 Quest Complete! ${deployer} unlocked '${creatorQuest.title}'!`);
        }

        const artisanQuest = masterQuests.find(q => q.id === 'SERIAL_LAUNCHER');
        if (artisanQuest && newDeployedCount >= 5 && !walletProfile.value().completedQuests.includes('SERIAL_LAUNCHER')) {
            walletProfile.get('completedQuests').push('SERIAL_LAUNCHER').write();
            walletProfile.update('points', p => (p || 0) + artisanQuest.points).write();
            console.log(`🎉 Quest Complete! ${deployer} unlocked '${artisanQuest.title}'!`);
        }

        const vanityDir = path.join(process.env.RENDER ? '/data' : __dirname, 'vanity');
        const usedDir = path.join(vanityDir, 'used');
        const keypairPath = path.join(vanityDir, keypairFile);
        if (fs.existsSync(keypairPath)) {
            if (!fs.existsSync(usedDir)) {
                fs.mkdirSync(usedDir, { recursive: true });
            }
            fs.renameSync(keypairPath, path.join(usedDir, keypairFile));
        }

        db.get('tokens').push({ 
            baseMint, 
            quote, 
            deployer, 
            name, 
            symbol, 
            description: description || '',
            pool: correctPoolAddress,
            uri, 
            website, 
            twitter,
            imageUrl: imageUrl || 'https://arweave.net/WCM5h_34E8m3y_k-h1i59Q_P5I54k-H2d_s4b-C3xZM',
            createdAt: new Date().toISOString(),
            migrated: false
        }).write();
        
        res.status(200).json({ success: true, poolAddress: correctPoolAddress });
    } catch (err) {
        console.error("Error in /confirm-creation:", err);
        res.status(500).json({ error: err.message });
    }
});

app.get('/platform-stats', async (req, res) => {
    try {
        const tokens = db.get('tokens').value();
        const trades = db.get('trades').value();
        
        const FORBIDDEN_WORDS = ['jeffy', 'jeff yu', 'jeffyu', 'jeffy yu'];
        
        const filteredTokens = tokens.filter(t => {
            const lowerName = (t.name || '').toLowerCase();
            const lowerSymbol = (t.symbol || '').toLowerCase();
            return !FORBIDDEN_WORDS.some(word => 
                lowerName.includes(word) || lowerSymbol.includes(word)
            );
        });
        
        const enrichedTokens = await Promise.all(filteredTokens.map(enrichWithJupiterData));
        
        const totalTokens = enrichedTokens.length;
        const totalVolume24h = enrichedTokens.reduce((sum, token) => sum + (token.stats24h?.buyVolume ?? 0), 0);
        const totalLiquidity = enrichedTokens.reduce((sum, token) => sum + (token.liquidity ?? 0), 0);
        const now = new Date();
        const oneDayAgo = new Date(now.getTime() - (24 * 60 * 60 * 1000));
        const newTokens24h = enrichedTokens.filter(t => t.createdAt && new Date(t.createdAt) > oneDayAgo).length;

        const totalVolumeAllTime = trades.reduce((sum, trade) => sum + (trade.usdVolume || 0), 0);
        const platformEarnings = totalVolumeAllTime * 0.013;

        const sortedByVolume = [...enrichedTokens].sort((a, b) => (b.stats24h?.buyVolume ?? 0) - (a.stats24h?.buyVolume ?? 0));
        const top5ByVolume = sortedByVolume.slice(0, 5).map(t => ({ name: t.name, symbol: t.symbol, volume: t.stats24h?.buyVolume ?? 0, imageUrl: t.imageUrl }));
        const sortedByMarketCap = [...enrichedTokens].sort((a, b) => (b.mcap ?? 0) - (a.mcap ?? 0));
        const top5ByMarketCap = sortedByMarketCap.slice(0, 5).map(t => ({ name: t.name, symbol: t.symbol, mcap: t.mcap ?? 0, imageUrl: t.imageUrl }));

        res.status(200).json({
            totalTokens,
            totalVolume24h,
            totalLiquidity,
            newTokens24h,
            top5ByVolume,
            top5ByMarketCap,
            platformEarnings
        });
    } catch (err) {
        console.error("Error in /platform-stats:", err);
        res.status(500).json({ error: "Failed to generate platform stats." });
    }
});

const formatDate = (date) => {
    const d = new Date(date);
    let month = '' + (d.getMonth() + 1);
    let day = '' + d.getDate();
    const year = d.getFullYear();
    if (month.length < 2) month = '0' + month;
    if (day.length < 2) day = '0' + day;
    return [year, month, day].join('-');
};

app.get('/api/profile/:walletAddress', async (req, res) => {
    const { walletAddress } = req.params;
    if (!walletAddress || walletAddress === 'none') {
        return res.status(404).json({ error: "Profile not found" });
    }
    try {
        const walletProfile = db.get('wallets').find({ address: walletAddress }).value();
        const quests = db.get('quests').value();
        const unlockedAchievements = walletProfile?.completedQuests?.map(questId => {
            return quests.find(q => q.id === questId);
        }).filter(Boolean) || [];
        const recentActivity = db.get('trades')
            .filter({ traderAddress: walletAddress })
            .orderBy('timestamp', 'desc')
            .take(10)
            .value();
        const junknetTokens = db.get('tokens').value();
        let currentHoldings = [];
        for (const token of junknetTokens) {
            try {
                const holderRes = await fetch(`https://datapi.jup.ag/v1/holders/${token.baseMint}`);
                if (holderRes.ok) {
                    const data = await holderRes.json();
                    const userHolding = data.holders.find(h => h.address === walletAddress);
                    if (userHolding && userHolding.amount > 0) {
                        currentHoldings.push({
                            token: `${token.name} (${token.symbol})`,
                            balance: userHolding.amount.toLocaleString(undefined, { maximumFractionDigits: 2 })
                        });
                    }
                }
            } catch (e) {
                console.error(`Could not fetch holders for ${token.baseMint}: ${e.message}`);
            }
        }
        const responseData = {
            walletAddress: walletAddress,
            flippingScore: walletProfile?.flippingScore || 0,
            totalPnlSol: walletProfile?.totalPnlSol || 0,
            totalVolumeSol: walletProfile?.totalVolumeSol || 0,
            currentHoldings: currentHoldings.length > 0 ? currentHoldings : [{ token: "No current holdings on orby", balance: "N/A" }],
            unlockedAchievements: unlockedAchievements,
            recentActivity: recentActivity
        };
        res.json(responseData);
    } catch (err) {
        console.error(`Failed to get profile for ${walletAddress}:`, err);
        res.status(500).json({ error: 'Failed to get profile data.' });
    }
});

app.get('/historical-stats', async (req, res) => {
    try {
        const tokens = db.get('tokens').value();
        const enrichedTokens = await Promise.all(tokens.map(enrichWithJupiterData));
        const statsByDay = {};
        enrichedTokens.forEach(token => {
            if (token.createdAt) {
                const date = formatDate(token.createdAt);
                if (!statsByDay[date]) {
                    statsByDay[date] = { volume: 0, tvl: 0, newTokens: 0 };
                }
                statsByDay[date].volume += token.stats24h?.buyVolume ?? 0;
                statsByDay[date].tvl += token.liquidity ?? 0;
                statsByDay[date].newTokens += 1;
            }
        });
        const sortedDates = Object.keys(statsByDay).sort((a, b) => new Date(a) - new Date(b));
        const labels = [];
        const volumeData = [];
        const tvlData = [];
        const deployedData = [];
        let cumulativeTokens = 0;
        sortedDates.forEach(date => {
            cumulativeTokens += statsByDay[date].newTokens;
            labels.push(date);
            volumeData.push(statsByDay[date].volume);
            tvlData.push(statsByDay[date].tvl);
            deployedData.push(cumulativeTokens);
        });
        res.status(200).json({
            labels,
            volumeData,
            tvlData,
            deployedData
        });
    } catch (err) {
        console.error("Error in /historical-stats:", err);
        res.status(500).json({ error: "Failed to get historical stats." });
    }
});

// --- REAL-TIME CHAT (WEBSOCKETS) ---
const wss = new WebSocketServer({ server });
wss.on('connection', ws => {
    console.log('🔌 New WebSocket connection');
    
    ws.on('message', rawData => {
        try {
            const msg = JSON.parse(rawData.toString());
            
            if (msg.type === 'join') {
                const room = msg.room;
                clientRooms.set(ws, room);
                if (!chatRooms.has(room)) {
                    chatRooms.set(room, []);
                }
                ws.send(JSON.stringify({
                    type: 'history',
                    messages: chatRooms.get(room).slice(-50)
                }));
            }
            
            if (msg.type === 'message') {
                const room = clientRooms.get(ws);
                if (room) {
                    const chatMsg = {
                        id: Date.now(),
                        wallet: msg.wallet,
                        text: msg.text,
                        timestamp: new Date().toISOString()
                    };
                    chatRooms.get(room).push(chatMsg);
                    if (chatRooms.get(room).length > 100) {
                        chatRooms.get(room).shift();
                    }
                    broadcast(room, { type: 'message', message: chatMsg });
                }
            }
        } catch (e) {
            console.error('WebSocket message error:', e);
        }
    });
    
    ws.on('close', () => {
        clientRooms.delete(ws);
    });
});

const ZCOIN_MINT = 'DQSmLyJgGyw83J3WhuVBzaFBRT2xaqF4mwkC9QD4o2AU';
const TOP_HOLDERS_URL = `https://datapi.jup.ag/v1/holders/${ZCOIN_MINT}`;

async function fetchTopHoldersWithEarnings(tokenMint) {
    const TRADERS_COUNT = 50;
    const HOLDERS_COUNT = 100;
    const PENALTY_PER_HOLDER = 1;

    try {
        const zecPrice = await getZecPrice();
        
        const internalApiUrl = `http://127.0.0.1:${process.env.PORT || 3000}/platform-stats`;
        const statsRes = await fetch(internalApiUrl); 
        if (!statsRes.ok) {
            throw new Error(`Internal API call to /platform-stats failed`);
        }
        const stats = await statsRes.json();
        
        const totalPlatformEarnings = stats.platformEarnings || 0;
        
        const communityRewardPool = totalPlatformEarnings * 0.70;
        
        const tradersPool = communityRewardPool * 0.25;
        const holdersPool = communityRewardPool * 0.40;
        
        const TRADER_EARNINGS_PER_USDC = tradersPool / TRADERS_COUNT;
        const perHolderBeforePenalty = holdersPool / HOLDERS_COUNT;
        const FINAL_PER_HOLDER_USDC = Math.max(0, perHolderBeforePenalty - PENALTY_PER_HOLDER);
        
        const TRADER_EARNINGS_ZEC = convertUsdcToZec(TRADER_EARNINGS_PER_USDC, zecPrice);
        const HOLDER_EARNINGS_ZEC = convertUsdcToZec(FINAL_PER_HOLDER_USDC, zecPrice);
        
        console.log(`💰 Platform Earnings: $${totalPlatformEarnings.toFixed(2)}`);
        console.log(`💰 Community Pool: $${communityRewardPool.toFixed(2)}`);
        console.log(`📊 Traders: ${TRADERS_COUNT} @ ${TRADER_EARNINGS_ZEC.toFixed(4)} ZEC ($${TRADER_EARNINGS_PER_USDC.toFixed(2)}) each`);
        console.log(`📊 Holders: ${HOLDERS_COUNT} @ ${HOLDER_EARNINGS_ZEC.toFixed(4)} ZEC ($${FINAL_PER_HOLDER_USDC.toFixed(2)}) each`);
        console.log(`💵 ZEC Price: $${zecPrice.toFixed(2)}`);

        const holdersRes = await fetch(TOP_HOLDERS_URL);
        if (!holdersRes.ok) {
            throw new Error(`Jupiter Holders API failed with status ${holdersRes.status}`);
        }
        const holdersData = await holdersRes.json();
        
        let rawHoldersArray = [];
        if (holdersData && Array.isArray(holdersData.holders)) {
            rawHoldersArray = holdersData.holders;
        }
        
        const top100 = rawHoldersArray.slice(0, HOLDERS_COUNT);
        
        const holdersWithEarnings = top100.map((holder, index) => ({
            address: holder.address || `UnknownHolder${index + 1}`,
            amount: holder.amount || 0,
            amountDisplay: holder.amountDisplay || '0', 
            estimatedEarningsUsdc: FINAL_PER_HOLDER_USDC,
            estimatedEarningsZec: HOLDER_EARNINGS_ZEC,
            zecDisplay: `${HOLDER_EARNINGS_ZEC.toFixed(4)} $ZEC 🪙`,
            usdDisplay: `$${FINAL_PER_HOLDER_USDC.toFixed(2)} USDC 💵`,
            isPool: (holder.name && holder.name.toLowerCase().includes('pool')) || false 
        }));

        const TOTAL_HOLDERS_POOL_USDC = FINAL_PER_HOLDER_USDC * HOLDERS_COUNT;
        const TOTAL_HOLDERS_POOL_ZEC = HOLDER_EARNINGS_ZEC * HOLDERS_COUNT;

        return {
            holders: holdersWithEarnings,
            totalPoolUsdc: TOTAL_HOLDERS_POOL_USDC,
            totalPoolZec: TOTAL_HOLDERS_POOL_ZEC,
            perHolderUsdc: FINAL_PER_HOLDER_USDC,
            perHolderZec: HOLDER_EARNINGS_ZEC,
            traderEarningsUsdc: TRADER_EARNINGS_PER_USDC,
            traderEarningsZec: TRADER_EARNINGS_ZEC,
            platformEarnings: totalPlatformEarnings,
            zecPrice: zecPrice
        };

    } catch (error) {
        console.error("Error in fetchTopHoldersWithEarnings:", error.message);
        
        const fallbackZecPrice = cachedZecPrice || 577;
        const fallbackPerHolderUsdc = 0;
        const fallbackPerHolderZec = 0;
        const fallbackPerTraderUsdc = 0;
        const fallbackPerTraderZec = 0;
        
        const fallbackHolders = Array.from({ length: Math.min(HOLDERS_COUNT, 100) }, (_, i) => ({
            address: `FallbackHolder${i + 1}`,
            amount: 0,
            amountDisplay: '0',
            estimatedEarningsUsdc: fallbackPerHolderUsdc,
            estimatedEarningsZec: fallbackPerHolderZec,
            zecDisplay: `${fallbackPerHolderZec.toFixed(4)} $ZEC 🪙`,
            usdDisplay: `$${fallbackPerHolderUsdc.toFixed(2)} USDC 💵`,
            isPool: false
        }));
        
        return {
            holders: fallbackHolders,
            totalPoolUsdc: 0,
            totalPoolZec: 0,
            perHolderUsdc: fallbackPerHolderUsdc,
            perHolderZec: fallbackPerHolderZec,
            traderEarningsUsdc: fallbackPerTraderUsdc,
            traderEarningsZec: fallbackPerTraderZec,
            zecPrice: fallbackZecPrice
        };
    }
}

app.get('/api/portfolio/:walletAddress', async (req, res) => {
    const { walletAddress } = req.params;
    
    try {
        const trades = db.get('trades').filter({ traderAddress: walletAddress }).value();
        const tokens = db.get('tokens').value();
        
        const holdings = {};
        
        for (const trade of trades) {
            if (!holdings[trade.tokenMint]) {
                holdings[trade.tokenMint] = {
                    mint: trade.tokenMint,
                    token: tokens.find(t => t.baseMint === trade.tokenMint),
                    totalBought: 0,
                    totalSold: 0,
                    totalBuyCost: 0,
                    totalSellRevenue: 0
                };
            }
            
            if (trade.type === 'buy') {
                holdings[trade.tokenMint].totalBought += trade.solVolume;
                holdings[trade.tokenMint].totalBuyCost += trade.usdVolume || 0;
            } else {
                holdings[trade.tokenMint].totalSold += trade.solVolume;
                holdings[trade.tokenMint].totalSellRevenue += trade.usdVolume || 0;
            }
        }
        
        const portfolio = Object.values(holdings).map(h => ({
            ...h,
            netPosition: h.totalBought - h.totalSold,
            realizedPnl: h.totalSellRevenue - (h.totalBuyCost * (h.totalSold / h.totalBought || 0)),
            tokenName: h.token?.name || 'Unknown',
            tokenSymbol: h.token?.symbol || '???',
            tokenLogo: h.token?.imageUrl
        }));
        
        res.json({
            totalTrades: trades.length,
            portfolio: portfolio.filter(p => p.netPosition > 0 || p.realizedPnl !== 0)
        });
        
    } catch (err) {
        console.error('Portfolio error:', err);
        res.status(500).json({ error: 'Failed to fetch portfolio' });
    }
});

app.get('/api/top-holders/:tokenMint', async (req, res) => {
    try {
        const { tokenMint } = req.params;
        const ZCOIN_MINT = 'DQSmLyJgGyw83J3WhuVBzaFBRT2xaqF4mwkC9QD4o2AU';
        
        let data = await fetchTopHoldersWithEarnings(ZCOIN_MINT);
        
        if (tokenMint !== ZCOIN_MINT) {
            try {
                const customHoldersRes = await fetch(`https://datapi.jup.ag/v1/holders/${tokenMint}`);
                if (customHoldersRes.ok) {
                    const customHoldersData = await customHoldersRes.json();
                    const rawCustomArray = Array.isArray(customHoldersData.holders) ? customHoldersData.holders : [];
                    const top100Custom = rawCustomArray.slice(0, 100);
                    
                    data.holders = top100Custom.map((holder, index) => ({
                        address: holder.address || `CustomHolder${index + 1}`,
                        amount: holder.amount || 0,
                        amountDisplay: holder.amountDisplay || '0',
                        estimatedEarnings: data.perHolder,
                        usdDisplay: `$${data.perHolder.toFixed(2)} USDC 💵`,
                        isPool: (holder.name && holder.name.toLowerCase().includes('pool')) || false
                    }));
                }
            } catch (customErr) {
                console.warn(`Custom holders fetch failed for ${tokenMint}:`, customErr.message);
            }
        }

        if (!Array.isArray(data.holders)) {
            console.error('API response holders is not an array; forcing fallback.');
            data.holders = [];
        }

        res.status(200).json(data);

    } catch (err) {
        console.error("Error in /api/top-holders endpoint:", err);
        res.status(500).json({ 
            error: 'Failed to fetch top holders data.', 
            holders: [],
            totalPool: 0 
        });
    }
});

// --- BACKGROUND QUEST & DATA ENGINE ---
async function getLatestTradesFromApi(tokenMint) {
    const JUPITER_API_URL = `https://datapi.jup.ag/v1/txs/${tokenMint}?limit=100`;
    try {
        const response = await fetch(JUPITER_API_URL);
        if (!response.ok) {
            throw new Error(`Jupiter Data API failed with status ${response.status}`);
        }
        const data = await response.json();
        const trades = data.txs || [];
        return trades.map(trade => ({
            signature: trade.txHash,
            timestamp: trade.timestamp,
            tokenMint: trade.asset,
            traderAddress: trade.traderAddress,
            solVolume: trade.nativeVolume,
            usdVolume: trade.usdVolume,
            type: trade.type,
        }));
    } catch (error) {
        console.error(`Error fetching trades from Jupiter for ${tokenMint}:`, error.message);
        return [];
    }
}

async function processNewTradesForQuests(newTrades) {
    if (!newTrades || newTrades.length === 0) return;
    console.log(`⚙️ Processing ${newTrades.length} new trade(s) for quests...`);

    for (const trade of newTrades) {
        broadcastTrade(trade);
    }

    const tradesByTrader = newTrades.reduce((acc, trade) => {
        if (trade.traderAddress) {
            if (!acc[trade.traderAddress]) {
                acc[trade.traderAddress] = [];
            }
            acc[trade.traderAddress].push(trade);
        }
        return acc;
    }, {});

    for (const traderAddress in tradesByTrader) {
        let walletProfile = db.get('wallets').find({ address: traderAddress });
        if (!walletProfile.value()) {
            db.get('wallets').push({
                address: traderAddress,
                points: 0,
                totalVolumeSol: 0,
                completedQuests: [],
                profitableFlips: 0,
                deployedCount: 0,
                successfulLaunches: 0,
                snipeCount: 0,
                profitableFlipStreak: 0,
            }).write();
            walletProfile = db.get('wallets').find({ address: traderAddress });
        }
        const userQuests = walletProfile.value().completedQuests || [];
        const completeQuest = (questId) => {
            const quest = masterQuests.find(q => q.id === questId);
            if (quest && !userQuests.includes(questId)) {
                walletProfile.get('completedQuests').push(questId).write();
                walletProfile.update('points', p => (p || 0) + quest.points).write();
                console.log(`🎉 Quest Complete! ${traderAddress.slice(0, 6)} unlocked '${quest.title}'!`);
                userQuests.push(questId);
            }
        };

        const tradesForThisUser = tradesByTrader[traderAddress];
        const newVolume = tradesForThisUser.reduce((sum, t) => sum + (t.solVolume || 0), 0);
        const newTotalVolume = (walletProfile.value().totalVolumeSol || 0) + newVolume;
        walletProfile.assign({ totalVolumeSol: newTotalVolume }).write();

        // ── $EGG: +10 per trade ──
        addEggPoints(traderAddress, EGG_PER_TRADE * tradesForThisUser.length);

        completeQuest('FIRST_STEPS');

        if (newTotalVolume >= 10) completeQuest('APPRENTICE_TRADER');
        if (newTotalVolume >= 100) completeQuest('JOURNEYMAN_TRADER');
        if (newTotalVolume >= 1000) completeQuest('MARKET_MAKER');
        if (newTotalVolume >= 5000) completeQuest('KINGPIN_TRADER');
        if (newTotalVolume >= 15000) completeQuest('TYCOON');

        const allUserTrades = db.get('trades').filter({ traderAddress }).value();
        const uniqueTokensTraded = new Set(allUserTrades.map(t => t.tokenMint));
        if (uniqueTokensTraded.size >= 5) completeQuest('THE_REGULAR');
        if (uniqueTokensTraded.size >= 25) completeQuest('DIVERSIFIER');

        for (const trade of tradesForThisUser) {
            if (trade.solVolume >= 25) completeQuest('WHALE_TRADE');

            if (trade.type === 'buy') {
                const token = db.get('tokens').find({ baseMint: trade.tokenMint }).value();
                if (!token) continue;

                const uniqueBuyers = [...new Set(db.get('trades').filter({ tokenMint: trade.tokenMint, type: 'buy' }).map('traderAddress').value())];
                if (uniqueBuyers.length <= 10) completeQuest('PIONEER_TRADER');

                const launchTime = new Date(token.createdAt).getTime();
                const tradeTime = new Date(trade.timestamp).getTime();
                if (tradeTime - launchTime <= 30000) {
                    completeQuest('SNIPER');
                    addEggPoints(traderAddress, EGG_SNIPER_BONUS); // +40 sniper bonus
                    const newSnipeCount = (walletProfile.value().snipeCount || 0) + 1;
                    walletProfile.assign({ snipeCount: newSnipeCount }).write();
                    if (newSnipeCount >= 5) completeQuest('ALPHA_SNIPER');
                }
            }
        }
        
        for (const trade of tradesForThisUser.filter(t => t.type === 'sell')) {
            const userTradesForToken = allUserTrades.filter(t => t.tokenMint === trade.tokenMint);
            const buys = userTradesForToken.filter(t => t.type === 'buy');
            
            if (buys.length > 0) {
                const totalBuyVolume = buys.reduce((sum, b) => sum + b.solVolume, 0);
                const avgBuyVolume = totalBuyVolume / buys.length;
                const isProfitable = trade.solVolume > avgBuyVolume;

                if (isProfitable) {
                    const newFlipCount = (walletProfile.value().profitableFlips || 0) + 1;
                    const newStreak = (walletProfile.value().profitableFlipStreak || 0) + 1;
                    walletProfile.assign({ profitableFlips: newFlipCount, profitableFlipStreak: newStreak }).write();

                    if (newFlipCount >= 1) completeQuest('FLIPPER');
                    if (newFlipCount >= 10) completeQuest('MASTER_FLIPPER');
                    if (newStreak >= 5) completeQuest('HOT_STREAK');
                    
                    if (trade.solVolume >= avgBuyVolume * 10) {
                        completeQuest('GIGA_FLIP');
                    }
                } else {
                    walletProfile.assign({ profitableFlipStreak: 0 }).write();
                }
            }
        }
    }
}

async function checkMarketCapQuests() {
    console.log('📈 Checking Market Cap Quests...');
    const tokens = db.get('tokens').filter({ migrated: false }).value();
    
    for (const token of tokens) {
        if (token.moonboyAwarded && token.unicornHunterAwarded) continue;
        
        try {
            const jupRes = await fetch(`https://datapi.jup.ag/v1/assets/search?query=${token.baseMint}`);
            if (!jupRes.ok) continue;
            
            const data = await jupRes.json();
            const jupToken = data?.[0];
            if (!jupToken?.mcap) continue;
            
            const mcap = jupToken.mcap;
            
            if (mcap >= 100000 && !token.moonboyAwarded) {
                const quest = masterQuests.find(q => q.id === 'MOON_BOY');
                if (!quest) continue;
                
                const buyers = [...new Set(db.get('trades')
                    .filter({ tokenMint: token.baseMint, type: 'buy' })
                    .map('traderAddress')
                    .value())];
                
                for (const buyerAddress of buyers) {
                    const walletProfile = db.get('wallets').find({ address: buyerAddress });
                    if (walletProfile.value() && !walletProfile.value().completedQuests.includes('MOON_BOY')) {
                        walletProfile.get('completedQuests').push('MOON_BOY').write();
                        walletProfile.update('points', p => (p || 0) + quest.points).write();
                        console.log(`🎉 Quest Complete! ${buyerAddress.slice(0,6)} unlocked '${quest.title}'!`);
                    }
                }
                db.get('tokens').find({ baseMint: token.baseMint }).assign({ moonboyAwarded: true }).write();
            }
            
            if (mcap >= 1000000 && !token.unicornHunterAwarded) {
                const quest = masterQuests.find(q => q.id === 'UNICORN_HUNTER');
                if (!quest) continue;
                
                const buyers = [...new Set(db.get('trades')
                    .filter({ tokenMint: token.baseMint, type: 'buy' })
                    .map('traderAddress')
                    .value())];
                
                for (const buyerAddress of buyers) {
                    const walletProfile = db.get('wallets').find({ address: buyerAddress });
                    if (walletProfile.value() && !walletProfile.value().completedQuests.includes('UNICORN_HUNTER')) {
                        walletProfile.get('completedQuests').push('UNICORN_HUNTER').write();
                        walletProfile.update('points', p => (p || 0) + quest.points).write();
                        console.log(`🎉 Quest Complete! ${buyerAddress.slice(0,6)} unlocked '${quest.title}'!`);
                    }
                }
                db.get('tokens').find({ baseMint: token.baseMint }).assign({ unicornHunterAwarded: true }).write();
            }

        } catch (error) {
            console.error(`Error checking MCap quests for ${token.symbol}:`, error.message);
        }
    }
}

async function checkLeaderboardQuests() {
    console.log(' Leaderboard Quests Check...');
    try {
        const quest = masterQuests.find(q => q.id === 'TOP_TEN_TRADER');
        if (!quest) return;

        const topTenWallets = db.get('wallets')
            .filter(w => w.points > 0)
            .orderBy('points', 'desc')
            .take(10)
            .map('address')
            .value();

        for (const walletAddress of topTenWallets) {
            const walletProfile = db.get('wallets').find({ address: walletAddress });
            if (walletProfile.value() && !walletProfile.value().completedQuests.includes('TOP_TEN_TRADER')) {
                walletProfile.get('completedQuests').push('TOP_TEN_TRADER').write();
                walletProfile.update('points', p => (p || 0) + quest.points).write();
                console.log(`🎉 Quest Complete! ${walletAddress.slice(0,6)} unlocked '${quest.title}'!`);
            }
        }
    } catch (error) {
        console.error("Error checking leaderboard quests:", error.message);
    }
}

app.get('/backup-4c47403e-6294-4192-8a66-aaacb94085f1/db.json', (req, res) => {
    console.log('✅ Initiating database backup download...');
    const dbPath = process.env.RENDER ? '/data/db.json' : 'db.json';

    res.download(dbPath, 'db.json', (err) => {
        if (err) {
            console.error('❌ Error sending backup file:', err);
            if (!res.headersSent) {
                res.status(500).send('Error: Could not download the database file.');
            }
        } else {
            console.log('✅ Backup file sent successfully.');
        }
    });
});

setInterval(checkMarketCapQuests, 5 * 60 * 1000);
setInterval(checkLeaderboardQuests, 60 * 60 * 1000);

async function updateDataEngine() {
    console.log('⚙️  Running data engine cycle...');
    try {
        const allPlatformTokens = db.get('tokens').value();
        for (const token of allPlatformTokens) {
            const latestTrades = await getLatestTradesFromApi(token.baseMint);
            if (latestTrades.length === 0) {
                continue;
            }
            const existingSignatures = new Set(db.get('trades').map('signature').value());
            const newUniqueTrades = latestTrades.filter(trade => !existingSignatures.has(trade.signature));
            if (newUniqueTrades.length > 0) {
                db.get('trades').push(...newUniqueTrades).write();
                await processNewTradesForQuests(newUniqueTrades);
            }
        }
    } catch (error) {
        console.error("Error during data engine cycle:", error);
    }
}
setInterval(updateDataEngine, 30 * 1000);

async function autoMigratePools() {
    console.log('🔄 Checking for graduated pools...');
    try {
        const tokensToCheck = db.get('tokens').filter({ migrated: false }).value();

        for (const token of tokensToCheck) {
            if (!token.pool) continue;

            const virtualPool = new PublicKey(token.pool);
            
            // Use quote-token curve progress (returns 0.0–1.0, reaches 1.0 at graduation)
            const progress = await client.state.getPoolQuoteTokenCurveProgress(virtualPool);
            console.log(`Pool ${token.symbol} (${token.pool.slice(0,4)}...) progress: ${(progress * 100).toFixed(2)}%`);

            if (progress >= 1) {
                console.log(`✅ Marking token ${token.symbol} as graduated!`);
                db.get('tokens')
                  .find({ baseMint: token.baseMint })
                  .assign({ migrated: true, migratedAt: new Date().toISOString() })
                  .write();
            }
        }
    } catch (error) {
        console.error('Auto-migration check error:', error.message);
    }
}

setInterval(autoMigratePools, 30 * 1000);

function broadcast(token, message) {
    for (const [client, room] of clientRooms.entries()) {
        if (room === token && client.readyState === client.OPEN) {
            client.send(JSON.stringify(message));
        }
    }
}

app.post('/quote-fees', async (req, res) => {
    const { poolAddresses } = req.body;
    if (!poolAddresses || !Array.isArray(poolAddresses)) {
        return res.status(400).json({ error: 'poolAddresses must be an array.' });
    }
    const FEE_QUOTE_URL = 'https://studio-api.jup.ag/dbc/fee';
    const feeQuotes = {};
    for (const poolAddress of poolAddresses) {
        try {
            const response = await fetch(FEE_QUOTE_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ poolAddress }),
            });
            if (response.ok) {
                const quote = await response.json();
                feeQuotes[poolAddress] = Number(quote.unclaimed ?? 0);
            } else {
                feeQuotes[poolAddress] = 0;
            }
        } catch (error) {
            console.error(`Failed to fetch fee for pool ${poolAddress}:`, error);
            feeQuotes[poolAddress] = 0;
        }
    }
    res.status(200).json(feeQuotes);
});

app.post('/claim-fees', async (req, res) => {
    const { poolAddress, ownerWallet } = req.body;
    console.log(`[CLAIM-FEES] Received request for pool: ${poolAddress} by owner: ${ownerWallet}`);
    if (!poolAddress || !ownerWallet) {
        return res.status(400).json({ error: 'Missing poolAddress or ownerWallet' });
    }
    const FEE_QUOTE_URL = 'https://studio-api.jup.ag/dbc/fee';
    const CREATE_TX_URL = 'https://studio-api.jup.ag/dbc/fee/create-tx';
    try {
        const quoteResponse = await fetch(FEE_QUOTE_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ poolAddress }),
        });
        if (!quoteResponse.ok) {
            const errorText = await quoteResponse.text();
            throw new Error(`Failed to get fee quote from Jupiter API: ${errorText}`);
        }
        const quote = await quoteResponse.json();
        const unclaimedAmount = Number(quote.unclaimed ?? 0);
        if (!unclaimedAmount || unclaimedAmount <= 0) {
            return res.status(400).json({ error: 'There are no fees to claim at this time.' });
        }
        const createTxResponse = await fetch(CREATE_TX_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                poolAddress,
                ownerWallet,
                maxQuoteAmount: unclaimedAmount,
            }),
        });
        if (!createTxResponse.ok) {
            const errorText = await createTxResponse.text();
            throw new Error(`Failed to create claim transaction via Jupiter API: ${errorText}`);
        }
        const txData = await createTxResponse.json();
        const base64Transaction = txData.transaction || txData.tx;
        if (!base64Transaction) {
            throw new Error('No transaction was returned from the Jupiter API.');
        }
        res.status(200).json({ transaction: base64Transaction });
    } catch (err) {
        console.error("Error in /claim-fees endpoint:", err);
        res.status(500).json({ error: err.message });
    }
});

const pick = (obj, path, fallback = null) => {
    try {
        return path.split('.').reduce((o, k) => (o && o[k] !== undefined ? o[k] : undefined), obj) ?? fallback;
    } catch {
        return fallback;
    }
};

async function enrichWithJupiterData(token) {
    const defaultImage = 'https://arweave.net/WCM5h_34E8m3y_k-h1i59Q_P5I54k-H2d_s4b-C3xZM';
    const now = Date.now();
    const cacheKey = token.baseMint;
    const cachedItem = jupiterCache.get(cacheKey);
    if (cachedItem && (now - cachedItem.timestamp < CACHE_DURATION_MS)) {
        return { ...token, ...cachedItem.data };
    }
    try {
        const jupRes = await fetch(`https://datapi.jup.ag/v1/assets/search?query=${token.baseMint}`);
        if (!jupRes.ok) throw new Error('Failed to fetch Jupiter data');
        const arr = await jupRes.json();
        const j = arr?.[0];
        let enrichedData = {};
        if (j) {
            enrichedData = {
                id: j.id || token.baseMint,
                name: j.name ?? token.name,
                symbol: j.symbol ?? token.symbol,
                imageUrl: j.icon || token.imageUrl || defaultImage,
                website: (j.extensions?.website) || token.website,
                twitter: (j.extensions?.twitter) || token.twitter,
                telegram: (j.extensions?.telegram) || token.telegram,
                createdAt: j.createdAt ?? token.createdAt ?? null,
                usdPrice: j.usdPrice ?? null,
                mcap: j.mcap ?? 0,
                liquidity: j.liquidity ?? 0,
                holderCount: j.holderCount ?? 0,
                stats24h: j.stats24h || null,
                audit: j.audit || {},
            };
        } else {
            enrichedData = { imageUrl: defaultImage, mcap: 0, liquidity: 0, holderCount: 0, stats24h: null, audit: {} };
        }
        jupiterCache.set(cacheKey, { data: enrichedData, timestamp: now });
        return { ...token, ...enrichedData };
    } catch (err) {
        console.error(`Error enriching token ${token.baseMint}: ${err.message}`);
        if (cachedItem) {
            return { ...token, ...cachedItem.data };
        }
        return { ...token, imageUrl: defaultImage, mcap: 0, liquidity: 0, holderCount: 0, stats24h: null };
    }
}

app.get('/api/quests', (req, res) => {
    res.json(masterQuests);
});

app.get('/api/wallet/:address', (req, res) => {
    const { address } = req.params;
    const walletProfile = db.get('wallets').find({ address }).value();
    if (walletProfile) {
        res.json(walletProfile);
    } else {
        res.json({
            address: address,
            points: 0,
            totalVolumeSol: 0,
            completedQuests: []
        });
    }
});

app.get('/token/:mint', async (req, res) => {
    const { mint } = req.params;
    
    const token = db.get('tokens').find({ baseMint: mint }).value();
    
    const templatePath = path.join(__dirname, 'public', 'token.html');
    
    if (!fs.existsSync(templatePath)) {
        return res.redirect(`/?token=${mint}`);
    }
    
    let html = fs.readFileSync(templatePath, 'utf-8');
    
    const tokenName = token?.name || 'Unknown Token';
    const tokenSymbol = token?.symbol || '???';
    const tokenDescription = token?.description || 'Trade this token on orby.fun';
    const tokenImage = token?.imageUrl || 'https://arweave.net/WCM5h_34E8m3y_k-h1i59Q_P5I54k-H2d_s4b-C3xZM';
    
    html = html
        .replace(/\{\{TOKEN_MINT\}\}/g, mint)
        .replace(/\{\{TOKEN_NAME\}\}/g, tokenName)
        .replace(/\{\{TOKEN_SYMBOL\}\}/g, tokenSymbol)
        .replace(/\{\{TOKEN_DESCRIPTION\}\}/g, tokenDescription.substring(0, 160))
        .replace(/\{\{TOKEN_IMAGE\}\}/g, tokenImage);
    
    res.send(html);
});

app.get('/all-tokens', async (req, res) => {
    try {
        const tokens = db.get('tokens').value();

        const FORBIDDEN_WORDS = ['jeffy', 'jeff yu', 'jeffyu', 'jeffy yu'];
        
        const filteredTokens = tokens.filter(t => {
            const lowerName = (t.name || '').toLowerCase();
            const lowerSymbol = (t.symbol || '').toLowerCase();
            return !FORBIDDEN_WORDS.some(word => 
                lowerName.includes(word) || lowerSymbol.includes(word)
            );
        });

        let enrichedTokens = await Promise.all(filteredTokens.map(enrichWithJupiterData));
        enrichedTokens.sort((a, b) => (new Date(b.createdAt) || 0) - (new Date(a.createdAt) || 0));
        res.status(200).json(enrichedTokens);
    } catch (err) {
        console.error("Error in /all-tokens:", err);
        res.status(500).json({ error: err.message });
    }
});

app.get('/tokens', async (req, res) => {
    const { deployer } = req.query;
    if (!deployer) return res.status(400).send({ error: 'Deployer query parameter is required.' });
    try {
        const tokens = db.get('tokens').filter({ deployer }).value();
        let enrichedTokens = await Promise.all(tokens.map(enrichWithJupiterData));
        enrichedTokens.sort((a, b) => (new Date(b.createdAt) || 0) - (new Date(a.createdAt) || 0));
        res.send(enrichedTokens);
    } catch (err) {
        console.error("Error in /tokens:", err);
        res.status(500).send({ error: "Failed to fetch tokens." });
    }
});

// --- JUPITER ULTRA API PROXY ---
const JUPITER_API_KEY = process.env.JUPITER_API_KEY || '';

app.get('/api/jupiter/quote', async (req, res) => {
    try {
        const { inputMint, outputMint, amount, taker } = req.query;
        
        if (!inputMint || !outputMint || !amount) {
            return res.status(400).json({ error: 'Missing required params: inputMint, outputMint, amount' });
        }
        
        let url = `https://api.jup.ag/ultra/v1/order?inputMint=${inputMint}&outputMint=${outputMint}&amount=${amount}`;
        if (taker) url += `&taker=${taker}`;
        
        const response = await fetch(url, {
            headers: {
                'x-api-key': JUPITER_API_KEY
            }
        });
        
        if (!response.ok) {
            const errorText = await response.text();
            console.error(`Jupiter quote error: ${response.status} - ${errorText}`);
            return res.status(response.status).json({ error: `Jupiter API error: ${response.status}` });
        }
        
        const data = await response.json();
        res.json(data);
        
    } catch (err) {
        console.error('Jupiter quote proxy error:', err);
        res.status(500).json({ error: err.message });
    }
});

app.post('/api/jupiter/execute', async (req, res) => {
    try {
        const { signedTransaction, requestId } = req.body;
        
        if (!signedTransaction || !requestId) {
            return res.status(400).json({ error: 'Missing signedTransaction or requestId' });
        }
        
        const response = await fetch('https://api.jup.ag/ultra/v1/execute', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'x-api-key': JUPITER_API_KEY
            },
            body: JSON.stringify({
                signedTransaction,
                requestId
            })
        });
        
        const data = await response.json();
        
        if (!response.ok) {
            console.error(`Jupiter execute error: ${response.status}`, data);
            return res.status(response.status).json(data);
        }
        
        if (data.status === 'Success' && data.swapEvents) {
            console.log(`✅ Jupiter swap executed: ${data.signature}`);
        }
        
        res.json(data);
        
    } catch (err) {
        console.error('Jupiter execute proxy error:', err);
        res.status(500).json({ error: err.message });
    }
});


// GET egg points for a wallet — returns `points` (the unified accrual field)
// Also returns holder info if the wallet holds $EGG tokens
app.get('/api/egg-points/:walletAddress', async (req, res) => {
    const { walletAddress } = req.params;
    try {
        const walletProfile = db.get('wallets').find({ address: walletAddress }).value();
        // `points` is the unified field — accrued by quests, trades, comments, launches
        const eggPoints  = walletProfile?.eggPoints || 0;  // claimable trader balance
        const lastClaim  = walletProfile?.lastEggClaim || 0;

        // Also check if this wallet is a top-100 $EGG holder and compute holder reward
        let holderRewardSol = 0;
        let holderRewardUsd = 0;
        let holderRank = null;
        try {
            const EGG_MINT_ADDR = 'Fkzmbyfjt8boVu8fuP7m3keLr9Un42yfKgYhUCcxEGG';
            const holdersRes = await fetch(`https://datapi.jup.ag/v1/holders/${EGG_MINT_ADDR}`);
            if (holdersRes.ok) {
                const holdersData = await holdersRes.json();
                const rawHolders = holdersData?.holders || [];
                const top100 = rawHolders.slice(0, 100);
                const totalHeld = top100.reduce((s, h) => s + (h.amount || 0), 0);
                const myEntry = top100.find(h => h.address === walletAddress);
                if (myEntry && totalHeld > 0) {
                    holderRank = top100.indexOf(myEntry) + 1;
                    // Fetch platform stats to compute pool share
                    const statsRes = await fetch(`http://127.0.0.1:${process.env.PORT || 3000}/platform-stats`);
                    if (statsRes.ok) {
                        const stats = await statsRes.json();
                        const platformEarnings = stats.platformEarnings || 0;
                        const holdersPool = platformEarnings * 0.70 * 0.40;
                        const pct = (myEntry.amount || 0) / totalHeld;
                        holderRewardUsd = holdersPool * pct;
                        const solPrice = walletProfile?.cachedSolPrice || 130;
                        holderRewardSol = holderRewardUsd / solPrice;
                    }
                }
            }
        } catch (e) { /* non-fatal */ }

        // Compute holder delta reward (what they'd get if they claimed now)
        let holderDeltaPool = 0;
        if (holderRewardUsd > 0 && walletProfile) {
            const snapshotAtLastClaim = walletProfile.holderPoolSnapshotAtClaim || 0;
            // Re-use the holdersPool from above calculation
            holderDeltaPool = Math.max(0, (holderRewardUsd / (walletProfile.holderPct || 1)) - snapshotAtLastClaim);
        }
        const lastHolderClaim = walletProfile?.lastHolderClaim || 0;

        res.json({ eggPoints, lastClaim, lastHolderClaim, holderRewardSol, holderRewardUsd, holderDeltaPool, holderRank });
    } catch (err) {
        console.error('Error fetching egg points:', err);
        res.status(500).json({ error: 'Failed to fetch egg points.' });
    }
});

// ── Helper: send SOL from WALLET_SECRET to a recipient ──────────────────────
async function sendSolReward(recipientAddress, solAmount) {
    const lamports = Math.floor(solAmount * LAMPORTS_PER_SOL);
    if (lamports <= 0) throw new Error('SOL amount too small to send.');

    const platformBalance = await connection.getBalance(wallet.publicKey);
    if (platformBalance < lamports + 5000) {
        throw new Error('Reward pool temporarily low. Try again soon.');
    }

    const recipientPubkey = new PublicKey(recipientAddress);
    const tx = new Transaction().add(
        SystemProgram.transfer({
            fromPubkey: wallet.publicKey,   // ← WALLET_SECRET
            toPubkey:   recipientPubkey,
            lamports
        })
    );
    tx.feePayer        = wallet.publicKey;
    tx.recentBlockhash = (await connection.getLatestBlockhash('confirmed')).blockhash;
    tx.sign(wallet);

    const signature = await connection.sendRawTransaction(tx.serialize(), {
        skipPreflight: false,
        maxRetries: 3
    });
    await connection.confirmTransaction(signature, 'confirmed');
    return signature;
}

// POST /api/egg-claim  — Trader reward claim
// Reads `points` from DB (the unified accrual field shown on leaderboard),
// sends SOL from WALLET_SECRET, resets points to 0.
app.post('/api/egg-claim', async (req, res) => {
    const { wallet: walletAddress } = req.body;
    if (!walletAddress) return res.status(400).json({ error: 'Wallet address required.' });

    try {
        let walletProfile = db.get('wallets').find({ address: walletAddress });
        if (!walletProfile.value()) {
            return res.status(400).json({ error: 'No $EGG balance found. Trade or comment first!' });
        }

        const currentEgg = walletProfile.value().eggPoints || 0;  // claimable trader balance (separate from leaderboard points)
        const lastClaim  = walletProfile.value().lastEggClaim || 0;
        const now        = Date.now();

        if (now - lastClaim < EGG_CLAIM_COOLDOWN) {
            const remaining = Math.ceil((EGG_CLAIM_COOLDOWN - (now - lastClaim)) / 1000);
            return res.status(429).json({ error: `Cooldown active. Try again in ${remaining}s.` });
        }
        if (currentEgg < EGG_MIN_CLAIM) {
            return res.status(400).json({
                error: `Need at least ${EGG_MIN_CLAIM.toLocaleString()} $EGG to claim. You have ${currentEgg.toLocaleString()}.`
            });
        }

        // ── LOCK: zero out eggPoints and set cooldown BEFORE sending SOL ────
        // Prevents double-claim from concurrent requests (two tabs, double-click).
        // If sendSolReward fails, the points are already consumed — this is intentional
        // to prevent abuse. Platform absorbs the rare TX failure cost.
        walletProfile.assign({ eggPoints: 0, points: 0, lastEggClaim: now }).write();

        const solAmount = parseFloat((currentEgg * EGG_TO_SOL_RATE).toFixed(6));
        const signature = await sendSolReward(walletAddress, solAmount);

        console.log(`✅ Trader claim: ${walletAddress.slice(0,6)}… ${solAmount} SOL (${currentEgg} $EGG burned). Tx: ${signature}`);
        res.json({ success: true, signature, solAmount, eggBurned: currentEgg });

    } catch (err) {
        console.error('Egg claim error:', err);
        res.status(500).json({ error: err.message || 'Claim failed.' });
    }
});

// POST /api/holder-claim  — Holder reward claim
//
// HOW IT WORKS (snapshot delta model — prevents double-claiming):
//
//   The holders pool grows as the platform earns fees over time.
//   Each holder's DB record stores `holderPoolSnapshotAtClaim` — the total
//   holdersPool value at the moment of their last claim.
//
//   On each claim we calculate:
//     currentHoldersPool = platformEarnings * 0.70 * 0.40
//     deltaPool = currentHoldersPool - holderPoolSnapshotAtClaim
//     myReward  = deltaPool * myPct
//
//   After claiming: holderPoolSnapshotAtClaim = currentHoldersPool
//   → Next claim only pays for NEW platform earnings since last claim
//   → 10-min cooldown still applies on top
//
app.post('/api/holder-claim', async (req, res) => {
    const { wallet: walletAddress } = req.body;
    if (!walletAddress) return res.status(400).json({ error: 'Wallet address required.' });

    try {
        let walletProfile = db.get('wallets').find({ address: walletAddress });
        if (!walletProfile.value()) {
            db.get('wallets').push({
                address: walletAddress, points: 0, eggPoints: 0, totalVolumeSol: 0,
                completedQuests: [], profitableFlips: 0, deployedCount: 0,
                snipeCount: 0, profitableFlipStreak: 0,
                lastEggClaim: 0, lastHolderClaim: 0,
                holderPoolSnapshotAtClaim: 0   // tracks pool total at last claim
            }).write();
            walletProfile = db.get('wallets').find({ address: walletAddress });
        }

        // ── 10-minute cooldown check ────────────────────────────────────────
        const lastHolderClaim = walletProfile.value().lastHolderClaim || 0;
        const now = Date.now();
        if (now - lastHolderClaim < EGG_CLAIM_COOLDOWN) {
            const remaining = Math.ceil((EGG_CLAIM_COOLDOWN - (now - lastHolderClaim)) / 1000);
            return res.status(429).json({ error: `Holder cooldown active. Try again in ${remaining}s.` });
        }

        // ── LOCK: write cooldown to DB IMMEDIATELY before any async work ────
        // This prevents double-claims from concurrent requests (two tabs, double-click, retry).
        // Any second request hitting this endpoint now will see the cooldown and be rejected.
        walletProfile.assign({ lastHolderClaim: now }).write();

        try {
        // ── Verify wallet is in top-100 $EGG holders ───────────────────────
        const EGG_MINT_ADDR = 'Fkzmbyfjt8boVu8fuP7m3keLr9Un42yfKgYhUCcxEGG';
        const holdersRes = await fetch(`https://datapi.jup.ag/v1/holders/${EGG_MINT_ADDR}`);
        if (!holdersRes.ok) throw new Error('Could not fetch holder data. Try again.');

        const holdersData = await holdersRes.json();
        const rawHolders  = holdersData?.holders || [];
        const top100      = rawHolders.slice(0, 100);
        const myEntry     = top100.find(h => h.address === walletAddress);

        if (!myEntry) {
            // Not a holder — roll back the lock so they can try again if they fix their wallet
            walletProfile.assign({ lastHolderClaim: lastHolderClaim }).write();
            return res.status(400).json({
                error: 'Your wallet is not in the top 100 $EGG holders. Hold more $EGG to qualify!'
            });
        }

        const totalHeld = top100.reduce((s, h) => s + (h.amount || 0), 0);
        if (totalHeld === 0) {
            walletProfile.assign({ lastHolderClaim: lastHolderClaim }).write();
            return res.status(400).json({ error: 'No holder data available.' });
        }

        // ── Compute current pool and delta since last claim ─────────────────
        const statsRes = await fetch(`http://127.0.0.1:${process.env.PORT || 3000}/platform-stats`);
        if (!statsRes.ok) throw new Error('Could not fetch platform stats.');
        const stats = await statsRes.json();

        const platformEarnings       = stats.platformEarnings || 0;
        const currentHoldersPool     = platformEarnings * 0.70 * 0.40;
        const snapshotAtLastClaim    = walletProfile.value().holderPoolSnapshotAtClaim || 0;

        // Only pay the DELTA — new pool earnings since last claim
        const deltaPool = Math.max(0, currentHoldersPool - snapshotAtLastClaim);
        const myPct     = (myEntry.amount || 0) / totalHeld;
        const rewardUsd = deltaPool * myPct;

        if (rewardUsd <= 0) {
            // No new earnings — roll back the lock so cooldown doesn't penalise them
            walletProfile.assign({ lastHolderClaim: lastHolderClaim }).write();
            return res.status(400).json({
                error: 'No new holder rewards since your last claim. Wait for more platform volume!'
            });
        }

        // ── Convert USD → SOL ───────────────────────────────────────────────
        let solPrice = 130;
        try {
            const priceRes = await fetch('https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT');
            const pd = await priceRes.json();
            solPrice = parseFloat(pd?.price) || 130;
        } catch(e) {}

        const solAmount = parseFloat((rewardUsd / solPrice).toFixed(6));
        if (solAmount < 0.000001) {
            walletProfile.assign({ lastHolderClaim: lastHolderClaim }).write();
            return res.status(400).json({
                error: `Reward too small to send ($${rewardUsd.toFixed(6)}). More platform volume needed!`
            });
        }

        // ── Send SOL from WALLET_SECRET ─────────────────────────────────────
        const signature = await sendSolReward(walletAddress, solAmount);

        // ── Advance snapshot — next claim only pays NEW earnings from here ───
        walletProfile.assign({
            lastHolderClaim: now,                          // confirm the lock timestamp
            holderPoolSnapshotAtClaim: currentHoldersPool  // delta resets to current pool
        }).write();

        console.log(`Holder claim: ${walletAddress.slice(0,6)} ${solAmount} SOL delta=${deltaPool.toFixed(4)} pool=${currentHoldersPool.toFixed(4)}`);
        res.json({ success: true, signature, solAmount, rewardUsd, holderPct: myPct * 100, deltaPool });

        } catch (innerErr) {
            // Roll back the lock on unexpected failure so user isn't stuck
            console.error('Holder claim inner error (rolling back):', innerErr);
            try { walletProfile.assign({ lastHolderClaim: lastHolderClaim }).write(); } catch(e){}
            if (!res.headersSent) res.status(500).json({ error: innerErr.message || 'Holder claim failed.' });
        }

    } catch (err) {
        console.error('Holder claim outer error:', err);
        if (!res.headersSent) res.status(500).json({ error: err.message || 'Holder claim failed.' });
    }
});

// --- SERVER START ---
const PORT = process.env.PORT || 3000;
server.listen(PORT, () => {
    console.log(`orbmemefun server running on http://localhost:${PORT}`);
});