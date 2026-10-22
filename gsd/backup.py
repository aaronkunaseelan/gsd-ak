"""Consistent private snapshots; reconcile verified backups by immutable directory copy."""

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from . import db as store
from . import owner


def backup(path, directory=None, upload=True):
    root = Path(directory or Path.home() / '.local/share/gsd/backups')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    verified = root / f'gsd-{stamp}.sqlite'
    target = root / f'.gsd-{stamp}.unverified'
    # Refuse an advancing source rather than uploading an unverified snapshot.
    with store.connect(path) as source:
        before = store.maxima(source)
        source_counts = {
            t: source.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in store.TABLES
        }
        source.execute('VACUUM INTO ?', (str(target),))
        if store.maxima(source) != before:
            raise ValueError('source advanced during backup; retry')
    os.chmod(target, 0o600)
    with store.connect(target) as check:
        if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('backup integrity check failed')
        counts = {t: check.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in store.TABLES}
        if store.maxima(check) != before or counts != source_counts:
            raise ValueError('source advanced during backup; retry')
    target.rename(verified)
    target = verified
    if upload:
        result = subprocess.run(
            [
                'rclone', 'copy', '--immutable', '--checksum',
                '--include', 'gsd-*.sqlite', str(root),
                owner.get('backup_remote'),
            ],
            capture_output=True,
        )
        if result.returncode:
            raise ValueError(f'rclone backup failed (exit {result.returncode}); snapshots retained')
    return {'object': target.name, 'counts': counts, 'uploaded': upload}
