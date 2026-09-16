"""Read-only module manifest verification. Never modifies live app data."""
import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path

def check(root,manifest):
    checks={}
    for name,expected in manifest['files'].items():
        path=root/name
        checks['file:'+name]=path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==expected
        if path.is_file() and path.suffix=='.py':
            try:ast.parse(path.read_text(encoding='utf-8'));checks['syntax:'+name]=True
            except (SyntaxError,UnicodeError):checks['syntax:'+name]=False
    for package in ('fastapi','pypdf','PIL','reportlab'):checks['dependency:'+package]=importlib.util.find_spec(package) is not None
    return dict(version=manifest['version'],status='ok' if all(checks.values()) else 'attention',checks=checks,passed=sum(checks.values()),total=len(checks),scope='Local file and dependency checks only. No service or database was opened.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',default='.');parser.add_argument('--manifest',default='release_manifest_8_24_0.json');a=parser.parse_args();root=Path(a.root).resolve();result=check(root,json.loads((root/a.manifest).read_text()));print(json.dumps(result));raise SystemExit(0 if result['status']=='ok' else 1)
