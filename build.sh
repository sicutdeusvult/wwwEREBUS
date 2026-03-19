#!/usr/bin/env bash
set -e
echo "==> Installing Python dependencies..."
pip install -r requirements.txt
echo "==> Build complete. Playwright browser will install on first run."
