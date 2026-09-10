"""Consistent local SQLite backup before deploying additive schema changes.

Output stays in gitignored _backup/. Does not change source data or require a service stop.

    python backup_store.py
"""
from datetime import datetime
from pathlib import Path
import sqlite3
import _env  # noqa: F401
import db


def main():
    target = Path(__file__).resolve().parent / '_backup' / ('household-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    target.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(db.DB_PATH.resolve().as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(target / 'family_manager.db') as dest:
            source.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity check failed')
    print('Verified backup: ' + str(target))


if __name__ == '__main__':
    main()
