#!/usr/bin/env bash
# One-time Project Zomboid dedicated-server deployment.
# Run as root on Ubuntu/Debian with systemd.
set -uo pipefail
umask 027

LOG=/root/deploy.log
exec >>"$LOG" 2>&1

DONE=()
SKIPPED=()
FAILED=()

ok() { DONE+=("$1"); printf '[OK] %s\n' "$1"; }
skip() { SKIPPED+=("$1"); printf '[SKIP] %s\n' "$1"; }
fail() { FAILED+=("$1"); printf '[FAIL] %s\n' "$1"; }
run() {
    local name=$1; shift
    printf '\n>>> %s\n' "$name"
    if "$@"; then ok "$name"; return 0; else local rc=$?; fail "$name (код $rc)"; return "$rc"; fi
}
report() {
    local rc=$?
    trap - EXIT
    printf '\n========== ИТОГ ==========\n'
    printf '\nВыполнено (%s):\n' "${#DONE[@]}"
    if ((${#DONE[@]})); then printf '  + %s\n' "${DONE[@]}"; else printf '  (нет)\n'; fi
    printf '\nПропущено (%s):\n' "${#SKIPPED[@]}"
    if ((${#SKIPPED[@]})); then printf '  - %s\n' "${SKIPPED[@]}"; else printf '  (нет)\n'; fi
    printf '\nНе удалось (%s):\n' "${#FAILED[@]}"
    if ((${#FAILED[@]})); then printf '  ! %s\n' "${FAILED[@]}"; else printf '  (нет)\n'; fi
    printf '\nЛог: %s\n' "$LOG"
    if ((rc != 0 || ${#FAILED[@]} > 0)); then exit 1; else exit 0; fi
}
trap report EXIT

export DEBIAN_FRONTEND=noninteractive
if (( EUID != 0 )); then fail 'Требуются права root'; exit 1; fi
if ! command -v apt-get >/dev/null 2>&1 || ! command -v systemctl >/dev/null 2>&1; then
    fail 'Нужны Debian/Ubuntu с apt и systemd'; exit 1
fi

# Keep these passwords as requested; users must change them immediately.
if run 'Обновление индексов APT' apt-get update; then
    run 'Обновление пакетов ОС' apt-get upgrade -y || true
else
    skip 'Обновление пакетов ОС: нет актуальных индексов'
fi

if dpkg --print-foreign-architectures | grep -qx i386; then
    skip 'Архитектура i386 уже подключена'
else
    run 'Подключение i386' dpkg --add-architecture i386 || true
fi
if run 'Обновление индексов после i386' apt-get update; then
    PKGS=(ca-certificates curl wget tar gzip sudo screen sqlite3 python3 acl lib32gcc-s1 lib32stdc++6 ufw fail2ban)
    if ! run 'Установка зависимостей' apt-get install -y "${PKGS[@]}"; then
        fail 'Зависимости недоступны, дальнейший деплой остановлен'; exit 1
    fi
else
    fail 'APT недоступен, дальнейший деплой остановлен'; exit 1
fi

for username in pidor1 pidor2; do
    if id "$username" >/dev/null 2>&1; then
        skip "Пользователь $username уже существует; пароль не менялся"
    elif useradd -m -s /bin/bash "$username" && printf '%s:%s\n' "$username" qwerty123 | chpasswd; then
        ok "Пользователь $username создан с временным паролем"
    else
        fail "Создание пользователя $username"
    fi
    if id "$username" >/dev/null 2>&1; then
        run "Группа sudo: $username" usermod -aG sudo "$username" || true
    fi
done
if id zomboid >/dev/null 2>&1; then
    skip 'Пользователь zomboid уже существует'
else
    if run 'Создание системного пользователя zomboid' useradd -m -s /bin/bash zomboid; then
        run 'Блокировка пароля zomboid' passwd -l zomboid || true
    fi
fi
if ! id zomboid >/dev/null 2>&1; then fail 'Нет пользователя zomboid'; exit 1; fi

if swapon --noheadings --show=NAME | grep -Fxq /swapfile; then
    skip 'Swap /swapfile уже активен'
elif [[ -e /swapfile ]]; then
    if run 'Активация существующего swap' swapon /swapfile; then
        if ! grep -Eq '^[[:space:]]*/swapfile[[:space:]]' /etc/fstab; then
            printf '/swapfile none swap sw 0 0\n' >> /etc/fstab
            ok 'Запись swap в fstab'
        fi
    fi
else
    free_kb=$(df -Pk / | awk 'NR==2 {print $4}')
    if [[ "$free_kb" =~ ^[0-9]+$ ]] && (( free_kb >= 10 * 1024 * 1024 )); then
        if fallocate -l 8G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile; then
            ok 'Swap 8 ГБ создан и активирован'
            if ! grep -Eq '^[[:space:]]*/swapfile[[:space:]]' /etc/fstab; then
                printf '/swapfile none swap sw 0 0\n' >> /etc/fstab
                ok 'Swap добавлен в fstab'
            fi
        else
            fail 'Создание swap'
        fi
    else
        skip 'Swap: менее 10 ГБ свободного места на /'
    fi
fi

# Allow configured SSH ports before enabling UFW to avoid lockout.
SSH_PORTS=$(sshd -T 2>/dev/null | awk '$1=="port" {print $2}' | sort -un || true)
if [[ -z "$SSH_PORTS" ]]; then
    SSH_PORTS=$(awk 'tolower($1)=="port" {print $2}' /etc/ssh/sshd_config 2>/dev/null | sort -un || true)
fi
if [[ -z "$SSH_PORTS" ]]; then SSH_PORTS=22; fi
for port in $SSH_PORTS; do
    if [[ "$port" =~ ^[0-9]+$ ]] && ((port >= 1 && port <= 65535)); then
        run "UFW: SSH TCP/$port" ufw allow "$port/tcp" || true
    else
        fail "Некорректный SSH-порт: $port"
    fi
done
run 'UFW: Zomboid UDP 16261–16262' ufw allow 16261:16262/udp || true
# Never enable firewall if SSH rules were not applied.
if ((${#FAILED[@]} == 0)); then
    run 'Включение UFW' ufw --force enable || true
else
    skip 'Включение UFW: имеются ошибки предварительной настройки'
fi

cat > /etc/fail2ban/jail.d/90-zomboid-deploy.local <<'EOF'
[DEFAULT]
banaction = ufw
bantime = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
EOF
ok 'Конфигурация Fail2ban записана'
run 'Автозапуск Fail2ban' systemctl enable fail2ban || true
run 'Перезапуск Fail2ban' systemctl restart fail2ban || true
if systemctl is-active --quiet fail2ban; then ok 'Fail2ban активен'; else fail 'Fail2ban не активен'; fi

install -d -o zomboid -g zomboid -m 0750 /home/zomboid/steamcmd /home/zomboid/pzserver
ok 'Каталоги SteamCMD и сервера подготовлены'

if [[ -f /home/zomboid/steamcmd/steamcmd.sh ]]; then
    skip 'SteamCMD уже распакован'
else
    STEAM_ARCHIVE=/home/zomboid/steamcmd/steamcmd_linux.tar.gz
    DL_OK=0
    for attempt in 1 2 3 4 5; do
        if runuser -u zomboid -- wget -q --tries=3 --timeout=30 \
            https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz \
            -O "$STEAM_ARCHIVE" && tar -tzf "$STEAM_ARCHIVE" >/dev/null 2>&1; then
            DL_OK=1; break
        fi
        sleep 5
    done
    if (( DL_OK )); then
        if runuser -u zomboid -- tar -xzf "$STEAM_ARCHIVE" -C /home/zomboid/steamcmd; then
            ok 'SteamCMD скачан и распакован'
        else
            fail 'Распаковка SteamCMD'
        fi
    else
        fail 'Скачивание SteamCMD после 5 попыток'
    fi
    rm -f "$STEAM_ARCHIVE"
fi

STEAMCMD=/home/zomboid/steamcmd/steamcmd.sh
SERVER_DIR=/home/zomboid/pzserver
if [[ ! -f "$STEAMCMD" ]]; then
    fail 'SteamCMD отсутствует; установка игры пропущена'
    exit 1
fi
chmod +x "$STEAMCMD"

STEAM_OK=0
for attempt in 1 2 3 4 5; do
    printf '\n>>> SteamCMD: попытка %s/5\n' "$attempt"
    if runuser -u zomboid -- "$STEAMCMD" \
        +force_install_dir "$SERVER_DIR" \
        +login anonymous +app_update 380870 validate +quit; then
        if [[ -s "$SERVER_DIR/start-server.sh" && -s "$SERVER_DIR/ProjectZomboid64.json" ]]; then
            STEAM_OK=1; break
        fi
        printf 'SteamCMD вернул 0, но ожидаемые файлы отсутствуют\n'
    fi
    sleep 10
done
if (( ! STEAM_OK )); then
    fail 'Загрузка Project Zomboid (5 попыток)'; exit 1
fi
ok 'Файлы Project Zomboid загружены через публичную ветку Steam'
chmod +x "$SERVER_DIR/start-server.sh"

# Heap по реальному объёму RAM: 16 GB -> 10 GB, 32 GB -> 20 GB.
RAM_MB=$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo)
if (( RAM_MB >= 30000 )); then
    HEAP_GB=20
elif (( RAM_MB >= 15000 )); then
    HEAP_GB=10
elif (( RAM_MB >= 7500 )); then
    HEAP_GB=4
else
    HEAP_GB=2
fi
export HEAP_GB

JSON_FILE="$SERVER_DIR/ProjectZomboid64.json"
[[ -f "$JSON_FILE.before-heap" ]] || cp -p "$JSON_FILE" "$JSON_FILE.before-heap"
if python3 - "$JSON_FILE" <<'PY'
import json, os, pathlib, re, sys
p = pathlib.Path(sys.argv[1])
data = json.loads(p.read_text())
if not isinstance(data, dict) or not isinstance(data.get('vmArgs'), list):
    raise ValueError('Не найден массив vmArgs в ProjectZomboid64.json')
newargs = [a for a in data['vmArgs']
           if not (isinstance(a, str) and re.fullmatch(r'-Xm[sx]\d+[gGmMkK]', a))]
newargs.extend(['-Xms2g', '-Xmx' + os.environ['HEAP_GB'] + 'g'])
data['vmArgs'] = newargs
tmp = p.with_name(p.name + '.tmp')
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
json.loads(tmp.read_text())      # проверка до подмены оригинала
os.replace(tmp, p)
PY
then
    chown zomboid:zomboid "$JSON_FILE" "$JSON_FILE.before-heap"
    ok "Java heap: Xms=2G, Xmx=${HEAP_GB}G"
else
    fail 'Настройка Java heap / проверка JSON'
fi

# Каталог данных (мир, конфиги, логи)
install -d -o zomboid -g zomboid -m 755 /home/zomboid/Zomboid
ok 'Каталог данных /home/zomboid/Zomboid готов'

# Доступ операторов к данным Project Zomboid
if setfacl -m u:pidor1:--x,u:pidor2:--x /home/zomboid \
   && setfacl -R -m u:pidor1:rwX,u:pidor2:rwX /home/zomboid/Zomboid \
   && find /home/zomboid/Zomboid -type d \
      -exec setfacl -m d:u:pidor1:rwx,d:u:pidor2:rwx {} +; then
    ok 'Доступ pidor1 и pidor2 к данным Zomboid настроен'
else
    fail 'Настройка ACL для операторов Zomboid'
fi

# Пароль админа: из окружения PZ_ADMIN_PASSWORD или случайный; хранится только у root
ENV_DIR=/etc/zomboid
ENV_FILE="$ENV_DIR/zomboid.env"
install -d -m 700 "$ENV_DIR"
if [[ -s "$ENV_FILE" ]]; then
    ok "Пароль админа уже задан ($ENV_FILE), не меняю"
else
    PZ_ADMIN_PASSWORD=${PZ_ADMIN_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_hex(12))')}
    install -m 600 /dev/null "$ENV_FILE"
    printf 'PZ_ADMIN_PASSWORD=%s\n' "$PZ_ADMIN_PASSWORD" > "$ENV_FILE"
    unset PZ_ADMIN_PASSWORD
    ok "Пароль админа сохранён в $ENV_FILE (chmod 600)"
fi

if cat > /etc/systemd/system/zomboid.service <<'EOF'
[Unit]
Description=Project Zomboid Dedicated Server
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=zomboid
Group=zomboid
WorkingDirectory=/home/zomboid/pzserver
EnvironmentFile=/etc/zomboid/zomboid.env
ExecStart=/home/zomboid/pzserver/start-server.sh -servername servertest -adminpassword ${PZ_ADMIN_PASSWORD}
Restart=on-failure
RestartSec=15
LimitNOFILE=65536
TimeoutStopSec=180

[Install]
WantedBy=multi-user.target
EOF
then
    ok 'Unit-файл systemd записан'
else
    fail 'Запись unit-файла systemd'
fi

if run 'Перезагрузка конфигурации systemd' systemctl daemon-reload; then
    run 'Автозапуск Zomboid включён (без запуска сейчас)' systemctl enable zomboid.service || true
    run 'Проверка unit-файла' systemd-analyze verify /etc/systemd/system/zomboid.service || true
else
    skip 'Включение автозапуска Zomboid: ошибка daemon-reload'
fi

# Firewall: только если ufw активен
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
    run 'ufw: порты PZ (UDP 16261, 16262-16272)' bash -c 'ufw allow 16261/udp && ufw allow 16262:16272/udp' || true
else
    skip 'ufw не активен: убедитесь, что UDP 16261 и 16262 открыты у хостера'
fi

if systemctl is-active --quiet zomboid.service; then
    fail 'Zomboid неожиданно активен; деплой не должен запускать сервер'
else
    ok 'Zomboid не запущен, готов к запуску'
fi

if [[ -f "$SERVER_DIR/steamapps/appmanifest_380870.acf" ]]; then
    awk '/"buildid"/ {gsub(/"/, "", $2); print "Steam Build ID: " $2; exit}' \
        "$SERVER_DIR/steamapps/appmanifest_380870.acf"
fi
printf 'Профиль запуска: servertest; каталог данных: /home/zomboid/Zomboid\n'
printf 'Запуск:  systemctl start zomboid\n'
printf 'Логи:    journalctl -u zomboid -f   (ждите строку "SERVER STARTED")\n'
printf 'Пароль админа: %s\n' "$ENV_FILE"
