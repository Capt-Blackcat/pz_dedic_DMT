# Warning: 100% human-debugged AI slop. Tested on real servers.
---

# Project Zomboid Dedicated: deploy / import / export
Четыре скрипта: `deploy_zomboid.sh` (установка VPS), `pz_import.py` (полный/частичный импорт), `pz_export.py` (бэкап мира и игроков), `pz_start.py` (запуск мира). Примеры ниже для сервера ivent: замени имя на своё. Все команды запускаются через sudo.
## Быстрый запуск через GitHub
Команды предполагают, что все четыре файла загружены в корень ветки main репозитория `Capt-Blackcat/pz_dedic_DMT`.

### Деплой VPS в поле `Post-install скрипт` при заказе:
```
#!/bin/sh
apt-get update && apt-get install -y curl
curl -fsSL https://raw.githubusercontent.com/Capt-Blackcat/pz_dedic_DMT/refs/heads/main/deploy_zomboid.sh -o /tmp/deploy_zomboid.sh && bash /tmp/deploy_zomboid.sh
```
---
# 1. Сразу после завершения деплоя обязательно проверь результат отдельной командой:
```
sudo tail -n 60 /root/deploy.log
```

В конце лога должен быть блок ИТОГ: проверь, что в Не удалось указано (нет). Деплой перенаправляет вывод в /root/deploy.log, поэтому отсутствие текста в терминале не означает ошибку или успех. Сервер игры после деплоя не запускается автоматически; zomboid.service предназначен только для servertest.

# 2. Импорт / восстановление мира:
Cначала загрузить архив через WinSCP в /home/zomboid/import/ (там должен быть ровно один архив), затем:
```
curl -fsSL https://raw.githubusercontent.com/Capt-Blackcat/pz_dedic_DMT/refs/heads/main/pz_import.py -o /tmp/pz_import.py && sudo python3 /tmp/pz_import.py --name ivent
```
Имя `--name` указывает, **какому серверу принадлежат файлы внутри архива**. Имя самого архива может быть любым (например, `vzaperti.zip` или `players-only.zip`). В `/home/zomboid/import/` должен находиться ровно один архив.

**Частичный импорт поддерживается.** Можно передать только одну или несколько частей: `Server/<имя>.ini`, `Server/<имя>_*.lua`, `db/<имя>.db` (с sidecar-файлами при наличии), `Saves/Multiplayer/<имя>/`, `Saves/Multiplayer/<имя>_player/`. Также допускается общая внешняя папка `vzaperti/` и архивирование одной папки `vzaperti/` либо `vzaperti_player/`.

По умолчанию файлы из архива **добавляются или заменяют одноимённые**. Остальные файлы, настройки и миры сохраняются. Если требуется **полностью заменить папки сохранений, которые есть в архиве**, добавь флаг `--replace-saves`:

```bash
sudo python3 /tmp/pz_import.py --name ivent --replace-saves
```

Перед заменой существующих компонентов создаётся бэкап в `/home/zomboid/backup/`; при ошибке импортёр пытается откатить **только затронутые компоненты**. При замене `.db` старые `-wal/-shm/-journal`, отсутствующие в архиве, удаляются после бэкапа. После успеха удаляется только использованный архив, другие файлы в `import/` не затрагиваются.

---
# 3. Экспорт мира в бэкап:
```
curl -fsSL https://raw.githubusercontent.com/Capt-Blackcat/pz_dedic_DMT/refs/heads/main/pz_export.py -o /tmp/pz_export.py && sudo python3 /tmp/pz_export.py --name ivent
```
---
# Запуск

