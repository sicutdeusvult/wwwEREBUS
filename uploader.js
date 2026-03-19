/**
 * uploader.js — Upload vanity keypairs to Render /data/vanity
 *
 * Usage:
 *   node uploader.js
 *
 * Reads AGENT_SECRET from environment or the hardcoded fallback below.
 * Set it to whatever you have in your Render environment variables.
 */

require('dotenv').config();
const axios  = require('axios');
const fs     = require('fs');
const path   = require('path');
const FormData = require('form-data');

// ── CONFIGURATION ─────────────────────────────────────────────────────────────
const SERVER_URL       = process.env.LAUNCHPAD_URL
                           ? `${process.env.LAUNCHPAD_URL}/upload-file`
                           : 'https://neuroalon.onrender.com/upload-file';

const AGENT_SECRET     = process.env.AGENT_SECRET || 'juniordoomboi';
const LOCAL_VANITY_DIR = path.join(__dirname, 'vanity');
const REMOTE_TARGET    = '/data/vanity';
// ─────────────────────────────────────────────────────────────────────────────

async function uploadFile(filePath) {
    const fileName = path.basename(filePath);
    process.stdout.write(`🚀 Uploading ${fileName}... `);

    const form = new FormData();
    form.append('file',   fs.createReadStream(filePath), fileName);
    form.append('target', REMOTE_TARGET);

    try {
        const response = await axios.post(SERVER_URL, form, {
            headers: {
                ...form.getHeaders(),
                'x-agent-secret': AGENT_SECRET,   // auth header
            },
            timeout: 30000,
            maxContentLength: Infinity,
            maxBodyLength:    Infinity,
        });

        if (response.data.success) {
            console.log(`✅  → ${response.data.path}`);
            return true;
        } else {
            console.log(`❌  server error: ${response.data.error || 'unknown'}`);
            return false;
        }
    } catch (err) {
        if (err.response) {
            const status = err.response.status;
            const body   = JSON.stringify(err.response.data);
            if (status === 401) {
                console.log(`❌  401 Unauthorized — check AGENT_SECRET matches Render env var`);
            } else {
                console.log(`❌  HTTP ${status}: ${body}`);
            }
        } else {
            console.log(`❌  ${err.message}`);
        }
        return false;
    }
}

async function runUploader() {
    console.log('');
    console.log('╔══════════════════════════════════════╗');
    console.log('║   Vanity Keypair Uploader             ║');
    console.log('╚══════════════════════════════════════╝');
    console.log(`📡 Target: ${SERVER_URL}`);
    console.log(`📂 Local:  ${LOCAL_VANITY_DIR}`);
    console.log(`🔑 Secret: ${AGENT_SECRET.slice(0, 4)}${'*'.repeat(AGENT_SECRET.length - 4)}`);
    console.log('');

    if (!fs.existsSync(LOCAL_VANITY_DIR)) {
        console.error(`❌ Directory not found: '${LOCAL_VANITY_DIR}'`);
        console.error(`   Create it and add your vanity keypair .json files first.`);
        process.exit(1);
    }

    const jsonFiles = fs.readdirSync(LOCAL_VANITY_DIR)
        .filter(f => path.extname(f).toLowerCase() === '.json');

    if (jsonFiles.length === 0) {
        console.log('⚠️  No .json files found in vanity/ directory.');
        return;
    }

    console.log(`Found ${jsonFiles.length} keypair(s). Uploading...\n`);

    let ok = 0, fail = 0;
    for (const fileName of jsonFiles) {
        const success = await uploadFile(path.join(LOCAL_VANITY_DIR, fileName));
        success ? ok++ : fail++;
    }

    console.log('');
    console.log(`✅ Done: ${ok} uploaded, ${fail} failed`);
    if (fail > 0) process.exit(1);
}

runUploader();
