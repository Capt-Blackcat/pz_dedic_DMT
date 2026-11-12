#!/usr/bin/env python3
"""Export one Project Zomboid server to /home/zomboid/backup as .tar.gz.

Usage: sudo python3 pz_export.py --name ivent
The archive includes Server/, db/, Saves/Multiplayer/NAME and optional NAME_player.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pwd
import re
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path, PurePosixPath

HOME = Path('/home/zomboid')
DATA = HOME / 'Zomboid'
BACKUPS = HOME / 'backup'
GRACE_SECONDS = 180
TERM_SECONDS = 45


class ExportError(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ExportError(message)


def command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True)
    if check and result.returncode:
        raise ExportError(f'{args[0]}: {(result.stderr or result.stdout).strip()}')
    return result


def game_processes(name: str) -> tuple[list[int], list[int]]:
    matched, unnamed = [], []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            parts = [a.decode('utf-8', 'replace') for a in (proc / 'cmdline').read_bytes().split(b'\0') if a]
        except OSError:
            continue
        joined = ' '.join(parts)
        if 'zombie.network.GameServer' not in joined and 'ProjectZomboid64' not in joined:
            continue
        server_name = None
        for i, arg in enumerate(parts):
            if arg == '-servername' and i + 1 < len(parts):
                server_name = parts[i + 1]
            elif arg.startswith('-servername='):
                server_name = arg.split('=', 1)[1]
        if server_name == name:
            matched.append(int(proc.name))
        elif server_name is None:
            unnamed.append(int(proc.name))
    return matched, unnamed


def process_parts(pid: int) -> list[str]:
    try:
        return [part.decode('utf-8', 'replace') for part in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0') if part]
    except OSError:
        return []


def parent_pid(pid: int) -> int:
    try:
        match = re.search(r'^PPid:\s*(\d+)', Path(f'/proc/{pid}/status').read_text(), re.M)
        return int(match.group(1)) if match else 0
    except OSError:
        return 0


def child_of(pid: int, parent: int) -> bool:
    for _ in range(64):
        if pid == parent:
            return True
        pid = parent_pid(pid)
        if pid <= 1:
            return False
    return False


def screen_session(pids: list[int]) -> int | None:
    for proc in Path('/proc').iterdir():
        if proc.name.isdigit() and 'SCREEN' in ' '.join(process_parts(int(proc.name))):
            screen_pid = int(proc.name)
            if all(child_of(pid, screen_pid) for pid in pids):
                return screen_pid
    return None


def systemd_unit(name: str) -> str | None:
    for unit in (f'pz-{name}.service', 'zomboid.service'):
        result = command('systemctl', 'show', unit, '--property=ExecStart,ActiveState', check=False)
        if 'ActiveState=active' in result.stdout and re.search(
            r'-servername[ =]' + re.escape(name) + r'(?=[ ;}]|$)', result.stdout
        ):
            return unit
    return None


def wait_stopped(name: str, seconds: int) -> bool:
    for _ in range(seconds):
        matched, unnamed = game_processes(name)
        if not matched and not unnamed:
            return True
        time.sleep(1)
    return False


def stop_if_running(name: str) -> None:
    pids, unnamed = game_processes(name)
    require(not unnamed, 'Найден сервер без -servername. Останови его вручную: нельзя определить, чей это процесс.')
    if not pids:
        print('[SKIP] Сервер уже остановлен')
        return

    answer = input(f'Сервер {name} запущен (PID {pids}). Остановить для экспорта? [y/N]: ').strip().lower()
    require(answer in ('y', 'yes'), 'Отмена оператором. Экспорт не выполнялся.')

    session = screen_session(pids)
    if session is not None:
        command('runuser', '-u', 'zomboid', '--', 'screen', '-S', str(session), '-p', '0', '-X', 'stuff', 'quit\r')
        print('[INFO] Отправлен quit через screen')
    else:
        unit = systemd_unit(name)
        require(unit is not None, 'Не удалось найти screen/systemd для этого сервера. Останови его вручную.')
        command('systemctl', 'stop', unit)
        print(f'[INFO] Остановлена служба {unit}')

    if not wait_stopped(name, GRACE_SECONDS):
        print('[WARN] Сервер не завершился; отправляем SIGTERM оставшимся процессам этого мира')
        active, unnamed = game_processes(name)
        require(not unnamed, 'Есть неопознанные процессы, SIGTERM не отправлен')
        for pid in active:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        require(wait_stopped(name, TERM_SECONDS), 'Сервер не остановился после SIGTERM; экспорт отменён')
    require(not any(game_processes(name)), 'Сервер всё ещё запущен; экспорт отменён')
    print('[OK] Сервер остановлен')


def safe_file(path: Path) -> None:
    require(not path.is_symlink() and path.is_file(), f'Недопустимый файл: {path}')


def collect(name: str) -> list[tuple[Path, str]]:
    config_dir = DATA / 'Server'
    database_dir = DATA / 'db'
    world = DATA / 'Saves' / 'Multiplayer' / name
    for directory in (DATA, config_dir, database_dir, DATA / 'Saves', world.parent):
        require(directory.is_dir() and not directory.is_symlink(), f'Не найден каталог: {directory}')
    require(world.is_dir() and not world.is_symlink(), f'Не найден мир: {world}')

    configs = sorted(p for p in config_dir.iterdir() if p.name.startswith((name + '.', name + '_')))
    require(any(p.name == name + '.ini' for p in configs), f'Не найден Server/{name}.ini')
    members: list[tuple[Path, str]] = []
    for p in configs:
        safe_file(p)
        members.append((p, f'Server/{p.name}'))

    database = database_dir / f'{name}.db'
    safe_file(database)
    members.append((database, f'db/{database.name}'))
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = database_dir / f'{name}.db{suffix}'
        if sidecar.exists() or sidecar.is_symlink():
            safe_file(sidecar)
            members.append((sidecar, f'db/{sidecar.name}'))

    player = world.with_name(name + '_player')
    save_folders = [world]
    if player.exists() or player.is_symlink():
        require(player.is_dir() and not player.is_symlink(), f'Недопустимая папка игроков: {player}')
        save_folders.append(player)
    for folder in save_folders:
        members.append((folder, folder.relative_to(DATA).as_posix()))
        for current, dirs, files in os.walk(folder, followlinks=False):
            for item in dirs + files:
                candidate = Path(current) / item
                require(not candidate.is_symlink(), f'Символьная ссылка в мире: {candidate}')
                require(candidate.is_dir() or candidate.is_file(), f'Необычный объект в мире: {candidate}')
            for dirname in dirs:
                folder = Path(current) / dirname
                members.append((folder, folder.relative_to(DATA).as_posix()))
            for filename in files:
                source = Path(current) / filename
                members.append((source, source.relative_to(DATA).as_posix()))
    require(any(source.is_file() and arc.startswith(f'Saves/Multiplayer/{name}/')
                for source, arc in members), 'Папка мира не содержит файлов')
    return members


def check_sqlite(path: Path) -> None:
    try:
        # The server has already stopped. Read-only mode avoids writing to its database.
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=20)
        try:
            result = conn.execute('PRAGMA quick_check').fetchall()
        finally:
            conn.close()
        require(result == [('ok',)], f'Повреждена SQLite-база: {path}')
    except sqlite3.Error as exc:
        raise ExportError(f'Ошибка SQLite ({path}): {exc}') from exc


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(archive_path: Path, expected: dict[str, tuple[int, str]]) -> None:
    remaining = dict(expected)
    with tarfile.open(archive_path, mode='r|gz') as archive:
        for member in archive:
            member_name = PurePosixPath(member.name)
            require(not member_name.is_absolute() and '..' not in member_name.parts,
                    f'Недопустимый путь в архиве: {member.name}')
            require(member.isfile() or member.isdir(), f'Недопустимый тип: {member.name}')
            if member.isdir():
                continue
            require(member.name in remaining, f'Лишний/повторный файл в архиве: {member.name}')
            size, digest = remaining.pop(member.name)
            require(member.size == size, f'Неверный размер файла: {member.name}')
            contents = archive.extractfile(member)
            require(contents is not None, f'Не удалось прочесть файл: {member.name}')
            found = hashlib.sha256()
            with contents:
                for chunk in iter(lambda: contents.read(1024 * 1024), b''):
                    found.update(chunk)
            require(found.hexdigest() == digest, f'Контрольная сумма не совпала: {member.name}')
    require(not remaining, f'В архив не попали файлы: {list(remaining)[:5]}')


def set_acl(path: Path, spec: str) -> None:
    command('setfacl', '-m', spec, str(path))


def export(name: str) -> Path:
    require(os.geteuid() == 0, 'Требуются права root: запуск через sudo')
    require(bool(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name)), 'Имя: только латиница, цифры, _ и -')
    require(BACKUPS.is_dir() and not BACKUPS.is_symlink(), f'Нет каталога бэкапов: {BACKUPS}')
    require(DATA.is_dir() and not DATA.is_symlink(), f'Нет каталога данных: {DATA}')
    require(os.access(BACKUPS, os.W_OK | os.X_OK), f'Нет права записи в {BACKUPS}')

    files = collect(name)
    stop_if_running(name)
    files = collect(name)  # State may have changed during graceful shutdown.
    check_sqlite(DATA / 'db' / f'{name}.db')
    player_db = DATA / 'Saves' / 'Multiplayer' / name / 'players.db'
    if player_db.is_file():
        check_sqlite(player_db)
    print('[OK] Базы SQLite проверены')

    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    filename = f'{name}_{stamp}.tar.gz'
    serial = 2
    while (BACKUPS / filename).exists():
        filename = f'{name}_{stamp}_{serial}.tar.gz'
        serial += 1
    result = BACKUPS / filename
    temp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(prefix=f'.pz-export-{name}-', suffix='.part', dir=BACKUPS,
                                         delete=False) as temp:
            temp_path = Path(temp.name)
            with tarfile.open(fileobj=temp, mode='w:gz', compresslevel=6) as tar:
                for source, arcname in files:
                    tar.add(source, arcname=arcname, recursive=False)
            temp.flush()
            os.fsync(temp.fileno())

        expected = {arcname: (source.stat().st_size, sha256(source))
                    for source, arcname in files if source.is_file()}
        verify_archive(temp_path, expected)
        print(f'[OK] Проверено файлов: {len(expected)} (SHA-256 + gzip)')

        owner = pwd.getpwnam('zomboid')
        os.chown(temp_path, owner.pw_uid, owner.pw_gid)
        os.chmod(temp_path, 0o640)
        set_acl(BACKUPS, 'u:pidor1:r-x,u:pidor2:r-x')
        set_acl(temp_path, 'u:pidor1:r--,u:pidor2:r--')

        # Publish without overwriting an existing archive, even if the name collides.
        os.link(temp_path, result)
        temp_path.unlink()
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    print(f'\n[OK] ЭКСПОРТ ГОТОВ: {result}')
    print(f'[OK] Размер: {result.stat().st_size:,} байт')
    print(f'[INFO] Сервер оставлен остановленным. Запуск вручную.')
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True, help='Имя сервера: ivent')
    args = parser.parse_args()
    try:
        export(args.name)
        return 0
    except (ExportError, OSError, tarfile.TarError, KeyboardInterrupt) as exc:
        print(f'\n[ERROR] Экспорт не выполнен: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
