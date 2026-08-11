from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import json, os, secrets, hashlib, asyncio, subprocess, shutil, urllib.request, queue, threading
from pathlib import Path
from typing import Optional

app = FastAPI()
bearer = HTTPBearer(auto_error=False)

CONFIG_FILE   = "/app/data/config.json"
TOKENS_FILE   = "/app/data/tokens.json"
BACKUP_ROOT   = os.environ.get("BACKUP_ROOT", "/backups")
ADMIN_USER    = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS    = os.environ.get("ADMIN_PASS", "changeme")
REMNAWAVE_DIR = os.environ.get("REMNAWAVE_DIR", "/opt/remnawave")
BOT_DIR       = os.environ.get("BOT_DIR", "/root/remnawave-bedolaga-telegram-bot")
CABINET_DIR   = os.environ.get("CABINET_DIR", "/root/bedolaga-cabinet")
HOST_IP       = os.environ.get("HOST_IP", "")  # IP этого сервера для DNS
LOGO_FILE     = "/app/data/logo"

def get_logo_path():
    for ext in [".png", ".svg", ".jpg", ".jpeg", ".webp"]:
        p = f"{LOGO_FILE}{ext}"
        if os.path.exists(p):
            return p
    return None


# ── auth ─────────────────────────────────────────────────────────────────────

