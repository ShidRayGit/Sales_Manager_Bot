#!/usr/bin/env bash
set -euo pipefail

# ===== Repo settings =====
OWNER="ShidRayGit"
REPO="Sales_Manager_Bot"
BRANCH="main"
RAW_BASE="https://raw.githubusercontent.com/${OWNER}/${REPO}/${BRANCH}"
# =========================

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "Please run as root (or with sudo)."
    exit 1
  fi
}

slugify() {
  # Keep a-zA-Z0-9 and dash/underscore; convert spaces to dash; lowercase
  local s="${1:-bot}"
  s="${s// /-}"
  s="$(echo "$s" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9._-]//g')"
  echo "${s:-bot}"
}

install_docker_if_missing() {
  if command -v docker >/dev/null 2>&1; then
    return
  fi
  echo "Installing Docker and Compose plugin..."
  apt-get update -y
  apt-get install -y ca-certificates curl gnupg lsb-release
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

main() {
  require_root

  echo "=== Sales Manager Bot installer ==="
  read -rp "Bot instance name (e.g. prod, test, myshop): " BOT_NAME_IN
  read -rp "BOT_TOKEN: " BOT_TOKEN
  read -rp "ADMIN_CHAT_ID (comma-separated if multiple): " ADMIN_CHAT_ID
  read -rp "Time zone (default Asia/Tehran): " TZ_INPUT

  BOT_NAME="$(slugify "${BOT_NAME_IN:-bot}")"
  TZ_INPUT="${TZ_INPUT:-Asia/Tehran}"

  INSTALL_DIR="/opt/sales-manager-bot/${BOT_NAME}"
  CONTAINER_NAME="telegram-bot-${BOT_NAME}"
  PROJECT_NAME="${BOT_NAME}"  # compose project name (separates networks/volumes)

  echo "Instance:        ${BOT_NAME}"
  echo "Install dir:     ${INSTALL_DIR}"
  echo "Container name:  ${CONTAINER_NAME}"
  echo "Compose project: ${PROJECT_NAME}"
  echo

  install_docker_if_missing

  mkdir -p "${INSTALL_DIR}"
  cd "${INSTALL_DIR}"

  echo "Fetching files from GitHub..."
  curl -fsSLo telegram_subscription_bot.py "${RAW_BASE}/telegram_subscription_bot.py"
  curl -fsSLo Dockerfile "${RAW_BASE}/Dockerfile"

  # Compose is generated here to inject unique container name and volumes per instance
  cat > docker-compose.yml <<EOF
services:
  telegram-bot:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: ${CONTAINER_NAME}
    env_file:
      - .env
    environment:
      - TZ=\${TZ:-Asia/Tehran}
      - DB_PATH=/app/data/data.db
    volumes:
      - ./data:/app/data
    restart: unless-stopped
EOF

  cat > .env <<EOF
BOT_TOKEN=${BOT_TOKEN}
ADMIN_CHAT_ID=${ADMIN_CHAT_ID}
TZ=${TZ_INPUT}
DB_PATH=/app/data/data.db
BACKUP_SRC=/app
MAX_BACKUP_MB=45
EOF
  chmod 600 .env

  echo "Building and starting the container..."
  COMPOSE_PROJECT_NAME="${PROJECT_NAME}" docker compose build
  COMPOSE_PROJECT_NAME="${PROJECT_NAME}" docker compose up -d

  echo
  echo "=== Done ==="
  echo "Tail logs:      COMPOSE_PROJECT_NAME='${PROJECT_NAME}' docker compose logs -f"
  echo "Restart:        COMPOSE_PROJECT_NAME='${PROJECT_NAME}' docker compose restart"
  echo "Stop:           COMPOSE_PROJECT_NAME='${PROJECT_NAME}' docker compose down"
  echo "DB on host:     ${INSTALL_DIR}/data/data.db"
  echo
  echo "To install another bot on the same server, run this script again with a different Bot instance name."
}

main "$@"