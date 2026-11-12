"""Offline regression tests: python3 -m unittest discover -s tests -v"""
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load(filename, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


imp = load('pz_import.py', 'pz_import')
exp = load('pz_export.py', 'pz_export')


class PartialImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        imp.HOME, imp.DATA, imp.INBOX, imp.BACKUPS = root, root / 'Zomboid', root / 'import', root / 'backup'
        imp.DATA.mkdir()
        imp.INBOX.mkdir()
        imp.DONE.clear()
        imp.FAILED.clear()
        imp.SKIPPED.clear()
        self.patches = [patch.object(imp.os, 'geteuid', return_value=0),
                        patch.object(imp.shutil, 'which', return_value='/usr/bin/setfacl'),
                        patch.object(imp, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')),
                        patch.object(imp, 'stop_server', return_value=None)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def write(self, path, data):
        dest = imp.DATA / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(data)
        return dest

    def archive(self, members):
        path = imp.INBOX / 'partial.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, contents in members.items():
                archive.writestr(name, contents)

    def test_full_archive_imports_player_and_preserves_unmentioned(self):
        self.write('Saves/Multiplayer/vzaperti/keep.bin', 'keep')
        self.write('Server/vzaperti_spawnregions.lua', 'keep config')
        self.archive({'vzaperti/Server/vzaperti.ini': 'ini',
                      'vzaperti/db/vzaperti.db': 'db',
                      'vzaperti/Saves/Multiplayer/vzaperti/map.bin': 'map',
                      'vzaperti/Saves/Multiplayer/vzaperti_player/players.bin': 'player'})
        imp.main('vzaperti')
        self.assertEqual((imp.DATA / 'Saves/Multiplayer/vzaperti/keep.bin').read_text(), 'keep')
        self.assertEqual((imp.DATA / 'Server/vzaperti_spawnregions.lua').read_text(), 'keep config')
        self.assertEqual((imp.DATA / 'Saves/Multiplayer/vzaperti_player/players.bin').read_text(), 'player')

    def test_partial_config_import(self):
        self.write('Server/vzaperti.ini', 'existing')
        self.archive({'Server/vzaperti_spawnpoints.lua': 'points'})
        imp.main('vzaperti')
        self.assertEqual((imp.DATA / 'Server/vzaperti.ini').read_text(), 'existing')
        self.assertEqual((imp.DATA / 'Server/vzaperti_spawnpoints.lua').read_text(), 'points')

    def test_db_replacement_removes_stale_wal(self):
        self.write('db/vzaperti.db', 'old')
        self.write('db/vzaperti.db-wal', 'stale')
        self.archive({'db/vzaperti.db': 'new'})
        imp.main('vzaperti')
        self.assertFalse((imp.DATA / 'db/vzaperti.db-wal').exists())
        with tarfile.open(next(imp.BACKUPS.glob('*.tar.gz'))) as backup:
            self.assertIn('db/vzaperti.db-wal', backup.getnames())

    def test_player_only_archive(self):
        self.archive({'vzaperti_player/player.bin': 'player'})
        imp.main('vzaperti')
        self.assertEqual((imp.DATA / 'Saves/Multiplayer/vzaperti_player/player.bin').read_text(), 'player')

    def test_replace_saves_is_explicit(self):
        self.write('Saves/Multiplayer/vzaperti/old.bin', 'old')
        self.archive({'Saves/Multiplayer/vzaperti/new.bin': 'new'})
        imp.main('vzaperti', replace_saves=True)
        self.assertFalse((imp.DATA / 'Saves/Multiplayer/vzaperti/old.bin').exists())

    def test_rollback_preserves_other_components(self):
        self.write('Saves/Multiplayer/vzaperti/old.bin', 'old')
        self.write('Saves/Multiplayer/vzaperti_player/keep.bin', 'keep')
        self.archive({'Saves/Multiplayer/vzaperti/new.bin': 'new'})
        original = imp.permissions
        calls = [0]
        def fail_once(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError('simulated failure')
            return original(*args, **kwargs)
        with patch.object(imp, 'permissions', side_effect=fail_once):
            with self.assertRaises(RuntimeError):
                imp.main('vzaperti')
        self.assertEqual((imp.DATA / 'Saves/Multiplayer/vzaperti/old.bin').read_text(), 'old')
        self.assertFalse((imp.DATA / 'Saves/Multiplayer/vzaperti/new.bin').exists())
        self.assertEqual((imp.DATA / 'Saves/Multiplayer/vzaperti_player/keep.bin').read_text(), 'keep')

    def test_traversal_is_rejected(self):
        self.archive({'../outside': 'unsafe'})
        with self.assertRaises(imp.ImportFailure):
            imp.main('vzaperti')


class ExportTests(unittest.TestCase):
    def test_player_folder_is_in_export(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(exp, 'DATA', Path(temp)):
                for name, content in [('Server/vzaperti.ini', 'ini'),
                                      ('Saves/Multiplayer/vzaperti/map.bin', 'map'),
                                      ('Saves/Multiplayer/vzaperti_player/player.bin', 'player')]:
                    path = exp.DATA / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)
                db = exp.DATA / 'db/vzaperti.db'
                db.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(db) as conn:
                    conn.execute('CREATE TABLE t (id INTEGER)')
                names = {name for _, name in exp.collect('vzaperti')}
                self.assertIn('Saves/Multiplayer/vzaperti_player/player.bin', names)


if __name__ == '__main__':
    unittest.main()
