"""Developer build: put the native library beside the Python source package."""
import shutil
import subprocess
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.version import sync_version

ROOT = Path(__file__).resolve().parents[1]

if __name__ == '__main__':
    sync_version(ROOT)
    subprocess.run(['cargo', 'build', '--release', '--locked'], cwd=ROOT, check=True)
    filename = 'benchset.dll' if sys.platform == 'win32' else (
        'libbenchset.dylib' if sys.platform == 'darwin' else 'libbenchset.so'
    )
    destination = ROOT / 'benchset/_native'
    destination.mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'target/release' / filename, destination / filename)
    print(destination / filename)
