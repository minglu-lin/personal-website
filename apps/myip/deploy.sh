#!/usr/bin/env bash
# ==============================================================================
# MyIP Quick Deployment & Management Script
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f .env ]; then
    echo "[*] Creating default .env from .env.example..."
    cp .env.example .env
fi

echo "[*] Pulling and starting MyIP Docker container..."
docker compose up -d

echo "[✓] MyIP is running on http://127.0.0.1:18966"
