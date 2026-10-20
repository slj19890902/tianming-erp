"""Build the offline macOS Vision helper into the immutable release payload."""
from pathlib import Path
import subprocess
import sys


def build_helper(tree: Path):
    if sys.platform != 'darwin':
        raise ValueError('macOS Vision helper must be built on Mac')
    source = tree / 'desktop_assistant/pdf_vision.swift'
    target = tree / 'runtime/bin/tianming-pdf-ocr'
    if not source.is_file() or source.is_symlink():
        raise ValueError('missing authenticated Vision helper source')
    target.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(['/usr/bin/xcrun','swiftc','-O',str(source),'-o',str(target)],
                   check=True,timeout=120,stdin=subprocess.DEVNULL,capture_output=True)
    target.chmod(0o755)
    # Native arm64 helper is signed by the release manifest, like runtime binaries.
    return target