### При наличии Viewpoint перед первым запуском выполнить:
```bash
sudo curl -fL https://github.com/zed-0xff/ZombieBuddy/releases/download/v2.3.4/ZombieBuddy.jar -o /home/zomboid/pzserver/ZombieBuddy.jar && sudo chown zomboid:zomboid /home/zomboid/pzserver/ZombieBuddy.jar && sudo chmod 644 /home/zomboid/pzserver/ZombieBuddy.jar && sudo python3 -c 'import json,pathlib,shutil,os; p=pathlib.Path("/home/zomboid/pzserver/ProjectZomboid64.json"); d=json.loads(p.read_text()); a=d["vmArgs"]; assert isinstance(a,list), "vmArgs must be a list"; agent="-javaagent:ZombieBuddy.jar"; a.append(agent) if agent not in a else None; b=p.with_name(p.name+".before-zombiebuddy"); shutil.copy2(p,b) if not b.exists() else None; t=p.with_name(p.name+".tmp-zombiebuddy"); t.write_text(json.dumps(d,ensure_ascii=False,indent=2)+"\n"); os.chmod(t,p.stat().st_mode); os.chown(t,p.stat().st_uid,p.stat().st_gid); os.replace(t,p)'
```

### Первичный запуск сервера PZ (деплой или свежий импорт):
```
curl -fsSL https://raw.githubusercontent.com/Capt-Blackcat/pz_dedic_DMT/refs/heads/main/pz_start.py -o /tmp/pz_start.py && sudo python3 /tmp/pz_start.py --name ivent
```

### Последующие запуски:
```
sudo python3 /tmp/pz_start.py --name ivent
```
### Перевод в фон:
```
Ctrl + A, затем D
```
### Возвращение к живой консоли:
```
sudo -u zomboid screen -r ivent
```
### Проверка фоновой сессии:
```
sudo -u zomboid screen -ls
```

---
### Где что лежит
```
/home/zomboid/
├── pzserver/                   # Серверные файлы и start-server.sh
├── Zomboid/
│   ├── Server/                # ivent.ini, ivent_SandboxVars.lua и другие конфиги
│   ├── db/                    # ivent.db (учётные записи / права)
│   └── Saves/Multiplayer/     # ivent/ (мир, players.db и прочие файлы)
├── import/                    # Один входящий архив; очищается после успешного импорта
└── backup/                    # Архивы экспорта и бэкапы перед заменой; сохраняются
```
В архиве сохраняется относительная структура Server/, db/, Saves/Multiplayer/. Для импорта все три части обязательны. Поддержка частичного переноса будет позже при необходимости. Формат экспорта: .tar.gz; ZIP/TAR/RAR/7Z в импортёре предусмотрены при наличии внешних утилит для RAR/7Z. Экспорт включает `Saves/Multiplayer/<имя>_player/`, если эта папка существует.
Что делают скрипты
- Deploy: зависимости, пользователи zomboid, pidor1, pidor2, ACL, SteamCMD, сервер, UFW/Fail2ban, каталоги import и backup. Создаёт zomboid.service только для servertest; автоматически сервер не запускает. Лог: /root/deploy.log.
- Start: запускает выбранный мир через GNU Screen и управляет его UDP-портами.
- Export: находит данные по --name; если сервер запущен, спрашивает y/n на остановку; проверяет SQLite, создаёт и проверяет архив в /home/zomboid/backup/. Другие бэкапы не трогает. Временный .part удаляет. Сервер не перезапускает.
- Import: принимает полные и частичные архивы, включая `_player`; спрашивает подтверждение остановки, сохраняет затронутые данные, объединяет или заменяет выбранные файлы и при ошибке пытается откатить изменения. После успеха удаляет использованный архив. Сервер не перезапускает.
При отказе n операция отменяется. quit/systemd используется перед резервным SIGTERM; чужие миры останавливать нельзя.

---
Остановка сервера в импорте/экспорте требует sudo; после выполнения запуск вручную. systemctl start zomboid запускает только servertest, не ivent. После первого деплоя смени временные пароли операторов: sudo passwd pidor1 и sudo passwd pidor2. Перед импортом сохрани исходный архив: после успешного выполнения импортёр удалит его из `import/`.
