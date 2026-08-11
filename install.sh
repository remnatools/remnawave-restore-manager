#!/bin/bash
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

REPO_URL="https://github.com/YOUR_GITHUB_ORG/remnawave-restore-manager.git"
INSTALL_DIR="${INSTALL_DIR:-/root/remnawave-restore-manager}"
LOG_FILE="/var/log/remnawave_restore.log"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║    Remnawave Restore Manager — Install   ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# Docker
if ! command -v docker &>/dev/null; then
    info "Устанавливаю Docker..."
    curl -fsSL https://get.docker.com | sh
else
    info "Docker уже установлен"
fi

# acme.sh
if [ ! -f ~/.acme.sh/acme.sh ]; then
    info "Устанавливаю acme.sh..."
    curl https://get.acme.sh | sh
    source ~/.bashrc 2>/dev/null || true
else
    info "acme.sh уже установлен"
fi

# Docker network
if ! docker network ls | grep -q remnawave-network; then
    info "Создаю Docker сеть remnawave-network..."
    docker network create remnawave-network
else
    info "Сеть remnawave-network уже существует"
fi

# Клонируем репо
if [ -d "$INSTALL_DIR" ]; then
    warn "Папка $INSTALL_DIR уже существует — обновляю..."
    cd "$INSTALL_DIR" && git pull
else
    info "Клонирую репо в $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# .env
if [ ! -f .env ]; then
    cp .env.example .env
    info "Создан .env из примера"

    echo ""
    warn "Настройте параметры:"
    read -rp "  ADMIN_USER [admin]: " AU
    read -rsp "  ADMIN_PASS: " AP; echo ""
    read -rp "  IP этого сервера (для DNS): " HIP
    read -rp "  Домен для SSL-сертификата [restore.your-domain.com]: " DOM

    sed -i "s/^ADMIN_USER=.*/ADMIN_USER=${AU:-admin}/" .env
    sed -i "s/^ADMIN_PASS=.*/ADMIN_PASS=$AP/" .env
    sed -i "s/^HOST_IP=.*/HOST_IP=$HIP/" .env
    info ".env настроен"
else
    warn ".env уже существует — не перезаписываю"
    DOM=""
fi

# SSL
mkdir -p "$INSTALL_DIR/app/ssl"
if [ -n "$DOM" ] && [ ! -f "$INSTALL_DIR/app/ssl/fullchain.pem" ]; then
    info "Выпускаю SSL сертификат для $DOM..."
    warn "Порт 80 должен быть свободен!"
    ~/.acme.sh/acme.sh --issue -d "$DOM" \
        --standalone --server letsencrypt \
        --keylength 2048 \
        --key-file "$INSTALL_DIR/app/ssl/privkey.key" \
        --fullchain-file "$INSTALL_DIR/app/ssl/fullchain.pem" \
    && info "SSL сертификат получен" \
    || warn "Не удалось получить сертификат — получите вручную"
else
    warn "SSL: поместите сертификат в $INSTALL_DIR/app/ssl/ (privkey.key, fullchain.pem)"
fi

# Лог файл
touch "$LOG_FILE"
info "Лог-файл создан: $LOG_FILE"

# Запуск
info "Собираю и запускаю контейнер..."
docker compose up -d --build

echo ""
echo "╔═══════════════════════════════════════════════╗"
echo "║  ✅  Restore Manager запущен!                 ║"
echo "╠═══════════════════════════════════════════════╣"
echo "║  Доступен на: https://YOUR_SERVER_IP:9443     ║"
echo "║  Откройте в браузере и настройте Cloudflare   ║"
echo "╚═══════════════════════════════════════════════╝"
echo ""
info "Документация: $INSTALL_DIR/README.md"
