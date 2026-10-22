"""Private owner settings, loaded only where needed."""

import json
import os
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


def config_path():
    return Path(os.environ.get('GSD_OWNER_CONFIG', '~/.config/gsd/owner.json')).expanduser()


@lru_cache(maxsize=None)
def _read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def get(key):
    path = config_path()
    settings = _read(path)
    if key not in settings:
        raise ValueError(f'{path}: missing owner config key {key}')
    return settings[key]


@lru_cache(maxsize=None)
def _zone(name):
    return ZoneInfo(name)


def zone():
    return _zone(get('timezone'))
