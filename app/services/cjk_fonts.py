"""Known local font faces; never substitute a configured missing font."""
from pathlib import Path
import sys

MAC_CJK_FONT = Path('/System/Library/Fonts/STHeiti Medium.ttc')


def mac_font_candidates():
    return (MAC_CJK_FONT,) if sys.platform == 'darwin' else ()


def font_face_index(path):
    # The shipped collection contains TC at index 0, Simplified Chinese at 1.
    return 1 if sys.platform == 'darwin' and Path(path).resolve() == MAC_CJK_FONT else 0


def font_size_limit(path):
    # The immutable macOS TTC is about 56 MB and contains two faces. Custom
    # fonts retain the original 50 MB cap; this exception names only that file.
    if sys.platform == 'darwin' and Path(path).resolve() == MAC_CJK_FONT:
        return 64_000_000
    return 50_000_000
