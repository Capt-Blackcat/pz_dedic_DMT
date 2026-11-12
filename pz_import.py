#!/usr/bin/env python3
"""Import one Project Zomboid server from /home/zomboid/import with backup and rollback.

Usage: sudo python3 pz_import.py --name ivent
For running servers, supports a GNU screen session owning the target process,
or a matching systemd unit pz-NAME.service / zomboid.service.
If the session cannot be identified, the import aborts without changing data.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

HOME = Path('/home/zomboid')
INBOX = HOME / 'import'
BACKUPS = HOME / 'backup'
DATA = HOME / 'Zomboid'
SUFFIXES = ('.tar.gz', '.tar.xz', '.tar.bz2', '.tgz', '.tar', '.zip', '.7z', '.rar')
DONE: list[str] = []
SKIPPED: list[str] = []
FAILED: list[str] = []


class ImportFailure(Exception):
    pass


def note(message: str) -> None:
    print(message, flush=True)


def done(message: str) -> None:
    DONE.append(message)
    note('[OK] ' + message)


def skipped(message: str) -> None:
    SKIPPED.append(message)
    note('[SKIP] ' + message)


def failure(message: str) -> None:
    FAILED.append(message)
    note('[ERROR] ' + message)


def report(name: str) -> None:
    note('\n========== ИТОГ: ' + name + ' ==========')
    for heading, items, marker in [('Выполнено', DONE, '+'), ('Пропущено', SKIPPED, '-'), ('Не удалось', FAILED, '!')]:
        note(f'{heading} ({len(items)}):')
        for value in items:
            note(f'  {marker} {value}')
        if not items:
            note('  (нет)')


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ImportFailure(message)


def run(argv: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, text=True, capture_output=capture)
    if check and result.returncode:
        raise ImportFailure(f'Команда {argv[0]} завершилась с кодом {result.returncode}: {(result.stderr or result.stdout or "").strip()}')
    return result


def validate_name(name: str) -> None:
    require(bool(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name)), 'Имя сервера: только латиница, цифры, _ или -')


def is_named_config(path: Path, name: str) -> bool:
    return path.is_file() and not path.is_symlink() and (path.name.startswith(name + '.') or path.name.startswith(name + '_'))


def current_paths(name: str) -> list[Path]:
    found: list[Path] = []
    confdir = DATA / 'Server'
    if confdir.is_dir():
        found.extend(sorted(p for p in confdir.iterdir() if is_named_config(p, name)))
    dbdir = DATA / 'db'
    if dbdir.is_dir():
        found.extend(sorted(p for p in dbdir.iterdir() if p.is_file() and not p.is_symlink() and (p.name == name + '.db' or p.name.startswith(name + '.db-'))))
    savedir = DATA / 'Saves' / 'Multiplayer' / name
    if savedir.is_dir() and not savedir.is_symlink():
        found.append(savedir)
    return found


def find_archive(name: str) -> Path:
    require(INBOX.is_dir(), f'Папка не существует: {INBOX}')
    archives = [p for p in INBOX.iterdir() if p.is_file() and not p.is_symlink() and p.name.lower().endswith(SUFFIXES)]
    matches = [p for p in archives if name.casefold() in p.name.casefold()]
    require(len(archives) == 1, f'В import должно находиться ровно 1 архив, найдено {len(archives)}: {[p.name for p in archives]}')
    require(len(matches) == 1, f'Архив не соответствует --name {name}: {archives[0].name}')
    return matches[0]


def check_member(name: str) -> Path:
    normalized = name.replace('\\', '/')
    p = PurePosixPath(normalized)
    require(bool(normalized) and not normalized.startswith(('/', '~')) and not re.match(r'^[A-Za-z]:', normalized), 'Недопустимый путь архива: ' + name)
    require('..' not in p.parts and '\x00' not in name, 'Выход из каталога архива: ' + name)
    return Path(*p.parts)


def safe_extract_tar(archive: Path, target: Path) -> None:
    with tarfile.open(archive, 'r:*') as tar:
        members = tar.getmembers()
        for member in members:
            check_member(member.name)
            require(member.isfile() or member.isdir(), f'Недопустимый тип элемента TAR: {member.name}')
        for member in members:
            dest = target / check_member(member.name)
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as stream, dest.open('wb') as output:
                    require(stream is not None, f'Не удалось прочесть {member.name}')
                    shutil.copyfileobj(stream, output)


def safe_extract_zip(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            check_member(info.filename)
            unix_mode = info.external_attr >> 16
            require(not (stat.S_IFMT(unix_mode) == stat.S_IFLNK), 'ZIP содержит символическую ссылку: ' + info.filename)
        bad = z.testzip()
        require(bad is None, f'Ошибка CRC ZIP: {bad}')
        for info in z.infolist():
            dest = target / check_member(info.filename)
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, dest.open('wb') as dst:
                    shutil.copyfileobj(src, dst)


def safe_extract_external(archive: Path, target: Path) -> None:
    """RAR/7Z extraction with a non-root process and archive path preflight."""
    is_rar = archive.name.lower().endswith('.rar')
    use_7z = shutil.which('7z') is not None
    require(use_7z or (is_rar and shutil.which('unrar') is not None),
            'Для .7z нужна команда 7z; для .rar подойдёт 7z либо unrar')
    if use_7z:
        listing = run(['7z', 'l', '-slt', str(archive)]).stdout
        items = listing.split('\n----------\n', 1)
        require(len(items) == 2, 'Не удалось получить список файлов архива')
        for line in items[1].splitlines():
            if line.startswith('Path = '):
                check_member(line[len('Path = '):])
            if line.startswith(('Symbolic Link = ', 'Hard Link = ')):
                raise ImportFailure('Архив содержит ссылки; импорт отменён')
        run(['7z', 't', str(archive)])
    else:
        for entry in run(['unrar', 'lb', str(archive)]).stdout.splitlines():
            check_member(entry)
        run(['unrar', 't', str(archive)])

    # An SFTP upload can be mode 600 and owned by an operator. Copy it into a
    # private staging folder accessible by the unprivileged extractor.
    run(['setfacl', '-m', 'u:zomboid:--x', str(INBOX)])
    import pwd
    account = pwd.getpwnam('zomboid')
    private_copy = target.parent / '.source_archive'
    shutil.copy2(archive, private_copy)
    os.chown(private_copy, account.pw_uid, account.pw_gid)
    os.chmod(private_copy, 0o600)
    os.chown(target.parent, account.pw_uid, account.pw_gid)
    os.chown(target, account.pw_uid, account.pw_gid)
    os.chmod(target.parent, 0o700)
    os.chmod(target, 0o700)
    if use_7z:
        command = ['7z', 'x', '-y', '-o' + str(target), str(private_copy)]
    else:
        command = ['unrar', 'x', '-o+', str(private_copy), str(target) + '/']
    run(['runuser', '-u', 'zomboid', '--', *command])
    for p in target.rglob('*'):
        require(not p.is_symlink(), f'Ссылка после извлечения: {p}')
        require(p.is_file() or p.is_dir(), f'Необычный файл после извлечения: {p}')


def unpack(archive: Path, stage: Path) -> None:
    lower = archive.name.lower()
    if lower.endswith(('.tar.gz', '.tar.xz', '.tar.bz2', '.tgz', '.tar')):
        safe_extract_tar(archive, stage)
    elif lower.endswith('.zip'):
        safe_extract_zip(archive, stage)
    elif lower.endswith(('.rar', '.7z')):
        safe_extract_external(archive, stage)
    else:
        raise ImportFailure('Неподдерживаемый тип архива')


def discover(stage: Path, name: str) -> list[tuple[Path, Path]]:
    base = stage
    for _ in range(5):
        if (base / 'Server').is_dir() or (base / 'Multiplayer').is_dir() or (base / 'Saves').is_dir():
            break
        children = list(base.iterdir())
        if len(children) == 1 and children[0].is_dir() and not children[0].is_symlink():
            base = children[0]
        else:
            break
    configs = base / 'Server'
    require(configs.is_dir(), 'В архиве нет папки Server')
    config_files = sorted(p for p in configs.iterdir() if is_named_config(p, name))
    require(any(p.name == name + '.ini' for p in config_files), f'Не найден Server/{name}.ini')
    db = base / 'db' / (name + '.db')
    if not db.is_file():
        db = base / (name + '.db')
    require(db.is_file() and not db.is_symlink(), f'В архиве нет {name}.db')
    save = base / 'Saves' / 'Multiplayer' / name
    if not save.is_dir():
        save = base / 'Multiplayer' / name
    require(save.is_dir() and not save.is_symlink(), f'В архиве нет Multiplayer/{name}/')
    require(any(save.iterdir()), f'Папка Multiplayer/{name}/ пуста')
    mapping = [(p, DATA / 'Server' / p.name) for p in config_files]
    # SQLite sidecars, if provided, must be imported along with the database.
    mapping.append((db, DATA / 'db' / db.name))
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = db.with_name(db.name + suffix)
        if sidecar.is_file():
            mapping.append((sidecar, DATA / 'db' / sidecar.name))
    mapping.append((save, DATA / 'Saves' / 'Multiplayer' / name))
    return mapping


def backup(name: str, paths: list[Path]) -> Path:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUPS, 0o750)
    run(['chown', 'zomboid:zomboid', str(BACKUPS)])
    run(['setfacl', '-m', 'u:pidor1:r-x,u:pidor2:r-x', str(BACKUPS)])
    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    out = BACKUPS / f'{name}_{stamp}.tar.gz'
    suffix = 2
    while out.exists():
        out = BACKUPS / f'{name}_{stamp}_{suffix}.tar.gz'
        suffix += 1
    for p in paths:
        if p.is_symlink() or (p.is_dir() and any(q.is_symlink() for q in p.rglob('*'))):
            raise ImportFailure(f'Символьная ссылка в старом сохранении: {p}')
    with tarfile.open(out, 'w:gz') as tar:
        for p in paths:
            tar.add(p, arcname=p.relative_to(DATA), recursive=True)
    with tarfile.open(out, 'r:gz') as tar:
        require(len(tar.getmembers()) > 0, 'Создан пустой архив резервной копии')
    run(['chown', 'zomboid:zomboid', str(out)])
    os.chmod(out, 0o640)
    run(['setfacl', '-m', 'u:pidor1:r--,u:pidor2:r--', str(out)])
    return out


def remove_targets(name: str) -> None:
    for p in current_paths(name):
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()


def install(mapping: list[tuple[Path, Path]]) -> None:
    for src, dst in mapping:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def permissions(name: str) -> None:
    paths = current_paths(name)
    require(bool(paths), 'Нет установленных файлов для назначения прав')
    for p in paths:
        run(['chown', '-R', 'zomboid:zomboid', str(p)])
        if p.is_dir():
            run(['setfacl', '-R', '-m', 'u:pidor1:rwX,u:pidor2:rwX,u:zomboid:rwX,m::rwx', str(p)])
            run(['find', str(p), '-type', 'd', '-exec', 'setfacl', '-m', 'd:u:pidor1:rwx,d:u:pidor2:rwx,d:u:zomboid:rwx,d:m::rwx', '{}', '+'])
        else:
            run(['setfacl', '-m', 'u:pidor1:rw-,u:pidor2:rw-,u:zomboid:rw-,m::rw-', str(p)])
    for directory in (DATA / 'Server', DATA / 'db', DATA / 'Saves', DATA / 'Saves' / 'Multiplayer'):
        if directory.is_dir():
            run(['setfacl', '-m', 'u:pidor1:rwx,u:pidor2:rwx,u:zomboid:rwx,m::rwx', str(directory)])
            run(['setfacl', '-m', 'd:u:pidor1:rwx,d:u:pidor2:rwx,d:u:zomboid:rwx,d:m::rwx', str(directory)])
    run(['setfacl', '-m', 'u:pidor1:r-x,u:pidor2:r-x', str(HOME)])
    run(['setfacl', '-m', 'u:pidor1:rwx,u:pidor2:rwx,u:zomboid:rwx,m::rwx', str(DATA)])


def read_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f'/proc/{pid}/cmdline').read_bytes()
        return [part.decode('utf-8', 'replace') for part in raw.split(b'\x00') if part]
    except (OSError, PermissionError):
        return []


def proc_ppid(pid: int) -> int:
    try:
        text = Path(f'/proc/{pid}/status').read_text()
        return int(re.search(r'^PPid:\s*(\d+)', text, re.M).group(1))
    except (OSError, AttributeError):
        return 0


def related_to(pid: int, ancestor_pid: int) -> bool:
    for _ in range(64):
        if pid == ancestor_pid:
            return True
        pid = proc_ppid(pid)
        if pid <= 1:
            return False
    return False


def find_game_processes(name: str) -> tuple[list[int], list[int]]:
    matching, unknown = [], []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        pid = int(proc.name)
        argv = read_cmdline(pid)
        if not argv:
            continue
        joined = ' '.join(argv)
        if 'zombie.network.GameServer' not in joined and 'ProjectZomboid64' not in joined:
            continue
        value = None
        for idx, token in enumerate(argv):
            if token == '-servername' and idx + 1 < len(argv):
                value = argv[idx + 1]
            elif token.startswith('-servername='):
                value = token.split('=', 1)[1]
        if value == name:
            matching.append(pid)
        elif value is None:
            unknown.append(pid)
    return matching, unknown


def wait_gone(pids: list[int], seconds: int) -> bool:
    for _ in range(seconds):
        if not any(Path(f'/proc/{pid}').exists() and read_cmdline(pid) for pid in pids):
            return True
        time.sleep(1)
    return False


def stop_server(name: str) -> None:
    pids, unknown = find_game_processes(name)
    require(not unknown, 'Обнаружен сервер без определённого -servername. Автоостановка небезопасна; сначала останови вручную.')
    if not pids:
        skipped('Целевой сервер не запущен')
        return
    reply = input(f'Сервер {name} запущен (PID {pids}). Остановить? [y/N]: ').strip().lower()
    require(reply in {'y', 'yes'}, 'Импорт отменён оператором')
    # Find a GNU screen session whose process actually owns the running JVM.
    screen_pid = None
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        argv = read_cmdline(int(proc.name))
        joined = ' '.join(argv)
        if 'SCREEN' not in joined:
            continue
        if not all(related_to(pid, int(proc.name)) for pid in pids):
            continue
        screen_pid = int(proc.name)
        break
    if screen_pid is not None:
        run(['runuser', '-u', 'zomboid', '--', 'screen', '-S', str(screen_pid), '-p', '0', '-X', 'stuff', 'quit\r'])
        note('Отправлена команда quit через screen; ожидание до 180 секунд')
    else:
        # Only stop a systemd service if its unit explicitly targets this server.
        unit = None
        for candidate in (f'pz-{name}.service', 'zomboid.service'):
            query = run(['systemctl', 'show', candidate, '--property=ExecStart,ActiveState'], check=False).stdout
            if 'ActiveState=active' in query and re.search(r'-servername[ =]'+re.escape(name)+r'(?=[ ;}]|$)', query):
                unit = candidate
                break
        require(unit is not None, 'Сервер работает, но его консоль не найдена (screen/systemd). Останови вручную и повтори импорт.')
        run(['systemctl', 'stop', unit])
        note(f'Остановлена служба {unit}')
    if not wait_gone(pids, 180):
        note('quit не завершил процесс; отправляем SIGTERM (kill)')
        for pid in pids:
            if read_cmdline(pid):
                os.kill(pid, signal.SIGTERM)
        require(wait_gone(pids, 45), 'Процесс всё ещё жив после SIGTERM. Импорт остановлен, SIGKILL не применяется.')
    require(not find_game_processes(name)[0], 'Целевой сервер продолжает работать')
    done('Сервер остановлен')


def restore(name: str, archive: Path | None) -> None:
    remove_targets(name)
    if archive is not None:
        with tarfile.open(archive, 'r:gz') as tar:
            for member in tar.getmembers():
                dest = DATA / check_member(member.name)
                require(member.isfile() or member.isdir(), 'В бэкапе обнаружена ссылка или специальный файл')
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as src, dest.open('wb') as out:
                        require(src is not None, 'Ошибка чтения резервной копии')
                        shutil.copyfileobj(src, out)
        permissions(name)
        done('Откат: предыдущая версия восстановлена')
    else:
        done('Откат: частично импортированные файлы удалены (старой версии не было)')


def cleanup_inbox() -> None:
    for p in INBOX.iterdir():
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
    done('Каталог import очищен')


def main(name: str) -> None:
    require(os.geteuid() == 0, 'Запускать от root (sudo)')
    validate_name(name)
    require(shutil.which('setfacl') is not None, 'Нужен пакет acl')
    archive = find_archive(name)
    done('Найден архив: ' + archive.name)
    stage = INBOX / ('.pz-stage-' + uuid.uuid4().hex[:10])
    stage.mkdir(mode=0o700)
    extracted = stage / 'contents'
    extracted.mkdir(mode=0o700)
    changed = False
    old_backup: Path | None = None
    try:
        unpack(archive, extracted)
        mapping = discover(extracted, name)
        done('Архив распакован, нужные файлы найдены')
        stop_server(name)
        old = current_paths(name)
        if old:
            old_backup = backup(name, old)
            done('Резервная копия: ' + str(old_backup))
        else:
            skipped('Предыдущих файлов сервера нет')
        changed = True
        remove_targets(name)
        done('Прежние файлы выбранного сервера удалены')
        install(mapping)
        done('Новые файлы размещены')
        permissions(name)
        done('Владелец и ACL настроены')
        require((DATA / 'db' / (name + '.db')).is_file(), 'Не найдена база после импорта')
        require((DATA / 'Server' / (name + '.ini')).is_file(), 'Не найден конфиг после импорта')
        require((DATA / 'Saves' / 'Multiplayer' / name).is_dir(), 'Не найден мир после импорта')
        done('Структура проверена')
        note('\nУстановленные файлы:')
        for p in current_paths(name):
            size = p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob('*') if f.is_file())
            note(f'  {p.relative_to(DATA)}  ({size} байт)')
        note(f'Бэкап: {old_backup if old_backup else "не требовался"}')
        note('Сервер оставлен остановленным; запуск вручную.')
    except BaseException as error:
        if changed:
            failure('Импорт прерван: ' + str(error))
            note('Запускается откат...')
            try:
                restore(name, old_backup)
            except BaseException as rollback_error:
                failure('ОТКАТ НЕ УДАЛСЯ: ' + str(rollback_error))
                note('Резервная копия: ' + str(old_backup))
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    # Cleanup is deliberately outside the rollback block. A cleanup error does
    # not invalidate successfully installed data and must not revert it.
    cleanup_inbox()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True, help='Имя сервера, например ivent')
    args = parser.parse_args()
    rc = 0
    try:
        main(args.name)
    except (Exception, KeyboardInterrupt) as error:
        if not FAILED:
            failure(str(error) or type(error).__name__)
        rc = 1
    finally:
        report(args.name)
    sys.exit(rc)
