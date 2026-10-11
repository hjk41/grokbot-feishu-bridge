#!/usr/bin/env python3
"""Move a pending event file to processed/. Usage: archive_pending.py <path>"""
import os, sys, shutil
from pathlib import Path
DIR = Path(__file__).resolve().parent
PENDING = DIR / "pending"
PROCESSED = DIR / "processed"

def archive(src):
    src = Path(src)
    if src.is_symlink():
        raise ValueError("symlinks are not accepted")
    src = src.resolve()
    if src.parent != PENDING.resolve() or src.suffix != ".json" or not src.is_file():
        raise ValueError("expected a JSON file directly inside pending/")
    PROCESSED.mkdir(mode=0o700, parents=True, exist_ok=True)
    dst = PROCESSED / src.name
    if dst.exists() or dst.is_symlink():
        raise ValueError("archive destination already exists")
    shutil.move(str(src), str(dst))
    return dst

def main():
    if len(sys.argv) != 2:
        print("usage: archive_pending.py <path>", file=sys.stderr)
        return 2
    os.umask(0o077)
    try:
        print(archive(sys.argv[1]))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
