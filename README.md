# pz_deploy_script_DMT

Скрипт разового развёртывания выделенного сервера Project Zomboid на чистом VPS.

## Требования
- Debian или Ubuntu с systemd, запуск от root
- Не менее 8 ГБ ОЗУ (рекомендуется 16 ГБ и больше), ~10 ГБ свободного места для swap и ~15 ГБ под игру
- Доступ в интернет (APT, SteamCMD, GitHub)

## Что делает скрипт
- Обновляет систему, ставит зависимости (SteamCMD, Java-библиотеки, ufw, fail2ban)
- Создаёт системного пользователя `zomboid` и дополнительных пользователей с sudo
- Создаёт swap 8 ГБ, если его нет
- Настраивает UFW (SSH и порты PZ: UDP 16261–16272) и Fail2ban
- Скачивает сервер PZ (app 380870) через SteamCMD
- Выставляет Java heap по объёму ОЗУ (16 ГБ → 10 ГБ, 32 ГБ → 20 ГБ)
- Создаёт systemd-сервис `zomboid` (автозапуск включён, **сервер не запускается**)
- Генерирует пароль админа PZ и сохраняет его в `/etc/zomboid/zomboid.env` (права 600)

## Использование
В поле post-deploy скрипта хостера:

```bash
#!/bin/bash
curl -fsSL --retry 5 --retry-delay 5 https://raw.githubusercontent.com/Capt-Blackcat/pz_deploy_script_DMT/ХЕШ_КОММИТА/deploy_zomboid.sh -o /root/deploy_zomboid.sh \
  && bash /root/deploy_zomboid.sh \
  || echo "$(date) загрузчик: не удалось скачать или выполнить скрипт" >> /root/deploy.log
```

Или вручную: `bash deploy_zomboid.sh` от root. Своё значение пароля админа
можно задать так: `PZ_ADMIN_PASSWORD=... bash deploy_zomboid.sh`.

## После деплоя
1. **Сразу смените пароли** созданных пользователей (`passwd ИМЯ`).
2. Проверьте итог: `cat /root/deploy.log` (разделы «Выполнено / Пропущено / Не удалось»).
3. Запуск: `systemctl start zomboid`
4. Логи: `journalctl -u zomboid -f` (ждите строку `SERVER STARTED`)
5. Вход админа: логин `admin`, пароль в `/etc/zomboid/zomboid.env`

## Перенос бэкапа мира
Остановите сервер (`systemctl stop zomboid`), затем замените в
`/home/zomboid/Zomboid` содержимое `Server/servertest*` и
`Saves/Multiplayer/servertest` данными из бэкапа и выдайте права:
`chown -R zomboid:zomboid /home/zomboid/Zomboid`.

## Пути
| Что | Где |
|---|---|
| Лог деплоя | `/root/deploy.log` |
| Сервер | `/home/zomboid/pzserver` |
| Данные, мир, конфиги | `/home/zomboid/Zomboid` |
| Unit-файл | `/etc/systemd/system/zomboid.service` |
| Пароль админа PZ | `/etc/zomboid/zomboid.env` |
| Оригинал JVM-настроек | `/home/zomboid/pzserver/ProjectZomboid64.json.before-heap` |

## Повторный запуск
Скрипт идемпотентен: существующие пользователи, swap, SteamCMD и пароль админа
не перезаписываются. Повторный запуск обновит файлы игры через `validate`
и заново применит настройки heap.
