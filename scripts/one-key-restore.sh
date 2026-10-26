#!/usr/bin/env bash
# ==============================================================================
# One-Key Disaster Recovery & Migration Script for Personal Website Fleet
# Target: Fresh Linux VPS (Ubuntu / Debian / Oracle Linux / Alma / Rocky)
# ==============================================================================
set -euo pipefail

# Text formatting
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

DOMAIN="${1:-lmlu.de}"
INSTALL_DIR="/var/www/personal-website"
REPO_URL="https://github.com/minglu-lin/personal-website.git"

echo -e "${BLUE}=====================================================================${NC}"
echo -e "${GREEN}  🎓 Personal Website & Cloud Services Disaster Recovery Deployer  ${NC}"
echo -e "${BLUE}=====================================================================${NC}"
echo -e "Primary Domain: ${YELLOW}${DOMAIN}${NC}"
echo -e "Target Directory: ${YELLOW}${INSTALL_DIR}${NC}\n"

# 1. Check Root Privileges
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}[!] Please run this script with sudo or as root.${NC}"
    exit 1
fi

# 2. Package Manager Detection & Dependency Installation
echo -e "${GREEN}[1/6] Installing runtime dependencies (Nginx, Docker, Git, Python3)...${NC}"
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    apt-get install -y git curl wget nginx python3 python3-pip docker.io docker-compose-v2
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y git curl wget nginx python3 python3-pip
    dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo || true
    dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin || true
    systemctl enable --now docker nginx
fi

systemctl enable --now docker
systemctl enable --now nginx

# 3. Pull or Clone Repository
echo -e "${GREEN}[2/6] Syncing personal-website repository...${NC}"
mkdir -p /var/www
if [ -d "$INSTALL_DIR/.git" ]; then
    echo -e "[*] Existing repository detected at $INSTALL_DIR. Pulling latest main..."
    cd "$INSTALL_DIR"
    git pull origin main
    git submodule update --init --recursive
else
    echo -e "[*] Cloning fresh repository to $INSTALL_DIR..."
    git clone --recurse-submodules "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# 4. Deploy Dockerized Applications (MyIP)
echo -e "${GREEN}[3/6] Starting Docker containerized services (MyIP)...${NC}"
cd "$INSTALL_DIR/apps/myip"
if [ ! -f .env ]; then
    cp .env.example .env
fi
docker compose up -d

# 5. Configure Nginx Reverse Proxy Matrix
echo -e "${GREEN}[4/6] Linking modular Nginx configurations...${NC}"
mkdir -p /etc/nginx/conf.d
cp "$INSTALL_DIR/deploy/nginx/conf.d/"*.conf /etc/nginx/conf.d/

# Ensure dummy self-signed cert exists if real cert is pending
mkdir -p /etc/s-box
if [ ! -f /etc/s-box/cert.pem ]; then
    echo -e "${YELLOW}[!] Warning: /etc/s-box/cert.pem not found. Generating temporary fallback cert...${NC}"
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout /etc/s-box/private.key \
        -out /etc/s-box/cert.pem \
        -subj "/CN=*.${DOMAIN}" 2>/dev/null
fi

echo -e "[*] Testing Nginx configuration..."
nginx -t
systemctl reload nginx

# 6. Deploy Native Systemd Background Services
echo -e "${GREEN}[5/6] Registering Systemd services...${NC}"
cp "$INSTALL_DIR/deploy/systemd/bin-lookup.service" /etc/systemd/system/ || true
cp "$INSTALL_DIR/deploy/systemd/fleet-status.service" /etc/systemd/system/ || true

systemctl daemon-reload
systemctl enable --now bin-lookup.service || true
systemctl enable --now fleet-status.service || true

# 7. Final Health & Status Verification
echo -e "${GREEN}[6/6] Verifying local services health...${NC}"
sleep 2

check_port() {
    local port=$1
    local name=$2
    if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$port" >/dev/null 2>&1; then
        echo -e "  [✓] ${name} (Port ${port}): ${GREEN}ONLINE${NC}"
    else
        echo -e "  [?] ${name} (Port ${port}): ${YELLOW}CHECKING / STARTING${NC}"
    fi
}

check_port 18966 "MyIP Toolbox"
check_port 18895 "Card BIN Lookup"
check_port 18889 "Fleet Telemetry Monitor"

echo -e "\n${BLUE}=====================================================================${NC}"
echo -e "${GREEN}  🎉 Disaster Recovery Deployment Complete! All Services Ready!   ${NC}"
echo -e "${BLUE}=====================================================================${NC}"
echo -e "Main Portal:       https://${DOMAIN}"
echo -e "MyIP Toolbox:      https://ip.${DOMAIN}"
echo -e "Card BIN Radar:    https://bin.${DOMAIN}"
echo -e "Fleet Status:      https://status-k9x2.${DOMAIN}"
echo -e "Cloud Webmail:     https://mail.${DOMAIN}"
echo -e "${BLUE}=====================================================================${NC}\n"
