"""Diagnostic only: replay the observed database inspection on fresh fixture copies."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
from port import materialize

root = Path.home() / 'tbwalprobe'
out = Path(__file__).resolve().parent / '_out/db-wal-recovery-wal-probe'
out.mkdir(parents=True, exist_ok=True)
root.mkdir(parents=True, exist_ok=True)
log = []
cwd, env = materialize(Path(os.environ['TB_TASKS']) / 'db-wal-recovery', root, log)
rows = []
for mode in ['ro', 'rw']:
    case = root / mode
    case.mkdir(exist_ok=True)
    for name in ['main.db', 'main.db-wal']:
        shutil.copy2(cwd / name, case / name)
    wal = case / 'main.db-wal'
    before = hashlib.sha256(wal.read_bytes()).hexdigest()
    target = ('file:' + (case / 'main.db').as_posix() + '?mode=ro') if mode == 'ro' else str(case / 'main.db')
    p = subprocess.run(['sqlite3', target, 'SELECT * FROM sqlite_master; SELECT * FROM items;'],
                       env=env, capture_output=True, text=True, timeout=60)
    rows.append({'mode': mode, 'rc': p.returncode, 'wal_exists_after': wal.exists(),
                 'wal_sha256_before': before,
                 'wal_sha256_after': hashlib.sha256(wal.read_bytes()).hexdigest() if wal.exists() else None,
                 'stdout': p.stdout, 'stderr': p.stderr})
version = subprocess.run(['sqlite3', '--version'], env=env, capture_output=True, text=True, check=True).stdout.strip()
result = {'cell': os.environ.get('XOS_CELL'), 'platform': platform.platform(), 'sqlite': version, 'observations': rows}
(out / 'probe.json').write_text(json.dumps(result, indent=2), encoding="utf-8")
(out / 'port.log').write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
print('WAL_PROBE ' + json.dumps({'cell': result['cell'], 'sqlite': version, 'observations': [{k: row[k] for k in ('mode', 'rc', 'wal_exists_after', 'wal_sha256_before', 'wal_sha256_after')} for row in rows]}))
