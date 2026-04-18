#!/usr/bin/env python3
"""Delete empty folders recursively."""

import argparse
import os
from pathlib import Path

def delete_empty_folders(root: Path, dry_run: bool = False) -> int:
    """Delete empty folders under root. Returns count of deleted folders."""
    deleted = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if not dirnames and not filenames:
            if dry_run:
                print(f"[DRY] Would delete empty folder: {dirpath}")
            else:
                os.rmdir(dirpath)
                print(f"Deleted empty folder: {dirpath}")
            deleted += 1
    return deleted

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Delete empty folders recursively")
    parser.add_argument("root", help="Root directory to scan")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without doing it")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.exists() or not root.is_dir():
        print(f"Error: {root} is not a valid directory")
        exit(1)

    deleted = delete_empty_folders(root, args.dry_run)
    print(f"{'Would delete' if args.dry_run else 'Deleted'} {deleted} empty folders")