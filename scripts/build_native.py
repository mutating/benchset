"""Developer build: put the native library beside the Python source package."""
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.cargo import build, publish, target_directory
from scripts.version import sync_version

ROOT = Path(__file__).resolve().parents[1]

if __name__ == '__main__':
    sync_version(ROOT)
    metadata = build(ROOT, ['cargo', 'build', '--release', '--locked'])
    filename = 'benchset.dll' if sys.platform == 'win32' else (
        'libbenchset.dylib' if sys.platform == 'darwin' else 'libbenchset.so'
    )
    print(publish(ROOT / 'benchset', target_directory(ROOT) / 'release' / filename, metadata))
