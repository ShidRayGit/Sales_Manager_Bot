#!/usr/bin/env bash
set -euo pipefail

# ========= Repo Settings =========
OWNER="ShidRayGit"
REPO="Sales_Manager_Bot"
BRANCH="main"
RAW_BASE="https://raw.githubusercontent.com/${OWNER}/${REPO}/${BRANCH}"
INSTALL_DIR="/opt/sales-manager-bot"
# ================================================

echo ">>> Sales Manager Bot installer"

if [[ $EUID -ne 0 ]]; then
  echo "لطفاً با sudo یا به‌عنوان root اجرا کن."
  exit 1
fi

read -rp "🤖 BOT_TOKEN را وارد کن: " BOT_TOKEN
read -rp "👤 ADMIN_CHAT_ID (می‌تونی چندتا با کاما بدی): " ADMIN_CHAT_ID
read -rp "🕒 TZ (پیش‌فرض Asia/Tehran): " TZ_INPUT
TZ_INPUT=${TZ_INPUT:-"Asia/Tehran"}

# نصب Docker + Compose اگر موجود نیست
if ! command -v docker >/dev/null 2>&1; then
  echo ">>> نصب Docker و Compose plugin ..."
  apt-get update -y
  apt-get install -y ca-certificates curl gnupg lsb-release
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    | tee /etc/apt/sources.list.d/docker.list > /dev/null
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo ">>> دریافت فایل‌ها از GitHub..."
curl -fsSLo telegram_subscription_bot.py "${RAW_BASE}/telegram_subscription_bot.py"
curl -fsSLo Dockerfile "${RAW_BASE}/Dockerfile"
curl -fsSLo docker-compose.yml "${RAW_BASE}/docker-compose.yml"

cat > .env <<EOF
BOT_TOKEN=${BOT_TOKEN}
ADMIN_CHAT_ID=${ADMIN_CHAT_ID}
TZ=${TZ_INPUT}
DB_PATH=/app/data/data.db
BACKUP_SRC=/app
MAX_BACKUP_MB=45
EOF
chmod 600 .env

echo ">>> ساخت ایمیج و اجرای کانتینر..."
docker compose build
docker compose up -d

echo "✅ نصب تمام شد!"
echo "➡️ نمایش لاگ: docker compose logs -f"
echo "➡️ ری‌استارت: docker compose restart"
echo "➡️ دیتابیس روی هاست: ${INSTALL_DIR}/data/data.db"