"""Keep the local store bounded: archive old step folders to B2, drop verified backups.

Nothing else ever removes a step folder or a local snapshot, so this is the one
place pruning happens. A step is archived only when its folder and its manifest
are both older than KEEP_DAYS, so a step that is still being written is never
touched and no lock is needed. The manifest, changes, result, status and open log
stay local so `gsd runs` still measures the step and the next step can carry its
open log forward; the remote tar is the cold copy.
"""

import hashlib
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import owner

KEEP_DAYS = 14
# Bounded work per call: the next steady run continues where this one stopped.
MAX_STEPS = 20
# open.md stays because step.previous_folder hands it to export.carry: a step can
# be the last committed one for more than KEEP_DAYS, and its open log is the
# carry-over the next step reads.
KEPT_IN_STEP = (
    'manifest.json',
    'changes.jsonl',
    'result.json',
    'status.json',
    'open.md',
)
ARCHIVED = 'archived.json'


def _sha1(path):
    digest = hashlib.sha1()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _remote_sha1(target):
    """The single sha1 rclone prints for a remote object, or None if it has none."""
    result = subprocess.run(
        ['rclone', 'hashsum', 'sha1', target], capture_output=True, text=True
    )
    lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode or len(lines) != 1 or not lines[0]:
        return None
    return lines[0][0].lower()


def _old_steps(root, keep_days):
    steps = Path(root) / 'steps'
    if not steps.is_dir():
        return []
    cutoff = time.time() - keep_days * 86400
    old = []
    for path in steps.glob('steady-*'):
        manifest = path / 'manifest.json'
        if not path.is_dir() or (path / ARCHIVED).exists() or not manifest.is_file():
            continue
        touched = max(path.stat().st_mtime, manifest.stat().st_mtime)
        if touched < cutoff:
            old.append((touched, path))
    return sorted(old, key=lambda item: (item[0], item[1].name))


def _old_backups(root, keep_days):
    directory = Path(root) / 'backups'
    if not directory.is_dir():
        return []
    cutoff = time.time() - keep_days * 86400
    old = [
        (path.stat().st_mtime, path)
        for path in directory.glob('gsd-*.sqlite')
        if path.is_file() and path.stat().st_mtime < cutoff
    ]
    return sorted(old, key=lambda item: (item[0], item[1].name))


def _tree_bytes(entry):
    if entry.is_dir() and not entry.is_symlink():
        return sum(
            path.stat().st_size
            for path in entry.rglob('*')
            if path.is_file() and not path.is_symlink()
        )
    return entry.stat().st_size


def _archive(root, path, remote, scratch):
    """Upload one step's cold copy, prove it, write archived.json, then prune."""
    name = path.name
    tar = scratch / f'{name}.tar.zst'
    target = f'{remote}steps/{name}.tar.zst'
    try:
        result = subprocess.run(
            ['tar', '--zstd', '-cf', str(tar), '-C', str(root / 'steps'), name],
            capture_output=True,
        )
        if result.returncode:
            raise ValueError(f'step {name}: tar failed (exit {result.returncode})')
        digest = _sha1(tar)
        size = tar.stat().st_size
        result = subprocess.run(
            ['rclone', 'copyto', '--immutable', str(tar), target], capture_output=True
        )
        if result.returncode:
            raise ValueError(f'step {name}: rclone copyto failed (exit {result.returncode})')
        if _remote_sha1(target) != digest:
            raise ValueError(f'step {name}: remote sha1 does not match the local tar')
    except (ValueError, OSError):
        tar.unlink(missing_ok=True)
        raise
    stamp = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    (path / ARCHIVED).write_text(
        json.dumps({'remote': target, 'sha1': digest, 'bytes': size, 'at': stamp}) + '\n'
    )
    freed = 0
    for entry in list(path.iterdir()):
        if entry.name in KEPT_IN_STEP or entry.name == ARCHIVED:
            continue
        freed += _tree_bytes(entry)
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    tar.unlink()
    return freed


def sweep(root, keep_days=KEEP_DAYS, dry_run=False):
    """Archive old steps and drop old backups that the remote still has, verified."""
    root = Path(root)
    steps = _old_steps(root, keep_days)[:MAX_STEPS]
    backups = _old_backups(root, keep_days)
    if dry_run:
        return {
            'steps_archived': 0,
            'steps_bytes_freed': 0,
            'backups_removed': 0,
            'kept_unverified': 0,
            'dry_run': True,
            'would_archive': [path.name for _, path in steps],
            'would_remove': [path.name for _, path in backups],
        }
    remote = owner.get('backup_remote') if steps or backups else None
    scratch = root / 'scratch'
    freed = 0
    if steps and not scratch.is_dir():
        scratch.mkdir(parents=True, mode=0o700)
    for _, path in steps:
        freed += _archive(root, path, remote, scratch)
    removed = 0
    kept_unverified = 0
    for _, path in backups:
        if _remote_sha1(f'{remote}{path.name}') == _sha1(path):
            path.unlink()
            removed += 1
        else:
            kept_unverified += 1
    return {
        'steps_archived': len(steps),
        'steps_bytes_freed': freed,
        'backups_removed': removed,
        'kept_unverified': kept_unverified,
        'dry_run': False,
    }
