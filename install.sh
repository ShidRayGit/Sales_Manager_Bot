#!/usr/bin/env bash
set -euo pipefail

# ===== Repo settings =====
OWNER="ShidRayGit"
REPO="Sales_Manager_Bot"
BRANCH="main"
RAW_BASE="https://raw.githubusercontent.com/${OWNER}/${REPO}/${BRANCH}"
BASE_DIR="/opt/sales-manager-bot"
# =========================

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "Please run as root (or with sudo)."
    exit 1
  fi
}

slugify() {
  local s="${1:-bot}"
  s="${s// /-}"
  s="$(echo "$s" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9._-]//g')"
  echo "${s:-bot}"
}

install_docker_if_missing() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
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

compose() {
  local proj="$1"; shift
  COMPOSE_PROJECT_NAME="$proj" docker compose "$@"
}

# raw list of bot instances
list_instances_raw() {
  [[ -d "${BASE_DIR}" ]] || return 0
  shopt -s nullglob
  for d in "${BASE_DIR}"/*; do
    [[ -d "$d" ]] && basename "$d"
  done
  shopt -u nullglob
}
# ---------- Actions ----------

install_instance() {
  echo "=== Install a new bot ==="
  read -rp "Bot instance name: " BOT_NAME_IN
  read -rp "BOT_TOKEN: " BOT_TOKEN
  read -rp "ADMIN_CHAT_ID (comma separated): " ADMIN_CHAT_ID
  read -rp "Time zone [Asia/Tehran]: " TZ_INPUT
  TZ_INPUT="${TZ_INPUT:-Asia/Tehran}"

  BOT_NAME="$(slugify "${BOT_NAME_IN:-bot}")"
  INSTALL_DIR="${BASE_DIR}/${BOT_NAME}"
  CONTAINER_NAME="telegram-bot-${BOT_NAME}"
  PROJECT_NAME="${BOT_NAME}"

  mkdir -p "${INSTALL_DIR}"
  cd "${INSTALL_DIR}"

  echo "Fetching bot files..."
  curl -fsSLo telegram_subscription_bot.py "${RAW_BASE}/telegram_subscription_bot.py"
  curl -fsSLo Dockerfile "${RAW_BASE}/Dockerfile"

  cat > docker-compose.yml <<EOF
services:
  telegram-bot:
    build: { context: ., dockerfile: Dockerfile }
    container_name: ${CONTAINER_NAME}
    env_file: [ .env ]
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

  compose "${PROJECT_NAME}" build
  compose "${PROJECT_NAME}" up -d
}

remove_instance() {
  echo "=== Remove a bot ==="
  mapfile -t INSTANCES < <(list_instances_raw)
  [[ ${#INSTANCES[@]} -eq 0 ]] && { echo "No bots found."; return; }

  echo "Select one to remove:"
  for i in "${!INSTANCES[@]}"; do
    printf " %d) %s\n" "$((i+1))" "${INSTANCES[$i]}"
  done
  read -rp "Number: " sel
  (( sel>=1 && sel<=${#INSTANCES[@]} )) || { echo "Invalid."; return; }
  BOT_NAME="${INSTANCES[$((sel-1))]}"
  INSTALL_DIR="${BASE_DIR}/${BOT_NAME}"
  PROJECT_NAME="${BOT_NAME}"
  CONTAINER_NAME="telegram-bot-${BOT_NAME}"

  read -rp "Confirm delete ${BOT_NAME}? (y/N): " ok
  [[ "${ok,,}" == "y" ]] || [[ "${ok,,}" == "yes" ]] || { echo "Aborted."; return; }

  echo "Stopping compose..."
  set +e
  compose "${PROJECT_NAME}" down
  echo "Stopping container ${CONTAINER_NAME} ..."
  docker stop "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  echo "Removing container ${CONTAINER_NAME} ..."
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  set -e

  rm -rf "${INSTALL_DIR}"
  echo "Removed ${BOT_NAME}."
}

restart_instance() {
  mapfile -t INSTANCES < <(list_instances_raw)
  [[ ${#INSTANCES[@]} -eq 0 ]] && { echo "No bots."; return; }
  for i in "${!INSTANCES[@]}"; do printf " %d) %s\n" "$((i+1))" "${INSTANCES[$i]}"; done
  read -rp "Number to restart: " sel
  BOT_NAME="${INSTANCES[$((sel-1))]}"
  compose "${BOT_NAME}" restart
}

logs_instance() {
  mapfile -t INSTANCES < <(list_instances_raw)
  [[ ${#INSTANCES[@]} -eq 0 ]] && { echo "No bots."; return; }
  for i in "${!INSTANCES[@]}"; do printf " %d) %s\n" "$((i+1))" "${INSTANCES[$i]}"; done
  read -rp "Number for logs: " sel
  BOT_NAME="${INSTANCES[$((sel-1))]}"
  compose "${BOT_NAME}" logs --tail=200 -f
}

# ---------- CLI shortcuts ----------
case "${1:-}" in
  install) require_root; install_docker_if_missing; mkdir -p "$BASE_DIR"; install_instance; exit;;
  remove)  require_root; mkdir -p "$BASE_DIR"; remove_instance; exit;;
  list)    require_root; list_instances_raw; exit;;
  restart) require_root; restart_instance; exit;;
  logs)    require_root; logs_instance; exit;;
esac

# ---------- Menu ----------
menu() {
  echo "=== Sales Manager Bot ==="
  echo "1) Install new bot"
  echo "2) Remove a bot"
  echo "3) List bots"
  echo "4) Restart bot"
  echo "5) Logs"
  echo "6) Exit"
  read -rp "Choice [1-6]: " c
  case "$c" in
    1) install_instance ;;
    2) remove_instance ;;
    3) list_instances_raw ;;
    4) restart_instance ;;
    5) logs_instance ;;
    6) exit 0 ;;
    *) echo "Invalid";;
  esac
}

main() {
  require_root
  install_docker_if_missing
  mkdir -p "$BASE_DIR"
  while true; do menu; echo; done
}

main "$@"