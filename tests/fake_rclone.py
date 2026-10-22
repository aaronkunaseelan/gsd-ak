#!/usr/bin/env python3
"""Test-only immutable directory store. Never invokes a network client."""
import fnmatch
import json
import os
from pathlib import Path
import shutil
import sys

args = sys.argv[1:]
assert args[:3] == ['copy', '--immutable', '--checksum'], args
remote = Path(os.environ['FAKE_REMOTE'])
if os.environ.get('FAKE_RCLONE_LOG'):
    with open(os.environ['FAKE_RCLONE_LOG'], 'a') as out:
        out.write(json.dumps(args) + '\n')
if os.environ.get('FAKE_RCLONE_FAIL'):
    sys.exit(int(os.environ['FAKE_RCLONE_FAIL']))
pattern = args[args.index('--include') + 1] if '--include' in args else '*'
source = Path(args[-2])
assert source.is_dir(), args
assert args[-1] in ('test_remote:example-backups/gsd/steps/', 'test_remote:example-backups/gsd/'), args
root = remote / ('steps' if args[-1].endswith('/steps/') else 'backups')
root.mkdir(parents=True, exist_ok=True)
for path in sorted(source.rglob('*')):
    if not path.is_file() or not fnmatch.fnmatch(path.name, pattern):
        continue
    target = root / path.relative_to(source)
    if target.exists():
        if target.read_bytes() != path.read_bytes():
            sys.exit(6)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
