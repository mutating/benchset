"""Exercise pip install from source in a fresh venv without Cargo on PATH."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    environment = os.environ.copy()
    names = ('cargo.exe', 'cargo')
    # CI hosts usually ship Rust. Hide its bin directories for this test so the
    # actual PEP 517 subprocess must use the automatic bootstrap path.
    environment['PATH'] = os.pathsep.join(
        entry for entry in environment.get('PATH', '').split(os.pathsep)
        if entry and not any((Path(entry) / name).is_file() for name in names)
    )
    if shutil.which('cargo', path=environment['PATH']) is not None:
        raise RuntimeError('Cargo is still visible in the source-install test')
    with tempfile.TemporaryDirectory(prefix='benchset-install-') as temporary:
        directory = Path(temporary)
        venv = directory / 'venv'
        subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
        windows = os.name == 'nt'
        python = venv / ('Scripts/python.exe' if windows else 'bin/python')
        command = venv / ('Scripts/benchset.exe' if windows else 'bin/benchset')
        subprocess.run([str(python), '-m', 'pip', 'install', str(ROOT)],
                       env=environment, cwd=directory, check=True)
        # Run outside the repository so imports come from the installed wheel.
        result = subprocess.run([
            str(command), '--bench', 'single_core', '--warmup', '0', '--duration', '0.05',
        ], env=environment, cwd=directory, text=True, capture_output=True, check=True)
        if 'single_core' not in result.stdout or 'completed' not in result.stdout:
            raise RuntimeError('the installed CLI did not complete a benchmark')
        print(result.stdout)
        subprocess.run([str(python), '-m', 'pip', 'check'], env=environment, cwd=directory, check=True)


if __name__ == '__main__':
    main()
