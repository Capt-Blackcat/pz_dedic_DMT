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
