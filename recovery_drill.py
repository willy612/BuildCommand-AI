"""Offline rehearsal of SQLite + uploaded-file recovery into a NEW directory.

Run against a quiesced COPY, never as a live-production backup mechanism.
PostgreSQL production backup/restore requires its native tooling and environment.
"""
import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

def drill(database,uploads,destination):
    database=Path(database).resolve();uploads=Path(uploads).resolve();destination=Path(destination).resolve()
    if destination.exists():raise ValueError('Use a new, empty destination path; existing data is never overwritten.')
    if uploads==destination or uploads in destination.parents:raise ValueError('Choose a destination outside the upload tree.')
    if not database.is_file() or not uploads.is_dir():raise ValueError('Choose an existing SQLite copy and upload directory.')
    source=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)
    try:
        if source.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Source database integrity check failed.')
        destination.mkdir(parents=True,exist_ok=False)
        target=sqlite3.connect(destination/'restored.sqlite')
        try:source.backup(target)
        finally:target.close()
    finally:source.close()
    manifest={}
    for path in sorted(uploads.rglob('*')):
        if path.is_symlink():raise ValueError('Symlink uploads need an explicit recovery plan.')
        if not path.is_file():continue
        relative=path.relative_to(uploads);copied=destination/'uploads'/relative;copied.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,copied)
        with path.open('rb') as src:first=hashlib.file_digest(src,'sha256').hexdigest()
        with copied.open('rb') as dst:second=hashlib.file_digest(dst,'sha256').hexdigest()
        if first!=second:raise ValueError('An uploaded file changed or did not copy correctly.')
        manifest[str(relative)]=first
    with sqlite3.connect(destination/'restored.sqlite') as restored:
        if restored.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Restored database integrity check failed.')
    result=dict(scope='Quiesced SQLite copy and uploaded-file copy rehearsal; not production PostgreSQL proof.',status='ok',files=len(manifest),file_hashes=manifest)
    (destination/'recovery-evidence.json').write_text(json.dumps(result,indent=2));return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--database',required=True);p.add_argument('--uploads',required=True);p.add_argument('--new-destination',required=True);a=p.parse_args();r=drill(a.database,a.uploads,a.new_destination);print(json.dumps({k:v for k,v in r.items() if k!='file_hashes'}))
