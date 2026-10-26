"""Derive Cargo metadata and documentation pins from [project].version."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def _validate(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value) is None:
        raise ValueError('project.version must be an X.Y.Z string')
    return value


def read_version(root: Path) -> str:
    configuration = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    project = configuration.get('project')
    if not isinstance(project, dict) or 'version' not in project:
        raise ValueError('Missing project.version in pyproject.toml')
    return _validate(project['version'])


def _table_pattern(table: str) -> str:
    # Scope the edited line to its table, leaving dependency/tool versions alone.
    return (r'(?m)(^[ \t]*\[' + re.escape(table) + r'\][^\n]*\n'
            r'(?:(?![ \t]*\[)[^\n]*\n)*?[ \t]*version[ \t]*=[ \t]*["\'](?!["\']))[^"\']*(["\'])')


def _replace(text: str, pattern: str, value: str, filename: str) -> str:
    updated, count = re.compile(pattern).subn(lambda match: match[1] + value + match[2], text)
    if count != 1:
        raise ValueError('Expected exactly one generated version in ' + filename)
    return updated


def sync_version(root: Path, new_version: Optional[str] = None, *, check: bool = False) -> str:
    if check and new_version is not None:
        raise ValueError('--check cannot change the version')
    value = read_version(root)
    if new_version is not None:
        value = _validate(new_version)
    patterns = {
        'Cargo.toml': (_table_pattern('package'),),
        'Cargo.lock': (r'(?m)(^\[\[package\]\][^\n]*\nname = "benchset"\nversion = ")[^"]*(")',),
        'README.md': (r'(?m)(^benchset = ")[^"]*(")', r'(\.exact\(")[^"]*("\))'),
    }
    if new_version is not None:
        patterns['pyproject.toml'] = (_table_pattern('project'),)
    changes = {}
    for filename, expressions in patterns.items():
        path = root / filename
        # Documentation and a lockfile are optional in minimal source checkouts.
        if filename in ('Cargo.lock', 'README.md') and not path.exists():
            continue
        original = path.read_text(encoding='utf-8')
        updated = original
        for expression in expressions:
            updated = _replace(updated, expression, value, filename)
        if updated != original:
            changes[path] = updated
    if check and changes:
        names = ', '.join(path.name for path in changes)
        raise ValueError('Generated versions must match project.version: ' + names
                         + '; run python scripts/version.py')
    # Validate every replacement before touching even the canonical source.
    for path, text in changes.items():
        path.write_text(text, encoding='utf-8')
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version', nargs='?', help='Set project.version and synchronize its generated copies.')
    parser.add_argument('--check', action='store_true', help='Fail on stale copies without writing any files.')
    arguments = parser.parse_args()
    if arguments.check and arguments.version is not None:
        parser.error('--check cannot be combined with a version')
    try:
        value = sync_version(Path(__file__).resolve().parents[1], arguments.version, check=arguments.check)
    except (OSError, ValueError) as error:
        sys.stderr.write(str(error) + '\n')
        return 1
    sys.stdout.write(value + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
