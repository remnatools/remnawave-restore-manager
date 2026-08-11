# Remnawave Restore Manager

Аварийный веб-инструмент для восстановления [Remnawave](https://github.com/remnawave) VPN-панели на резервном сервере.

![Python](https://img.shields.io/badge/Python-FastAPI-009688?style=flat-square)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)

## Концепция

Restore Manager устанавливается на **резервном (standby) сервере** и хранит копии бэкапов, поступающих по rsync с основного сервера через [Backup Manager](https://github.com/YOUR_GITHUB_ORG/remnawave-backup-manager).

При аварии на основном сервере — открываете браузер, проходите 5 шагов wizard и панель работает на резервном.

## Возможности

- **5-шаговый wizard восстановления** с live-логом в браузере (SSE)
- **Dry Run режим** — полная симуляция без реальных изменений
- **Автообнаружение Redis** — TCP или Unix socket, исправляет `.env` автоматически
- **Alembic recovery** — при несовместимости версий бота пересобирает из `bot_src.tar.gz`
- **DNS переключение** — обновляет A-записи в Cloudflare через API (шаг 3)
- **Брендинг** — кастомный логотип, favicon, название
- **Управление бэкапами** — список, удаление, авточистка по лимиту
- Работает **без nginx** — SSL напрямую через uvicorn (xray занимает порт 443)

## Шаги wizard

| Шаг | Действие |
|-----|----------|
| 0 | Выбор бэкапа из списка |
| 1 | Сканирование системы, остановка мешающих сервисов |
| 2 | Восстановление БД панели и бота из бэкапа |
| 3 | Переключение DNS в Cloudflare на IP этого сервера |
| 4 | Запуск всего стека + health check |

## Установка

### Быстрый старт (одна команда)

```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_GITHUB_ORG/remnawave-restore-manager/main/install.sh | bash
```

### Ручная установка

**1. Установить Docker и acme.sh**

```bash
curl -fsSL https://get.docker.com | sh
curl https://get.acme.sh | sh && source ~/.bashrc
```

**2. Клонировать репо**

```bash
git clone https://github.com/YOUR_GITHUB_ORG/remnawave-restore-manager.git ~/remnawave-restore-manager
cd ~/remnawave-restore-manager
```

**3. Создать `.env`**

```bash
cp .env.example .env
nano .env   # задать ADMIN_USER, ADMIN_PASS, HOST_IP
```

**4. Выпустить SSL сертификат**

> ⚠️ Используйте RSA 2048 (`--keylength 2048`) — uvicorn не поддерживает EC ключи

```bash
mkdir -p ~/remnawave-restore-manager/app/ssl

~/.acme.sh/acme.sh --issue -d restore.your-domain.com \
  --standalone --server letsencrypt \
  --keylength 2048 \
  --key-file ~/remnawave-restore-manager/app/ssl/privkey.key \
  --fullchain-file ~/remnawave-restore-manager/app/ssl/fullchain.pem
```

**5. Создать лог-файл и запустить**

```bash
touch /var/log/remnawave_restore.log
docker compose up -d --build
```

**6. Открыть firewall**

```bash
ufw allow 9443/tcp
```

Интерфейс доступен на `https://YOUR_SERVER_IP:9443`.

### Настройка Cloudflare (для шага 3)

В интерфейсе → **Настройки**:

1. Создайте токен на https://dash.cloudflare.com/profile/api-tokens
2. Template: **Edit zone DNS** → Zone Resources: ваш домен
3. Вставьте токен в настройки, нажмите **Определить IP автоматически**

## Архитектура (warm standby)

```
Резервный сервер
├── remnanode (docker)      — xray нода, порт 443
├── restore-manager         — порт 9443, HTTPS напрямую (uvicorn + SSL)
│   └── app/ssl/            — SSL сертификат (acme.sh, RSA 2048)
└── remnawave стек          — остановлен, ждёт восстановления
    ├── remnawave-db
    ├── remnawave-redis
    ├── remnawave (панель)
    ├── remnawave_bot
    └── cabinet_frontend
```

nginx **не нужен** до момента восстановления — wizard поднимает его на шаге 4.

## Конфигурация

```env
ADMIN_USER=admin
ADMIN_PASS=your-password

# IP этого сервера (куда переключать DNS)
HOST_IP=

# Пути на хосте
HOST_BACKUP_ROOT=/root/remnawave-backups
REMNAWAVE_DIR=/opt/remnawave
BOT_DIR=/root/remnawave-bedolaga-telegram-bot
CABINET_DIR=/root/bedolaga-cabinet
```

## Структура репо

```
remnawave-restore-manager/
├── app/
│   └── main.py              # FastAPI приложение
├── templates/
│   └── index.html           # Веб-интерфейс wizard
├── data/                    # Конфиг и токены (не в git)
├── install.sh               # Установщик
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── .gitignore
```

## Связанные проекты

- **[Remnawave Backup Manager](https://github.com/YOUR_GITHUB_ORG/remnawave-backup-manager)** — создаёт бэкапы и отправляет их на этот сервер по rsync

## Лицензия

MIT
