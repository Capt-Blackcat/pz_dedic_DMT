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
