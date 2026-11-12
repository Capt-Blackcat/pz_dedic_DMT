# Project Zomboid Dedicated: deploy / import / export
Три скрипта: `deploy_zomboid.sh` (установка VPS), `pz_import.py` (восстановление/перенос мира), `pz_export.py` (бэкап мира). Примеры ниже для сервера ivent: замени имя на своё. Все команды запускаются через sudo.
Быстрый запуск через GitHub
Команды предполагают, что все три файла загружены в корень ветки main репозитория `Capt-Blackcat/pz_deploy_script_DMT`.

### Деплой VPS в поле `Post-install скрипт` при заказе:
```
#!/bin/sh
apt-get update && apt-get install -y curl
curl -fsSL https://raw.githubusercontent.com/Capt-Blackcat/pz_deploy_script_DMT/refs/heads/main/deploy_zomboid.sh -o /tmp/deploy_zomboid.sh && bash /tmp/deploy_zomboid.sh
```
---
## 1. Сразу после завершения деплоя обязательно проверь результат отдельной командой:
```
sudo tail -n 60 /root/deploy.log
```

В конце лога должен быть блок ИТОГ: проверь, что в Не удалось указано (нет). Деплой перенаправляет вывод в /root/deploy.log, поэтому отсутствие текста в терминале не означает ошибку или успех. Сервер игры после деплоя не запускается автоматически; zomboid.service предназначен только для servertest.

# 2. Импорт / восстановление мира:
Cначала загрузить архив через WinSCP в /home/zomboid/import/ (там должен быть ровно один архив), затем:
```
curl -fsSL https://raw.githubusercontent.com/Capt-Blackcat/pz_deploy_script_DMT/refs/heads/main/pz_import.py -o /tmp/pz_import.py && sudo python3 /tmp/pz_import.py --name ivent
```
Имя --name должно совпадать с именем мира внутри архива (ivent.ini, ivent.db, Multiplayer/ivent). Название архива может содержать дату, например `ivent_2026-11-11_14-30-00.tar.gz`.

---
## 3. Экспорт мира в бэкап:
```
curl -fsSL https://raw.githubusercontent.com/Capt-Blackcat/pz_deploy_script_DMT/refs/heads/main/pz_export.py -o /tmp/pz_export.py && sudo python3 /tmp/pz_export.py --name ivent
```
---

# Запуск
### Первичный запуск сервера PZ (деплой или свежий импорт):
```
curl -fsSL https://raw.githubusercontent.com/Capt-Blackcat/pz_deploy_script_DMT/refs/heads/main/pz_start.py -o /tmp/pz_start.py && sudo python3 /tmp/pz_start.py --name ivent
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
В архиве сохраняется относительная структура Server/, db/, Saves/Multiplayer/. Для импорта все три части обязательны. Поддержка частичного переноса будет позже при необходимости. Формат экспорта: .tar.gz; ZIP/TAR/RAR/7Z в импортёре предусмотрены при наличии внешних утилит для RAR/7Z.
Что делают скрипты
- Deploy: зависимости, пользователи zomboid, pidor1, pidor2, ACL, SteamCMD, сервер, UFW/Fail2ban, каталоги import и backup. Создаёт zomboid.service только для servertest; автоматически сервер не запускает. Лог: /root/deploy.log.
- Export: находит данные по --name; если сервер запущен, спрашивает y/n на остановку; проверяет SQLite, создаёт и проверяет архив в /home/zomboid/backup/. Другие бэкапы не трогает. Временный .part удаляет. Сервер не перезапускает.
- Import: ищет архив в import, распаковывает и проверяет состав; при необходимости спрашивает y/n на остановку сервера; сохраняет предыдущую версию в backup, удаляет старые файлы только выбранного мира, устанавливает новую версию, назначает права. При ошибке после начала замены пытается откатить изменения. После успеха очищает import/. Сервер не перезапускает.
При отказе n операция отменяется. quit/systemd используется перед резервным SIGTERM; чужие миры останавливать нельзя.

---
Остановка сервера в импорте/экспорте требует sudo; после выполнения запуск вручную. systemctl start zomboid запускает только servertest, не ivent. После первого деплоя смени временные пароли операторов: sudo passwd pidor1 и sudo passwd pidor2. Не запускай импорт при наличии других нужных файлов в import/: после успешного импорта каталог очищается целиком.