def load_tokens():
    try:
        if os.path.exists(TOKENS_FILE):
            with open(TOKENS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_tokens(t):
    os.makedirs(os.path.dirname(TOKENS_FILE), exist_ok=True)
    with open(TOKENS_FILE, "w") as f:
        json.dump(t, f)

def make_token(user):
    return hashlib.sha256(f"{user}{secrets.token_hex(16)}".encode()).hexdigest()

def check_auth(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Unauthorized")
    tokens = load_tokens()
    if credentials.credentials not in tokens:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return tokens[credentials.credentials]


# ── config ────────────────────────────────────────────────────────────────────

def get_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {
        "cf_token": "",
        "cf_zone_id": "",
        "cf_domains": ["your-domain.com", "sub.your-domain.com", "cabinet.your-domain.com"],
        "host_ip": HOST_IP,
        "server_alias": "",
        "brand_name": "ВЛЕС",
        "logo_bg": "linear-gradient(135deg,#e74c3c,#922b21)",
        "keep_backups": 7,
    }

def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ── helpers ───────────────────────────────────────────────────────────────────

def fmt_size(b):
    for u in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} GB"

def get_backups():
    backups = []
    p = Path(BACKUP_ROOT)
    if not p.exists():
        return backups
    for d in sorted(p.iterdir(), reverse=True):
        if d.is_dir() and d.name[:4].isdigit():
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            files = [f.name for f in d.iterdir() if f.is_file()]
            manifest = {}
            mf = d / "manifest.txt"
            if mf.exists():
                for line in mf.read_text().splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        manifest[k.strip()] = v.strip()
            backups.append({
                "name": d.name,
                "size": size,
                "size_human": fmt_size(size),
                "files": files,
                "manifest": manifest,
            })
    return backups

LOG_FILE = "/var/log/remnawave_restore.log"


def log(msg: str):
    import datetime
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    with open(LOG_FILE, "a") as f:
        f.write(line)


def get_log():
    if os.path.exists(LOG_FILE):
        lines = Path(LOG_FILE).read_text().splitlines()
        return "\n".join(lines[-200:])
    return "Лог пуст"


def run_cmd(cmd, **kwargs):
    """Запустить команду, вернуть (ok, output)."""
    kwargs.setdefault("timeout", 60)
    result = subprocess.run(
        cmd, capture_output=True, text=True, **kwargs
    )
    out = (result.stdout + result.stderr).strip()
    return result.returncode == 0, out

def docker_compose_run(path, *args, timeout=120):
    return run_cmd(
        ["docker", "compose", *args],
        cwd=path,
        timeout=timeout,
    )


# ── Cloudflare API ────────────────────────────────────────────────────────────

def cf_request(method, path, token, body=None):
    url = f"https://api.cloudflare.com/client/v4{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

def cf_get_zone_id(token, domain_root):
    """Найти zone_id по корневому домену."""
    r = cf_request("GET", f"/zones?name={domain_root}", token)
    if r.get("success") and r["result"]:
        return r["result"][0]["id"]
    return None

def cf_get_records(token, zone_id, domains):
    """Получить A-записи для списка доменов."""
    records = []
    for d in domains:
        r = cf_request("GET", f"/zones/{zone_id}/dns_records?type=A&name={d}", token)
        if r.get("success"):
            records.extend(r["result"])
    return records

def cf_update_record(token, zone_id, record_id, name, ip):
    return cf_request("PATCH", f"/zones/{zone_id}/dns_records/{record_id}", token, {
        "content": ip,
        "proxied": False,
    })


# ── page render ───────────────────────────────────────────────────────────────

def render_page(backups, cfg):
    backups_json  = json.dumps(backups)
    backup_count  = len(backups)
    latest        = backups[0]["name"] if backups else "—"
    cf_token      = cfg.get("cf_token", "")
    cf_zone_id    = cfg.get("cf_zone_id", "")
    cf_domains    = "\n".join(cfg.get("cf_domains", []))
    host_ip       = cfg.get("host_ip", "")
    server_alias  = cfg.get("server_alias", "")
    brand_name    = cfg.get("brand_name", "ВЛЕС")
    logo_bg       = cfg.get("logo_bg", "linear-gradient(135deg,#e74c3c,#922b21)")
    has_logo      = "true" if get_logo_path() else "false"
    server_label  = f"{server_alias} ({host_ip})" if server_alias else host_ip
    keep_backups = cfg.get("keep_backups", 7)
    cf_status  = (
        '<span style="color:var(--accent2)">✓ Настроен</span>'
        if cf_token else
        '<span style="color:var(--muted)">Не настроен</span>'
    )

    with open("/app/templates/index.html") as f:
        html = f.read()

    html = html.replace("{{BACKUP_COUNT}}",  str(backup_count))
    html = html.replace("{{LATEST_BACKUP}}", latest)
    html = html.replace("{{CF_STATUS}}",     cf_status)
    html = html.replace("{{CF_TOKEN}}",      cf_token)
    html = html.replace("{{CF_ZONE_ID}}",    cf_zone_id)
    html = html.replace("{{CF_DOMAINS}}",    cf_domains)
    html = html.replace("{{HOST_IP}}",       host_ip)
    html = html.replace("{{KEEP_BACKUPS}}",  str(keep_backups))
    html = html.replace("{{BACKUPS_JSON}}",  backups_json)
    html = html.replace("{{BRAND_NAME}}",    brand_name)
    html = html.replace("{{LOGO_BG}}",       logo_bg)
    html = html.replace("{{HAS_LOGO}}",      has_logo)
    html = html.replace("{{SERVER_LABEL}}", server_label)
    html = html.replace("{{SERVER_ALIAS}}",  server_alias)
    return html


# ── login page ────────────────────────────────────────────────────────────────

LOGIN_HTML = '''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ВЛЕС — Restore</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d0f14; color: #e2e8f0; font-family: -apple-system, sans-serif; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
  .login-box { background: #151820; border: 1px solid #252a3a; border-radius: 16px; padding: 40px; width: 360px; }
  .logo { display: flex; align-items: center; gap: 12px; margin-bottom: 32px; justify-content: center; }
  .logo-icon { width: 40px; height: 40px; background: linear-gradient(135deg, #e74c3c, #c0392b); border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 20px; }
  .logo-text { font-size: 18px; font-weight: 700; }
  .logo-sub { font-size: 12px; color: #6b7a99; margin-top: 2px; }
  .form-group { margin-bottom: 16px; }
  .form-label { display: block; font-size: 12px; color: #6b7a99; margin-bottom: 6px; font-weight: 500; }
  .form-input { width: 100%; background: #0d0f14; border: 1px solid #252a3a; border-radius: 8px; padding: 10px 14px; color: #e2e8f0; font-size: 14px; }
  .form-input:focus { outline: none; border-color: #e74c3c; }
  .btn-login { width: 100%; background: #e74c3c; color: white; border: none; border-radius: 8px; padding: 11px; font-size: 14px; font-weight: 600; cursor: pointer; margin-top: 8px; }
  .btn-login:hover { background: #c0392b; }
  .error { color: #e74c3c; font-size: 13px; margin-top: 12px; text-align: center; }
  .warning { background: #3a1a1a; border: 1px solid #5a2a2a; border-radius: 8px; padding: 10px 14px; font-size: 12px; color: #e74c3c; margin-bottom: 20px; text-align: center; }
</style>
</head>
<body>
<div class="login-box">
  <div class="logo">
    <div class="logo-icon">🚨</div>
    <div><div class="logo-text">ВЛЕС Restore</div><div class="logo-sub">Менеджер восстановления</div></div>
  </div>
  <div class="warning">⚠ Аварийный инструмент</div>
  <div id="error" class="error" style="display:none">Неверный логин или пароль</div>
  <div class="form-group"><label class="form-label">Логин</label><input class="form-input" type="text" id="username" autofocus></div>
  <div class="form-group"><label class="form-label">Пароль</label><input class="form-input" type="password" id="password"></div>
  <button class="btn-login" onclick="doLogin()">Войти</button>
</div>
<script>
async function doLogin() {
  const u = document.getElementById('username').value;
  const p = document.getElementById('password').value;
  const r = await fetch('/api/login', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({username: u, password: p}) });
  if (r.ok) { const d = await r.json(); localStorage.setItem('auth_token', d.token); window.location.href = '/'; }
  else { document.getElementById('error').style.display = 'block'; }
}
document.addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
</script>
</body>
</html>'''


# ── routes: auth ──────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    cfg = get_config()
    brand    = cfg.get("brand_name", "ВЛЕС")
    logo_bg  = cfg.get("logo_bg", "linear-gradient(135deg,#e74c3c,#c0392b)")
    has_logo = get_logo_path() is not None
    logo_ts  = int(os.path.getmtime(get_logo_path())) if has_logo else 0
    logo_html = f'<img src="/api/logo?v={logo_ts}" style="width:100%;height:100%;object-fit:contain;border-radius:10px">' if has_logo else "🚨"

    html = f'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" href="/favicon.ico?v={logo_ts}" type="image/png">
<title>{brand} — Restore</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d0f14; color: #e2e8f0; font-family: -apple-system, sans-serif; min-height: 100vh; display: flex; align-items: center; justify-content: center; }}
  .login-box {{ background: #151820; border: 1px solid #252a3a; border-radius: 16px; padding: 40px; width: 360px; max-width: 94vw; }}
  .logo {{ display: flex; align-items: center; gap: 12px; margin-bottom: 32px; justify-content: center; }}
  .logo-icon {{ width: 40px; height: 40px; background: {logo_bg}; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 20px; overflow: hidden; }}
  .logo-text {{ font-size: 18px; font-weight: 700; }}
  .logo-sub {{ font-size: 12px; color: #6b7a99; margin-top: 2px; }}
  .form-group {{ margin-bottom: 16px; }}
  .form-label {{ display: block; font-size: 12px; color: #6b7a99; margin-bottom: 6px; font-weight: 500; }}
  .form-input {{ width: 100%; background: #0d0f14; border: 1px solid #252a3a; border-radius: 8px; padding: 10px 14px; color: #e2e8f0; font-size: 14px; }}
  .form-input:focus {{ outline: none; border-color: #e74c3c; }}
  .btn-login {{ width: 100%; background: #e74c3c; color: white; border: none; border-radius: 8px; padding: 11px; font-size: 14px; font-weight: 600; cursor: pointer; margin-top: 8px; }}
  .btn-login:hover {{ background: #c0392b; }}
  .error {{ color: #e74c3c; font-size: 13px; margin-top: 12px; text-align: center; }}
  .warning {{ background: #3a1a1a; border: 1px solid #5a2a2a; border-radius: 8px; padding: 10px 14px; font-size: 12px; color: #e74c3c; margin-bottom: 20px; text-align: center; }}
</style>
</head>
<body>
<div class="login-box">
  <div class="logo">
    <div class="logo-icon">{logo_html}</div>
    <div><div class="logo-text">{brand} Restore</div><div class="logo-sub">Менеджер восстановления</div></div>
  </div>
  <div class="warning">⚠ Аварийный инструмент</div>
  <div id="error" class="error" style="display:none">Неверный логин или пароль</div>
  <div class="form-group"><label class="form-label">Логин</label><input class="form-input" type="text" id="username" autofocus></div>
  <div class="form-group"><label class="form-label">Пароль</label><input class="form-input" type="password" id="password"></div>
  <button class="btn-login" onclick="doLogin()">Войти</button>
</div>
<script>
async function doLogin() {{
  const u = document.getElementById("username").value;
  const p = document.getElementById("password").value;
  const r = await fetch("/api/login", {{ method: "POST", headers: {{"Content-Type":"application/json"}}, body: JSON.stringify({{username: u, password: p}}) }});
  if (r.ok) {{ const d = await r.json(); localStorage.setItem("auth_token", d.token); window.location.href = "/"; }}
  else {{ document.getElementById("error").style.display = "block"; }}
}}
document.addEventListener("keydown", e => {{ if (e.key === "Enter") doLogin(); }});
</script>
</body>
</html>'''
    return HTMLResponse(html)

@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    if secrets.compare_digest(body.get("username", ""), ADMIN_USER) and \
       secrets.compare_digest(body.get("password", ""), ADMIN_PASS):
        token = make_token(body["username"])
        tokens = load_tokens()
        tokens[token] = body["username"]
        save_tokens(tokens)
        return {"token": token}
    raise HTTPException(status_code=401, detail="Invalid credentials")

@app.get("/logout")
async def logout(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    if credentials:
        tokens = load_tokens()
        tokens.pop(credentials.credentials, None)
        save_tokens(tokens)
    return RedirectResponse("/login", status_code=302)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    cfg = get_config()
    backups = get_backups()
    return HTMLResponse(render_page(backups, cfg))


# ── routes: config ────────────────────────────────────────────────────────────

@app.get("/api/config")
async def api_get_config(user=Depends(check_auth)):
    return get_config()

@app.post("/api/config")
async def api_save_config(request: Request, user=Depends(check_auth)):
    body = await request.json()
    cfg = get_config()
    for key in ["cf_token", "cf_zone_id", "host_ip", "brand_name", "logo_bg"]:
        if key in body:
            cfg[key] = body[key]
    # server_alias — всегда сохраняем (даже пустую строку)
    if "server_alias" in body:
        cfg["server_alias"] = str(body["server_alias"]).strip()
    if "cf_domains" in body:
        cfg["cf_domains"] = [d.strip() for d in body["cf_domains"] if d.strip()]
    if "keep_backups" in body:
        cfg["keep_backups"] = max(1, int(body["keep_backups"]))
    save_config(cfg)
    return {"status": "ok"}


@app.delete("/api/backups/{name}")
async def delete_backup(name: str, user=Depends(check_auth)):
    """Удалить конкретный бэкап."""
    backup_path = Path(BACKUP_ROOT) / name
    if not backup_path.exists():
        raise HTTPException(404, "Бэкап не найден")
    shutil.rmtree(backup_path)
    log(f"Удалён бэкап: {name}")
    return {"status": "ok"}


@app.post("/api/backups/cleanup")
async def cleanup_backups(user=Depends(check_auth)):
    """Удалить старые бэкапы сверх лимита keep_backups."""
    cfg = get_config()
    keep = cfg.get("keep_backups", 7)
    backups = get_backups()  # отсортированы новые первыми
    to_delete = backups[keep:]
    deleted = []
    for b in to_delete:
        p = Path(BACKUP_ROOT) / b["name"]
        if p.exists():
            shutil.rmtree(p)
            deleted.append(b["name"])
            log(f"Автоочистка: удалён {b['name']}")
    return {"deleted": deleted, "count": len(deleted), "kept": min(keep, len(backups))}


# ── routes: backups ───────────────────────────────────────────────────────────

@app.get("/api/backups")
async def api_backups(user=Depends(check_auth)):
    cfg = get_config()
    keep = cfg.get("keep_backups", 7)
    backups = get_backups()
    # Автоочистка если бэкапов больше лимита
    if len(backups) > keep:
        for b in backups[keep:]:
            p = Path(BACKUP_ROOT) / b["name"]
            if p.exists():
                shutil.rmtree(p)
                log(f"Автоочистка: удалён {b['name']}")
        backups = get_backups()
    return backups


# ── routes: wizard steps ──────────────────────────────────────────────────────

@app.get("/api/myip")
async def get_my_ip(user=Depends(check_auth)):
    """Определить внешний IP сервера."""
    for url in ["https://api.ipify.org", "https://api4.my-ip.io/ip", "https://ipv4.icanhazip.com"]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                ip = r.read().decode().strip()
                if ip:
                    return {"ip": ip}
        except Exception:
            continue
    raise HTTPException(500, "Не удалось определить IP")
async def api_log(user=Depends(check_auth)):
    return {"log": get_log()}


@app.post("/api/cf/test")
async def cf_test(user=Depends(check_auth)):
    """Проверить Cloudflare токен через бэкенд."""
    cfg = get_config()
    token = cfg.get("cf_token", "")
    if not token:
        raise HTTPException(400, "Токен не настроен")
    r = cf_request("GET", "/user/tokens/verify", token)
    if r.get("success"):
        status = r.get("result", {}).get("status", "unknown")
        return {"ok": True, "message": f"Токен валиден: {status}"}
    errors = r.get("errors", [])
    raise HTTPException(400, f"Ошибка: {errors}")


@app.get("/api/step/1/scan")
async def step1_scan(user=Depends(check_auth)):
    """Сканировать систему: найти мешающие контейнеры, xray и занятые порты."""
    log("=== ШАГ 1: Сканирование системы ===")

    # Контейнеры которые МЕШАЮТ восстановлению (nginx и restore НЕ в списке)
    KNOWN_CONTAINERS = {
        "remnawave":           {"label": "Панель Remnawave"},
        "remnawave_bot":       {"label": "Бот Bedolaga"},
        "cabinet_frontend":    {"label": "Кабинет (frontend)"},
        "remnawave-sub-page":  {"label": "Subscription page"},
        "remnawave-db":        {"label": "БД панели"},
        "remnawave-redis":     {"label": "Redis панели"},
        "remnawave_bot_db":    {"label": "БД бота"},
        "remnawave_bot_redis": {"label": "Redis бота"},
        "remnanode":           {"label": "xray (remnanode docker)"},
    }

    _, out = run_cmd(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"])
    running_containers = {}
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            running_containers[parts[0]] = parts[1]

    found = []
    for cname, info in KNOWN_CONTAINERS.items():
        if cname in running_containers:
            found.append({
                "name": cname,
                "label": info["label"],
                "status": running_containers[cname],
                "type": "docker",
            })

    # Сканируем xray через /proc (pid: host)
    import glob as _glob
    services = []
    for comm_path in _glob.glob("/proc/*/comm"):
        try:
            with open(comm_path) as cf:
                if cf.read().strip() == "xray":
                    pid = comm_path.split("/")[2]
                    services.append({
                        "name": "xray",
                        "label": "xray (systemd)",
                        "status": f"running (PID {pid})",
                        "type": "systemd",
                        "pid": pid,
                    })
                    break
        except Exception:
            continue

    # Проверить занятые критичные порты через /proc/1/net/tcp (сеть хоста)
    # 443 не проверяем — занят nginx который мы не трогаем
    port_issues = []
    for port in [3000, 8080, 80]:
        port_hex = format(port, '04X')
        occupied = False
        for tcp_file in ["/proc/1/net/tcp", "/proc/1/net/tcp6"]:
            try:
                with open(tcp_file) as f:
                    for line in f.readlines()[1:]:
                        parts = line.strip().split()
                        if len(parts) > 3:
                            local_port = parts[1].split(":")[1]
                            state = parts[3]
                            if local_port.upper() == port_hex and state == "0A":
                                occupied = True
                                break
            except Exception:
                continue
            if occupied:
                break
        if occupied:
            port_issues.append({"port": port, "detail": f"порт {port} занят"})

    all_items = found + services
    log(f"Мешающих: {len(found)} контейнеров, {len(services)} процессов, {len(port_issues)} портов")
    return {"containers": all_items, "port_issues": port_issues, "blocking_count": len(all_items)}


@app.post("/api/step/1/stop")
async def step1_stop(request: Request, user=Depends(check_auth)):
    """Остановить контейнер или systemd процесс."""
    body    = await request.json()
    name    = body.get("name", "").strip()
    kind    = body.get("type", "docker")
    dry_run = body.get("dry_run", False)

    if not name:
        raise HTTPException(400, "Не указано имя")

    if dry_run:
        log(f"[DRY RUN] stop {name} ({kind})")
        return {"ok": True, "message": f"[DRY RUN] {name} был бы остановлен"}

    if kind == "systemd":
        import glob as _glob, signal as _sig
        pid = None
        for comm_path in _glob.glob("/proc/*/comm"):
            try:
                with open(comm_path) as cf:
                    if cf.read().strip() == name:
                        pid = int(comm_path.split("/")[2])
                        break
            except Exception:
                continue
        if pid:
            try:
                os.kill(pid, _sig.SIGTERM)
                msg = f"✓ {name} (PID {pid}) остановлен"
                ok = True
            except Exception as e:
                msg = f"✗ {name}: {e}"
                ok = False
        else:
            msg = f"ℹ {name} уже не запущен"
            ok = True
    else:
        ok, out = run_cmd(["docker", "stop", name])
        msg = f"✓ {name} остановлен" if ok else f"✗ {name}: {out[:100]}"

    log(msg)
    return {"ok": ok, "message": msg}


@app.post("/api/step/1/stop-all")
async def step1_stop_all(request: Request, user=Depends(check_auth)):
    """Остановить все мешающие контейнеры и процессы."""
    body    = await request.json()
    dry_run = body.get("dry_run", False)
    logs    = []
    log(f"=== ШАГ 1: {'[DRY RUN] ' if dry_run else ''}Остановка всех мешающих сервисов ===")

    STOP_CONTAINERS = [
        "remnawave", "remnawave_bot", "cabinet_frontend", "remnawave-sub-page",
        "remnawave-db", "remnawave-redis", "remnawave_bot_db", "remnawave_bot_redis", "remnanode",
    ]

    _, out = run_cmd(["docker", "ps", "--format", "{{.Names}}"])
    running = out.splitlines()

    for cname in STOP_CONTAINERS:
        if cname in running:
            if dry_run:
                logs.append(f"[DRY RUN] docker stop {cname}")
            else:
                ok, cout = run_cmd(["docker", "stop", cname])
                logs.append(f"✓ {cname} остановлен" if ok else f"✗ {cname}: {cout[:80]}")

    # Остановить xray
    import glob as _glob, signal as _sig
    for comm_path in _glob.glob("/proc/*/comm"):
        try:
            with open(comm_path) as cf:
                if cf.read().strip() == "xray":
                    pid = int(comm_path.split("/")[2])
                    if dry_run:
                        logs.append(f"[DRY RUN] kill xray (PID {pid})")
                    else:
                        os.kill(pid, _sig.SIGTERM)
                        logs.append(f"✓ xray (PID {pid}) остановлен")
                    break
        except Exception:
            continue

    for l in logs: log(l)
    return {"ok": True, "logs": logs}


@app.get("/api/step/1/stop-all/stream")
async def step1_stop_all_stream(dry_run: str = "false", token: str = ""):
    """SSE stream для остановки всех сервисов — live обновления."""
    tokens = load_tokens()
    if token not in tokens:
        return HTMLResponse("Unauthorized", status_code=401)

    dry = dry_run.lower() == "true"

    async def generate():
        q = queue.Queue()

        def emit(msg: str):
            q.put(msg)
            log(msg)

        def run_stop():
            import glob as _glob, signal as _sig
            emit(f"=== ШАГ 1: {'[DRY RUN] ' if dry else ''}Остановка сервисов ===")

            STOP_CONTAINERS = [
                "remnawave", "remnawave_bot", "cabinet_frontend", "remnawave-sub-page",
                "remnawave-db", "remnawave-redis", "remnawave_bot_db", "remnawave_bot_redis", "remnanode",
            ]

            _, out = run_cmd(["docker", "ps", "--format", "{{.Names}}"])
            running = out.splitlines()

            for cname in STOP_CONTAINERS:
                if cname in running:
                    if dry:
                        emit(f"[DRY RUN] docker stop {cname}")
                    else:
                        emit(f"⏳ Останавливаю {cname}...")
                        ok, cout = run_cmd(["docker", "stop", cname], timeout=30)
                        emit(f"✓ {cname} остановлен" if ok else f"✗ {cname}: {cout[:80]}")

            # xray
            for comm_path in _glob.glob("/proc/*/comm"):
                try:
                    with open(comm_path) as cf:
                        if cf.read().strip() == "xray":
                            pid = int(comm_path.split("/")[2])
                            if dry:
                                emit(f"[DRY RUN] kill xray (PID {pid})")
                            else:
                                emit(f"⏳ Останавливаю xray (PID {pid})...")
                                os.kill(pid, _sig.SIGTERM)
                                emit(f"✓ xray (PID {pid}) остановлен")
                            break
                except Exception:
                    continue

            emit("✓ Готово — повторяю сканирование")
            q.put(None)

        t = threading.Thread(target=run_stop, daemon=True)
        t.start()

        while True:
            try:
                item = q.get(timeout=30)
                if item is None:
                    yield "data: {\"done\": true}\n\n"
                    break
                yield f"data: {json.dumps({'log': item})}\n\n"
            except queue.Empty:
                yield "data: {\"ping\": true}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    """Определить конфигурацию Redis и исправить .env если нужно."""
    # Проверяем команду запуска remnawave-redis
    ok, out = run_cmd(["docker", "inspect", "remnawave-redis",
                       "--format", "{{.Config.Cmd}}"])
    if not ok:
        # Контейнер не запущен — пробуем определить из docker-compose
        ok, out = run_cmd(["bash", "-c",
            f"grep -A20 'remnawave-redis:' {REMNAWAVE_DIR}/docker-compose.yml | grep unixsocket || true"])

    uses_socket = "unixsocket" in out.lower()

    if uses_socket:
        # Найти путь к сокету
        ok2, sock_out = run_cmd(["docker", "inspect", "remnawave-redis",
                                  "--format", "{{.Config.Cmd}}"])
        # Извлечь путь из --unixsocket /var/run/valkey/valkey.sock
        import re
        match = re.search(r'--unixsocket\s+(\S+)', sock_out)
        sock_path = match.group(1) if match else "/var/run/valkey/valkey.sock"

        # Исправить .env
        with open(env_path) as f:
            env = f.read()
        env = re.sub(r'REDIS_HOST=.*', 'REDIS_HOST=', env)
        env = re.sub(r'REDIS_PORT=.*', 'REDIS_PORT=', env)
        env = re.sub(r'REDIS_SOCKET=.*', f'REDIS_SOCKET={sock_path}', env)
        with open(env_path, 'w') as f:
            f.write(env)
        logs.append(f"✓ Redis: Unix socket режим → REDIS_SOCKET={sock_path}")
    else:
        logs.append("✓ Redis: TCP режим — .env не требует изменений")
async def step2_restore_db(request: Request, user=Depends(check_auth)):
    """Шаг 2: восстановить БД из выбранного бэкапа."""
    body = await request.json()
    backup_name = body.get("backup_name", "")
    dry_run = body.get("dry_run", False)
    logs = []
    prefix = "[DRY RUN] " if dry_run else ""
    log(f"=== ШАГ 2: {'[DRY RUN] ' if dry_run else ''}Восстановление БД из {backup_name} ===")

    backup_path = Path(BACKUP_ROOT) / backup_name
    if not backup_path.exists():
        raise HTTPException(404, f"Бэкап не найден: {backup_name}")

    # Восстановить .env панели
    panel_env_src = backup_path / "panel.env"
    if panel_env_src.exists():
        if not dry_run:
            shutil.copy(panel_env_src, f"{REMNAWAVE_DIR}/.env")
            # Автоматически исправить Redis конфиг под текущий сервер
            fix_redis_config(f"{REMNAWAVE_DIR}/.env", logs)
        logs.append(f"✓ {prefix}panel.env → {REMNAWAVE_DIR}/.env ({fmt_size(panel_env_src.stat().st_size)})")
    else:
        logs.append("⚠ panel.env не найден в бэкапе")

    # Восстановить .env бота
    bot_env_src = backup_path / "bot.env"
    if bot_env_src.exists():
        if not dry_run:
            shutil.copy(bot_env_src, f"{BOT_DIR}/.env")
        logs.append(f"✓ {prefix}bot.env → {BOT_DIR}/.env ({fmt_size(bot_env_src.stat().st_size)})")
    else:
        logs.append("⚠ bot.env не найден в бэкапе")

    # Проверить наличие SQL файлов
    panel_sql = backup_path / "panel_db.sql"
    bot_sql   = backup_path / "bot_db.sql"
    logs.append(f"✓ panel_db.sql найден ({fmt_size(panel_sql.stat().st_size)})" if panel_sql.exists() else "⚠ panel_db.sql не найден")
    logs.append(f"✓ bot_db.sql найден ({fmt_size(bot_sql.stat().st_size)})"   if bot_sql.exists()   else "⚠ bot_db.sql не найден")

    if dry_run:
        logs.append("✓ [DRY RUN] Все файлы проверены, реальное восстановление не выполнялось")
        for l in logs: log(l)
        return {"ok": True, "logs": logs}

    import time

    # Запустить БД панели
    ok, out = docker_compose_run(REMNAWAVE_DIR, "up", "-d", "remnawave-db", "remnawave-redis")
    logs.append("✓ remnawave-db запущен" if ok else f"✗ Ошибка запуска БД: {out}")
    if not ok:
        for l in logs: log(l)
        return {"ok": False, "logs": logs}

    # Ждём готовности БД
    for i in range(15):
        time.sleep(2)
        ok_ready, _ = run_cmd(["docker", "exec", "remnawave-db", "pg_isready", "-U", "postgres"])
        if ok_ready:
            logs.append("✓ БД готова к подключению")
            break
    else:
        logs.append("✗ БД не ответила за 30 секунд")
        for l in logs: log(l)
        return {"ok": False, "logs": logs}

    # Восстановить БД панели
    if panel_sql.exists():
        run_cmd(["docker", "exec", "remnawave-db", "psql", "-U", "postgres", "-c",
                 "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"])
        ok, out = run_cmd(
            ["docker", "exec", "-i", "remnawave-db", "psql", "-U", "postgres"],
            stdin=open(panel_sql),
        )
        logs.append("✓ БД панели восстановлена" if ok else f"✗ Ошибка БД панели: {out[:200]}")

    # Запустить БД бота
    ok, out = docker_compose_run(BOT_DIR, "up", "-d", "postgres", "redis")
    logs.append("✓ БД бота запущена" if ok else f"⚠ БД бота: {out[:100]}")

    # Восстановить БД бота
    if bot_sql.exists():
        time.sleep(3)
        run_cmd(["docker", "exec", "remnawave_bot_db", "psql", "-U", "remnawave_user", "-d", "remnawave_bot", "-c",
                 "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"])
        ok, out = run_cmd(
            ["docker", "exec", "-i", "remnawave_bot_db", "psql", "-U", "remnawave_user", "-d", "remnawave_bot"],
            stdin=open(bot_sql),
        )
        logs.append("✓ БД бота восстановлена" if ok else f"✗ Ошибка БД бота: {out[:200]}")

    for l in logs:
        log(l)
    return {"ok": True, "logs": logs}


def fix_redis_config(env_path: str, logs: list):
    """Определить конфигурацию Redis и исправить .env если нужно."""
    import re
    ok, out = run_cmd(["docker", "inspect", "remnawave-redis",
                       "--format", "{{.Config.Cmd}}"])
    if not ok:
        ok, out = run_cmd(["bash", "-c",
            f"grep -A20 'remnawave-redis:' {REMNAWAVE_DIR}/docker-compose.yml | grep unixsocket || true"])

    uses_socket = "unixsocket" in out.lower()

    if uses_socket:
        match = re.search(r'--unixsocket\s+(\S+)', out)
        sock_path = match.group(1) if match else "/var/run/valkey/valkey.sock"
        try:
            with open(env_path) as f:
                env = f.read()
            env = re.sub(r'REDIS_HOST=.*', 'REDIS_HOST=', env)
            env = re.sub(r'REDIS_PORT=.*', 'REDIS_PORT=', env)
            env = re.sub(r'REDIS_SOCKET=.*', f'REDIS_SOCKET={sock_path}', env)
            with open(env_path, 'w') as f:
                f.write(env)
            logs.append(f"✓ Redis: Unix socket → REDIS_SOCKET={sock_path}")
        except Exception as e:
            logs.append(f"⚠ Не удалось исправить Redis конфиг: {e}")
    else:
        logs.append("✓ Redis: TCP режим — .env не требует изменений")


@app.get("/api/step/2/stream")
async def step2_stream(backup_name: str, dry_run: str = "false", token: str = "",
                       request: Request = None):
    """SSE stream для шага 2 — восстановление БД в реальном времени."""
    # Проверяем токен из query параметра
    tokens = load_tokens()
    if token not in tokens:
        return HTMLResponse("Unauthorized", status_code=401)

    dry = dry_run.lower() == "true"

    async def generate():
        q = queue.Queue()

        def emit(msg: str):
            q.put(msg)
            log(msg)

        def run_step():
            import time
            prefix = "[DRY RUN] " if dry else ""
            emit(f"=== ШАГ 2: {'[DRY RUN] ' if dry else ''}Восстановление БД из {backup_name} ===")

            backup_path = Path(BACKUP_ROOT) / backup_name
            if not backup_path.exists():
                emit(f"✗ Бэкап не найден: {backup_name}")
                q.put(None); return

            panel_env_src = backup_path / "panel.env"
            if panel_env_src.exists():
                if not dry:
                    shutil.copy(panel_env_src, f"{REMNAWAVE_DIR}/.env")
                    fix_redis_config(f"{REMNAWAVE_DIR}/.env", [])
                emit(f"✓ {prefix}panel.env → {REMNAWAVE_DIR}/.env ({fmt_size(panel_env_src.stat().st_size)})")
            else:
                emit("⚠ panel.env не найден в бэкапе")

            bot_env_src = backup_path / "bot.env"
            if bot_env_src.exists():
                if not dry:
                    shutil.copy(bot_env_src, f"{BOT_DIR}/.env")
                emit(f"✓ {prefix}bot.env → {BOT_DIR}/.env ({fmt_size(bot_env_src.stat().st_size)})")
            else:
                emit("⚠ bot.env не найден в бэкапе")

            panel_sql = backup_path / "panel_db.sql"
            bot_sql   = backup_path / "bot_db.sql"
            emit(f"✓ panel_db.sql найден ({fmt_size(panel_sql.stat().st_size)})" if panel_sql.exists() else "⚠ panel_db.sql не найден")
            emit(f"✓ bot_db.sql найден ({fmt_size(bot_sql.stat().st_size)})" if bot_sql.exists() else "⚠ bot_db.sql не найден")

            if dry:
                emit("✓ [DRY RUN] Все файлы проверены")
                q.put(None); return

            emit("⏳ Запускаю БД панели...")
            ok, out = docker_compose_run(REMNAWAVE_DIR, "up", "-d", "remnawave-db", "remnawave-redis")
            emit("✓ remnawave-db запущен" if ok else f"✗ Ошибка: {out}")
            if not ok: q.put(None); return

            emit("⏳ Жду готовности БД (до 30 сек)...")
            for i in range(15):
                time.sleep(2)
                ok_r, _ = run_cmd(["docker", "exec", "remnawave-db", "pg_isready", "-U", "postgres"])
                if ok_r:
                    emit("✓ БД готова к подключению")
                    break
                emit(f"⏳ Попытка {i+1}/15...")
            else:
                emit("✗ БД не ответила за 30 секунд")
                q.put(None); return

            if panel_sql.exists():
                emit("⏳ Восстанавливаю БД панели...")
                run_cmd(["docker", "exec", "remnawave-db", "psql", "-U", "postgres", "-c",
                         "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"])
                ok, out = run_cmd(["docker", "exec", "-i", "remnawave-db", "psql", "-U", "postgres"],
                                  stdin=open(panel_sql))
                emit("✓ БД панели восстановлена" if ok else f"✗ Ошибка: {out[:200]}")

            emit("⏳ Запускаю БД бота...")
            ok, out = docker_compose_run(BOT_DIR, "up", "-d", "postgres", "redis")
            emit("✓ БД бота запущена" if ok else f"⚠ {out[:100]}")

            if bot_sql.exists():
                time.sleep(3)
                emit("⏳ Восстанавливаю БД бота...")
                run_cmd(["docker", "exec", "remnawave_bot_db", "psql", "-U", "remnawave_user",
                         "-d", "remnawave_bot", "-c",
                         "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"])
                ok, out = run_cmd(["docker", "exec", "-i", "remnawave_bot_db", "psql",
                                   "-U", "remnawave_user", "-d", "remnawave_bot"],
                                  stdin=open(bot_sql))
                emit("✓ БД бота восстановлена" if ok else f"✗ Ошибка: {out[:200]}")

            emit("✓ Шаг 2 завершён")
            q.put(None)

        t = threading.Thread(target=run_step, daemon=True)
        t.start()

        while True:
            try:
                item = q.get(timeout=60)
                if item is None:
                    yield "data: {\"done\": true}\n\n"
                    break
                yield f"data: {json.dumps({'log': item})}\n\n"
            except queue.Empty:
                yield "data: {\"ping\": true}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/step/4/stream")
async def step4_stream(backup_name: str = "", dry_run: str = "false", token: str = "",
                       request: Request = None):
    """SSE stream для шага 4 — запуск стека в реальном времени."""
    tokens = load_tokens()
    if token not in tokens:
        return HTMLResponse("Unauthorized", status_code=401)

    dry = dry_run.lower() == "true"

    async def generate():
        q = queue.Queue()

        def emit(msg: str):
            q.put(msg)
            log(msg)

        def run_step():
            import time as _t
            emit(f"=== ШАГ 4: {'[DRY RUN] ' if dry else ''}Запуск стека ===")

            if dry:
                for label, path in [("Панель", REMNAWAVE_DIR), ("Бот", BOT_DIR), ("Кабинет", CABINET_DIR)]:
                    cf = Path(path) / "docker-compose.yml"
                    emit(f"✓ [DRY RUN] {label}: docker-compose.yml {'найден' if cf.exists() else 'НЕ найден'} в {path}")
                q.put(None); return

            emit("⏳ Запускаю Remnawave панель...")
            ok, out = docker_compose_run(REMNAWAVE_DIR, "up", "-d", timeout=120)
            emit("✓ Remnawave панель запущена" if ok else f"✗ Панель: {out[:200]}")

            emit("⏳ Запускаю бота...")
            ok, out = docker_compose_run(BOT_DIR, "up", "-d", timeout=60)
            if ok:
                emit("✓ Бот запущен, проверяю стабильность (8 сек)...")
                _t.sleep(8)
                ok_s, s_out = run_cmd(["docker", "inspect", "remnawave_bot", "--format", "{{.State.Status}}"])
                if s_out.strip() == "restarting":
                    ok_l, l_out = run_cmd(["docker", "logs", "remnawave_bot", "--tail=30"])
                    if "alembic" in l_out.lower() or "Can't locate revision" in l_out:
                        emit("⚠ Бот упал: несовместимая версия БД. Обновляю исходники из бэкапа...")
                        emit("⏳ Распаковываю bot_src.tar.gz...")
                        logs_tmp = []
                        ok_r = _recover_bot_from_backup(backup_name, logs_tmp)
                        for l in logs_tmp: emit(l)
                        if not ok_r:
                            emit("✗ Не удалось автоматически восстановить бота")
                    else:
                        emit(f"⚠ Бот нестабилен. Проверьте: docker logs remnawave_bot")
                else:
                    emit("✓ Бот стабилен")
            else:
                emit(f"✗ Бот: {out[:200]}")

            emit("⏳ Запускаю кабинет...")
            ok, out = docker_compose_run(CABINET_DIR, "up", "-d", timeout=60)
            emit("✓ Кабинет запущен" if ok else f"⚠ Кабинет: {out[:100]}")

            emit("✓ Шаг 4 завершён")
            q.put(None)

        t = threading.Thread(target=run_step, daemon=True)
        t.start()

        while True:
            try:
                item = q.get(timeout=60)
                if item is None:
                    yield "data: {\"done\": true}\n\n"
                    break
                yield f"data: {json.dumps({'log': item})}\n\n"
            except queue.Empty:
                yield "data: {\"ping\": true}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/step/2/check-db")
async def step2_check_db(user=Depends(check_auth)):
    """Проверить что в БД есть данные."""
    logs = []
    ok, out = run_cmd(["docker", "exec", "remnawave-db", "psql", "-U", "postgres", "-c",
                        "SELECT COUNT(*) FROM users;"])
    if ok and out:
        logs.append(f"✓ Таблица User: {out.splitlines()[-2].strip()} записей")
    else:
        logs.append(f"✗ Ошибка проверки БД: {out[:100]}")
    return {"ok": ok, "logs": logs}

@app.post("/api/step/3/update-dns")
async def step3_update_dns(request: Request, user=Depends(check_auth)):
    """Шаг 3: обновить A-записи в Cloudflare."""
    body    = await request.json()
    dry_run = body.get("dry_run", False)
    cfg     = get_config()
    token   = cfg.get("cf_token", "")
    zone_id = cfg.get("cf_zone_id", "")
    domains = cfg.get("cf_domains", [])
    new_ip  = cfg.get("host_ip", "")
    logs    = []
    log(f"=== ШАГ 3: {'[DRY RUN] ' if dry_run else ''}Переключение DNS → {new_ip} ===")

    if not token:
        raise HTTPException(400, "Cloudflare токен не настроен")
    if not new_ip:
        raise HTTPException(400, "IP этого сервера не задан в настройках")

    # Найти zone_id
    if not zone_id and domains:
        root = domains[0].split(".")[-2] + "." + domains[0].split(".")[-1]
        zone_id = cf_get_zone_id(token, root)
        if zone_id:
            logs.append(f"✓ Zone ID найден: {zone_id}")
            cfg["cf_zone_id"] = zone_id
            save_config(cfg)
        else:
            logs.append("✗ Zone ID не найден")
            for l in logs: log(l)
            return {"ok": False, "logs": logs}

    records = cf_get_records(token, zone_id, domains)
    if not records:
        logs.append("✗ A-записи не найдены")
        for l in logs: log(l)
        return {"ok": False, "logs": logs}

    updated = 0
    for rec in records:
        old_ip = rec["content"]
        if dry_run:
            logs.append(f"✓ [DRY RUN] {rec['name']}: {old_ip} → {new_ip} (не изменено)")
            updated += 1
        else:
            r = cf_update_record(token, zone_id, rec["id"], rec["name"], new_ip)
            if r.get("success"):
                logs.append(f"✓ {rec['name']}: {old_ip} → {new_ip}")
                updated += 1
            else:
                logs.append(f"✗ {rec['name']}: ошибка — {r.get('errors', '')}")

    for l in logs: log(l)
    return {"ok": updated > 0, "logs": logs, "updated": updated}

@app.post("/api/step/3/check-dns")
async def step3_check_dns(user=Depends(check_auth)):
    """Проверить что DNS уже резолвится на новый IP."""
    cfg = get_config()
    domains = cfg.get("cf_domains", [])
    new_ip  = cfg.get("host_ip", "")
    logs = []
    all_ok = True
    for domain in domains:
        ok, out = run_cmd(["dig", "+short", domain, "@1.1.1.1"])
        resolved = out.strip().split("\n")[-1] if out else ""
        if resolved == new_ip:
            logs.append(f"✓ {domain} → {resolved}")
        else:
            logs.append(f"⏳ {domain} → {resolved or '?'} (ожидаем {new_ip})")
            all_ok = False
    return {"ok": all_ok, "logs": logs}

@app.post("/api/step/4/start-stack")
async def step4_start_stack(request: Request, user=Depends(check_auth)):
    """Шаг 4: запустить весь стек панели."""
    body        = await request.json()
    dry_run     = body.get("dry_run", False)
    backup_name = body.get("backup_name", "")
    logs        = []
    log(f"=== ШАГ 4: {'[DRY RUN] ' if dry_run else ''}Запуск стека ===")

    if dry_run:
        for label, path in [("Панель", REMNAWAVE_DIR), ("Бот", BOT_DIR), ("Кабинет", CABINET_DIR)]:
            cf = Path(path) / "docker-compose.yml"
            logs.append(f"✓ [DRY RUN] {label}: docker-compose.yml найден в {path}" if cf.exists()
                        else f"✗ [DRY RUN] {label}: docker-compose.yml НЕ найден в {path}")
        for l in logs: log(l)
        return {"ok": True, "logs": logs}

    # Панель
    ok, out = docker_compose_run(REMNAWAVE_DIR, "up", "-d", timeout=120)
    logs.append("✓ Remnawave панель запущена" if ok else f"✗ Панель: {out[:200]}")

    # Бот — с обработкой alembic ошибки
    import time as _time
    ok, out = docker_compose_run(BOT_DIR, "up", "-d", timeout=60)
    if ok:
        logs.append("✓ Бот запущен, проверяю стабильность...")
        _time.sleep(8)
        ok_s, s_out = run_cmd(["docker", "inspect", "remnawave_bot",
                                "--format", "{{.State.Status}}"])
        if s_out.strip() == "restarting":
            ok_l, l_out = run_cmd(["docker", "logs", "remnawave_bot", "--tail=30"])
            if "alembic" in l_out.lower() or "Can't locate revision" in l_out:
                logs.append("⚠ Бот упал: несовместимая версия БД (alembic). Обновляю исходники из бэкапа...")
                recovered = _recover_bot_from_backup(backup_name, logs)
                if not recovered:
                    logs.append("✗ Не удалось автоматически восстановить бота")
            else:
                logs.append(f"⚠ Бот нестабилен. Проверьте логи: docker logs remnawave_bot")
        else:
            logs.append("✓ Бот стабилен")
    else:
        logs.append(f"✗ Бот: {out[:200]}")

    # Кабинет
    ok, out = docker_compose_run(CABINET_DIR, "up", "-d", timeout=60)
    logs.append("✓ Кабинет запущен" if ok else f"⚠ Кабинет: {out[:100]}")

    for l in logs: log(l)
    return {"ok": True, "logs": logs}


def _recover_bot_from_backup(backup_name: str, logs: list) -> bool:
    """Распаковать исходники бота из бэкапа и пересобрать образ."""
    import shutil as _shutil

    # Найти бэкап с bot_src.tar.gz
    if backup_name:
        backup_path = Path(BACKUP_ROOT) / backup_name
    else:
        candidates = sorted(Path(BACKUP_ROOT).iterdir(), reverse=True)
        backup_path = next((b for b in candidates if (b / "bot_src.tar.gz").exists()), None)

    if not backup_path or not (backup_path / "bot_src.tar.gz").exists():
        logs.append("✗ bot_src.tar.gz не найден в бэкапах")
        return False

    logs.append(f"✓ Распаковываю исходники из {backup_path.name}/bot_src.tar.gz")

    bot_dir = Path(BOT_DIR)
    bot_bak = Path(str(bot_dir) + ".bak")
    if bot_bak.exists():
        _shutil.rmtree(bot_bak)
    if bot_dir.exists():
        bot_dir.rename(bot_bak)

    ok, out = run_cmd(["tar", "-xzf", str(backup_path / "bot_src.tar.gz"),
                       "-C", str(bot_dir.parent)], timeout=60)
    if not ok:
        logs.append(f"✗ Ошибка распаковки: {out[:100]}")
        if bot_bak.exists(): bot_bak.rename(bot_dir)
        return False

    # Восстанавливаем .env
    bot_env_bak = backup_path / "bot.env"
    if bot_env_bak.exists():
        _shutil.copy(bot_env_bak, bot_dir / ".env")
        logs.append("✓ .env бота восстановлен")

    logs.append("✓ Пересобираю образ бота (~1-2 мин)...")
    ok, out = docker_compose_run(BOT_DIR, "build", "--no-cache", timeout=300)
    if not ok:
        logs.append(f"✗ Ошибка сборки: {out[:200]}")
        return False

    logs.append("✓ Образ собран, запускаю бота...")
    ok, out = docker_compose_run(BOT_DIR, "up", "-d", timeout=60)
    logs.append("✓ Бот успешно обновлён и запущен" if ok else f"✗ {out[:100]}")
    return ok


@app.post("/api/step/4/health-check")
async def step4_health_check(user=Depends(check_auth)):
    """Проверить health панели."""
    logs = []
    import time
    time.sleep(5)

    # Получить IP контейнера remnawave
    ok_ip, ip_out = run_cmd(["docker", "inspect", "remnawave",
                              "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"])
    panel_ip = ip_out.strip().split("\n")[0] if ok_ip and ip_out.strip() else "localhost"

    ok, out = run_cmd(["curl", "-sf", f"http://{panel_ip}:3001/health"])
    logs.append(f"✓ Remnawave API отвечает ({panel_ip}:3001)" if ok else f"✗ API не отвечает: {out[:100]}")

    ok2, out2 = run_cmd(["docker", "ps", "--filter", "name=remnawave", "--format", "{{.Names}}: {{.Status}}"])
    for line in out2.splitlines():
        logs.append(f"  {line}")

    return {"ok": ok, "logs": logs}


@app.get("/api/log")
async def get_log_api(user=Depends(check_auth)):
    return {"log": get_log()}


# ── Branding endpoints ────────────────────────────────────────────────────────
from fastapi import UploadFile, File, Form
from fastapi.responses import FileResponse, Response
import mimetypes

@app.get("/api/logo")
async def get_logo_restore():
    logo = get_logo_path()
    if not logo:
        raise HTTPException(404, "No logo")
    content = Path(logo).read_bytes()
    mt = mimetypes.guess_type(logo)[0] or "image/png"
    return Response(content=content, media_type=mt,
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/favicon.ico")
async def favicon_restore():
    logo = get_logo_path()
    if not logo:
        raise HTTPException(404, "No favicon")
    content = Path(logo).read_bytes()
    mt = mimetypes.guess_type(logo)[0] or "image/png"
    return Response(content=content, media_type=mt,
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.post("/api/branding")
async def save_branding_restore(
    brand_name: str = Form(""),
    logo_bg:    str = Form(""),
    server_alias: str = Form(""),
    logo: UploadFile = File(None),
    user=Depends(check_auth),
):
    cfg = get_config()
    if brand_name:    cfg["brand_name"]    = brand_name.strip()
    if logo_bg:       cfg["logo_bg"]       = logo_bg
    if server_alias is not None: cfg["server_alias"] = server_alias.strip()
    save_config(cfg)
    if logo and logo.filename:
        ext = Path(logo.filename).suffix.lower()
        if ext not in [".png", ".jpg", ".jpeg", ".svg", ".webp"]:
            raise HTTPException(400, "Допустимые форматы: PNG, JPG, SVG, WEBP")
        for old_ext in [".png", ".svg", ".jpg", ".jpeg", ".webp"]:
            old = f"{LOGO_FILE}{old_ext}"
            if os.path.exists(old): os.remove(old)
        content = await logo.read()
        with open(f"{LOGO_FILE}{ext}", "wb") as f: f.write(content)
    return {"status": "ok", "brand_name": cfg.get("brand_name","ВЛЕС"), "has_logo": get_logo_path() is not None}

@app.delete("/api/branding/logo")
async def delete_logo_restore(user=Depends(check_auth)):
    for ext in [".png", ".svg", ".jpg", ".jpeg", ".webp"]:
        p = f"{LOGO_FILE}{ext}"
        if os.path.exists(p): os.remove(p)
    return {"status": "ok"}
