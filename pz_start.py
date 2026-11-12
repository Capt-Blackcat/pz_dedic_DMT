#!/usr/bin/env python3
"""Start a named Project Zomboid world in GNU screen with persistent, non-conflicting UDP ports.

Usage: sudo python3 pz_start.py --name ivent
Attach: sudo -u zomboid screen -r ivent
Detach: Ctrl+A, D

The deploy-managed servertest service is deliberately left untouched.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path('/home/zomboid')
SERVER = HOME / 'pzserver' / 'start-server.sh'
CONFIGS = HOME / 'Zomboid' / 'Server'
PORTS_DIR = Path('/etc/zomboid/ports')
BASE_PORTS = (16261, 16262)  # reserved for the deployment's servertest service
# Use 10-port gaps to avoid packing unrelated game/Steam traffic too tightly.
CANDIDATES = range(16271, 16992, 10)


class StartError(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise StartError(message)


def cmd(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(args, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise StartError(f'Не удалось выполнить {args[0]}: {exc}') from exc
    if check and proc.returncode:
        raise StartError(f'{args[0]} завершился с кодом {proc.returncode}: '
                         f'{(proc.stderr or proc.stdout).strip()}')
    return proc


def valid_port(value: object, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise StartError(f'Неверный порт {label}: {value!r}') from exc
    require(1024 <= number <= 65534, f'Порт {label} вне диапазона 1024–65534: {number}')
    return number


def read_ini(path: Path) -> tuple[str, dict[str, str]]:
    require(not path.is_symlink(), f'Символическая ссылка вместо конфигурации: {path}')
    if not path.exists():
        return '', {}
    require(path.is_file(), f'Необычный тип конфигурации: {path}')
    try:
        contents = path.read_text(encoding='utf-8-sig')
    except UnicodeError as exc:
        raise StartError(f'Не удалось прочесть UTF-8 конфигурацию {path}: {exc}') from exc
    values: dict[str, str] = {}
    for line in contents.splitlines():
        match = re.match(r'^\s*(DefaultPort|UDPPort)\s*=\s*([^#;\r\n]*)', line)
        if match:
            require(match.group(1) not in values, f'Повтор параметра {match.group(1)}: {path}')
            values[match.group(1)] = match.group(2).strip()
    return contents, values


def ini_ports(path: Path) -> tuple[int, int]:
    _, settings = read_ini(path)
    return (valid_port(settings.get('DefaultPort', BASE_PORTS[0]), str(path) + ':DefaultPort'),
            valid_port(settings.get('UDPPort', BASE_PORTS[1]), str(path) + ':UDPPort'))


def saved_ports(name: str) -> tuple[int, int] | None:
    path = PORTS_DIR / f'{name}.json'
    require(not path.is_symlink(), f'Ссылка вместо записи о портах: {path}')
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise StartError(f'Не удалось прочитать {path}: {exc}') from exc
    require(isinstance(data, dict), f'Некорректная запись о портах: {path}')
    return (valid_port(data.get('DefaultPort'), str(path) + ':DefaultPort'),
            valid_port(data.get('UDPPort'), str(path) + ':UDPPort'))


def reserved_ports(name: str) -> set[int]:
    reserved = set(BASE_PORTS)
    if CONFIGS.is_dir():
        for path in CONFIGS.glob('*.ini'):
            if path.stem != name:
                reserved.update(ini_ports(path))
    if PORTS_DIR.is_dir():
        for path in PORTS_DIR.glob('*.json'):
            if path.stem != name:
                reserved.update(saved_ports(path.stem) or ())
    return reserved


def listening_udp_ports() -> set[int]:
    """Read bound IPv4/IPv6 UDP ports directly from procfs (no ss dependency)."""
    ports: set[int] = set()
    for path in (Path('/proc/net/udp'), Path('/proc/net/udp6')):
        if not path.exists():
            continue
        try:
            lines = path.read_text().splitlines()[1:]
        except OSError as exc:
            raise StartError(f'Не удалось проверить занятые UDP-порты: {path}: {exc}') from exc
        for line in lines:
            columns = line.split()
            if len(columns) < 2:
                continue
            try:
                ports.add(int(columns[1].rsplit(':', 1)[1], 16))
            except (ValueError, IndexError):
                raise StartError(f'Не удалось разобрать порт в {path}: {line}')
    return ports


def running_world(name: str) -> bool:
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = [a.decode('utf-8', 'replace') for a in
                    (entry / 'cmdline').read_bytes().split(b'\0') if a]
        except OSError:
            continue
        if not any('zombie.network.GameServer' in arg or 'ProjectZomboid64' in arg
                   for arg in argv):
            continue
        for idx, value in enumerate(argv):
            if value == '-servername' and idx + 1 < len(argv) and argv[idx + 1] == name:
                return True
            if value == '-servername=' + name:
                return True
    return False


def screen_exists(name: str) -> bool:
    output = cmd(['runuser', '-u', 'zomboid', '--', 'screen', '-ls'], check=False)
    if output.returncode not in (0, 1):
        raise StartError(f'Не удалось проверить screen: {(output.stderr or output.stdout).strip()}')
    return re.search(r'(?m)^\s*\d+\.' + re.escape(name) + r'\s+\(', output.stdout) is not None


def select_ports(name: str) -> tuple[tuple[int, int], str]:
    occupied = listening_udp_ports()
    reserved = reserved_ports(name)

    ini = CONFIGS / f'{name}.ini'
    preferences: list[tuple[tuple[int, int], str]] = []
    stored = saved_ports(name)
    if stored:
        preferences.append((stored, 'сохранённое назначение'))
    if ini.is_file() and not ini.is_symlink():
        preferences.append((ini_ports(ini), 'текущий конфиг'))
    preferences.extend((((p, p + 1), 'новая пара') for p in CANDIDATES))

    for pair, source in preferences:
        if pair[0] != pair[1] and not (set(pair) & (reserved | occupied)):
            return pair, source
    raise StartError('Не найдено свободной пары UDP-портов в диапазоне 16271–16992')


def allow_ufw(pair: tuple[int, int]) -> None:
    if not shutil.which('ufw'):
        print('[WARN] UFW не найден; проверь внешний firewall у провайдера', flush=True)
        return
    output = cmd(['ufw', 'status'])
    if 'Status: active' not in output.stdout:
        print('[WARN] UFW неактивен; проверь внешний firewall у провайдера', flush=True)
        return
    for port in pair:
        cmd(['ufw', 'allow', f'{port}/udp'])
    print('[OK] UFW: UDP-порты разрешены', flush=True)


def write_registry(name: str, pair: tuple[int, int]) -> None:
    destination = PORTS_DIR / f'{name}.json'
    require(not destination.is_symlink(), f'Ссылка вместо записи о портах: {destination}')
    fd, temp_path = tempfile.mkstemp(prefix='.pz-port-', dir=PORTS_DIR)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'DefaultPort': pair[0], 'UDPPort': pair[1]}, stream, indent=2)
            stream.write('\n')
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, destination)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def update_ini(name: str, pair: tuple[int, int]) -> bool:
    path = CONFIGS / f'{name}.ini'
    contents, values = read_ini(path)
    if not path.exists():
        return False  # New world: use CLI flags; do not create a two-line INI.
    changes = {'DefaultPort': pair[0], 'UDPPort': pair[1]}
    lines = contents.splitlines(keepends=True)
    for key, value in changes.items():
        expression = re.compile(r'^(\s*' + re.escape(key) + r'\s*=\s*)[^\r\n]*(\r?\n)?$')
        if key in values:
            for i, line in enumerate(lines):
                match = expression.match(line)
                if match:
                    lines[i] = match.group(1) + str(value) + (match.group(2) or '')
                    break
        else:
            if lines and not lines[-1].endswith(('\n', '\r')):
                lines.append('\n')
            lines.append(f'{key}={value}\n')
    updated = ''.join(lines)
    if updated == contents:
        return True
    stat = path.stat()
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{name}-', dir=CONFIGS)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(updated)
        shutil.copystat(path, tmp_name)
        os.chown(tmp_name, stat.st_uid, stat.st_gid)
        os.replace(tmp_name, path)
        # Preserve operator edit access even when replacing an ACL-enabled file.
        cmd(['setfacl', '-m', 'u:pidor1:rw-,u:pidor2:rw-', str(path)])
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return True


def ensure_config_directory() -> None:
    data_dir = HOME / 'Zomboid'
    require(data_dir.is_dir() and not data_dir.is_symlink(),
            f'Не найден обычный каталог {data_dir}; сначала выполни деплой')
    require(not CONFIGS.is_symlink(), f'Каталог конфигурации не должен быть ссылкой: {CONFIGS}')
    if CONFIGS.is_dir():
        return
    require(not CONFIGS.exists(), f'Необычный тип объекта: {CONFIGS}')
    CONFIGS.mkdir(mode=0o750)
    zomboid = pwd.getpwnam('zomboid')
    os.chown(CONFIGS, zomboid.pw_uid, zomboid.pw_gid)
    cmd(['setfacl', '-m',
         'u:pidor1:rwx,u:pidor2:rwx,d:u:pidor1:rwx,d:u:pidor2:rwx', str(CONFIGS)])
    print(f'[OK] Создан каталог {CONFIGS}', flush=True)


def start(name: str) -> None:
    require(os.geteuid() == 0, 'Запуск: sudo python3 pz_start.py --name NAME')
    require(bool(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name)), 'Имя: только латиница, цифры, _ и -')
    require(name != 'servertest', 'servertest управляется через zomboid.service; его настройки не меняем')
    require(SERVER.is_file(), f'Не найден {SERVER}; сначала выполни деплой')
    for command in ('screen', 'runuser', 'setfacl'):
        require(shutil.which(command) is not None, f'Не найдена команда: {command}')
    ensure_config_directory()
    require(not (CONFIGS / f'{name}.ini').is_symlink(), 'INI не должен быть символьной ссылкой')

    PORTS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(PORTS_DIR, 0o700)
    with (PORTS_DIR / '.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        require(not running_world(name), f'Сервер {name} уже запущен')
        require(not screen_exists(name), f'Screen-сессия {name} уже существует')

        pair, reason = select_ports(name)
        print(f'[PORT] {name}: {pair[0]}/udp и {pair[1]}/udp ({reason})', flush=True)
        allow_ufw(pair)
        write_registry(name, pair)
        configured = update_ini(name, pair)
        if configured:
            print(f'[OK] {name}.ini: порты сохранены', flush=True)
        else:
            print('[INFO] INI пока нет: новый мир создаст его при старте. '
                  'Порты сохранены для следующих запусков через pz_start.py.', flush=True)

        # The explicit arguments also protect the initial boot before the INI exists.
        command = [
            'runuser', '-u', 'zomboid', '--', 'screen', '-dmS', name,
            'bash', '-c',
            'cd /home/zomboid/pzserver && exec ./start-server.sh '
            '-servername "$1" -port "$2" -udpport "$3"',
            'pz-start', name, str(pair[0]), str(pair[1]),
        ]
        cmd(command)
        time.sleep(1)
        require(screen_exists(name), 'Screen-сессия сразу закрылась; сервер не запустился. Проверь логи Zomboid.')
        print(f'\n[OK] Создана фоновая screen-сессия: {name}', flush=True)
        print(f'Подключиться: sudo -u zomboid screen -r {name}', flush=True)
        print('В фон: Ctrl+A, затем D. Остановка: quit в игровой консоли.', flush=True)
        print('ВНИМАНИЕ: screen-сессия ещё не означает, что сервер полностью загрузился.', flush=True)
        print('Проверь сообщение SERVER STARTED в консоли.', flush=True)
        print('Для подключения игроков: IP VPS и порт ' + str(pair[0]), flush=True)
        print('Если игроки извне не подключаются, проверь firewall в панели VPS.', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True, help='Имя мира, например ivent')
    args = parser.parse_args()
    try:
        start(args.name)
    except (StartError, OSError, ValueError) as exc:
        print(f'[ERROR] {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())