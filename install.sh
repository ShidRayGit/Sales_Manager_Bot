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
  # Keep a-z0-9, dot, underscore, dash; convert spaces to dashes; lowercase
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

# ------- Actions -------
install_instance() {
  echo "=== Install a new bot instance ==="
  read -rp "Bot instance name (e.g. prod, test, myshop): " BOT_NAME_IN
  read -rp "BOT_TOKEN: " BOT_TOKEN
  read -rp "ADMIN_CHAT_ID (comma-separated if multiple): " ADMIN_CHAT_ID
  read -rp "Time zone (default Asia/Tehran): " TZ_INPUT
  TZ_INPUT="${TZ_INPUT:-Asia/Tehran}"

  BOT_NAME="$(slugify "${BOT_NAME_IN:-bot}")"
  INSTALL_DIR="${BASE_DIR}/${BOT_NAME}"
  CONTAINER_NAME="telegram-bot-${BOT_NAME}"
  PROJECT_NAME="${BOT_NAME}"

  echo
  echo "Instance:        ${BOT_NAME}"
  echo "Install dir:     ${INSTALL_DIR}"
  echo "Container name:  ${CONTAINER_NAME}"
  echo "Compose project: ${PROJECT_NAME}"
  echo

  mkdir -p "${INSTALL_DIR}"
  cd "${INSTALL_DIR}"

  echo "Fetching files from GitHub..."
  curl -fsSLo telegram_subscription_bot.py "${RAW_BASE}/telegram_subscription_bot.py"
  curl -fsSLo Dockerfile "${RAW_BASE}/Dockerfile"

  # Generate compose per instance
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

  # .env for this instance
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
  compose "${PROJECT_NAME}" build
  compose "${PROJECT_NAME}" up -d
  compose "${PROJECT_NAME}" ps

  echo
  echo "Done. Tail logs with:"
  echo "  COMPOSE_PROJECT_NAME='${PROJECT_NAME}' docker compose logs -f"
}

remove_instance() {
  echo "=== Remove an existing bot instance ==="
  read -rp "Bot instance name to remove: " BOT_NAME_IN
  BOT_NAME="$(slugify "${BOT_NAME_IN:-bot}")"
  INSTALL_DIR="${BASE_DIR}/${BOT_NAME}"
  PROJECT_NAME="${BOT_NAME}"
  CONTAINER_NAME="telegram-bot-${BOT_NAME}"

  if [[ ! -d "${INSTALL_DIR}" ]]; then
    echo "Instance directory not found: ${INSTALL_DIR}"
    return 1
  fi

  echo "This will stop and remove container(s) and delete: ${INSTALL_DIR}"
  read -rp "Type the instance name to confirm (${BOT_NAME}): " CONFIRM
  if [[ "$(slugify "$CONFIRM")" != "${BOT_NAME}" ]]; then
    echo "Aborted."
    return 1
  fi

  set +e
  compose "${PROJECT_NAME}" down
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1
  set -e

  rm -rf "${INSTALL_DIR}"
  echo "Instance '${BOT_NAME}' removed."
}

list_instances() {
  echo "=== Instances under ${BASE_DIR} ==="
  if [[ ! -d "${BASE_DIR}" ]]; then
    echo "(none)"
    return 0
  fi
  shopt -s nullglob
  for d in "${BASE_DIR}"/*; do
    [[ -d "$d" ]] || continue
    name="$(basename "$d")"
    echo "- ${name}"
  done
  shopt -u nullglob
}

restart_instance() {
  read -rp "Bot instance name to restart: " BOT_NAME_IN
  BOT_NAME="$(slugify "${BOT_NAME_IN:-bot}")"
  PROJECT_NAME="${BOT_NAME}"
  compose "${PROJECT_NAME}" restart
  compose "${PROJECT_NAME}" ps
}

logs_instance() {
  read -rp "Bot instance name to show logs: " BOT_NAME_IN
  BOT_NAME="$(slugify "${BOT_NAME_IN:-bot}")"
  PROJECT_NAME="${BOT_NAME}"
  compose "${PROJECT_NAME}" logs --tail=200 -f
}

# ------- CLI shortcuts (optional) -------
# Usage: install.sh install|remove|list|restart|logs
if [[ "${1:-}" == "install" ]]; then require_root; install_docker_if_missing; mkdir -p "${BASE_DIR}"; install_instance; exit 0; fi
if [[ "${1:-}" == "remove"  ]]; then require_root; mkdir -p "${BASE_DIR}"; remove_instance; exit 0; fi
if [[ "${1:-}" == "list"    ]]; then require_root; list_instances; exit 0; fi
if [[ "${1:-}" == "restart" ]]; then require_root; restart_instance; exit 0; fi
if [[ "${1:-}" == "logs"    ]]; then require_root; logs_instance; exit 0; fi

# ------- TUI Menu -------
menu() {
  echo "=== Sales Manager Bot ==="
  echo "1) Install new bot"
  echo "2) Remove a bot"
  echo "3) List bots"
  echo "4) Restart a bot"
  echo "5) Show logs"
  echo "6) Exit"
  read -rp "Select an option [1-6]: " opt
  case "$opt" in
    1) install_instance ;;
    2) remove_instance ;;
    3) list_instances ;;
    4) restart_instance ;;
    5) logs_instance ;;
    6) exit 0 ;;
    *) echo "Invalid option";;
  esac
}

main() {
  require_root
  install_docker_if_missing
  mkdir -p "${BASE_DIR}"
  while true; do
    menu
    echo
  done
}

main "$@"