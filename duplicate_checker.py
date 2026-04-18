#!/usr/bin/env python3
"""DuplicateChecker: Check for duplicates in a folder structure and move them to a Duplicate folder.

Uses the same index as SortPhotos for tracking processed files.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
from pathlib import Path
import sys
from typing import List, Tuple
try:
    from tqdm import tqdm
    _HAS_TQDM = True
except Exception:
    tqdm = None  # type: ignore
    _HAS_TQDM = False

from photo_utils import (
    IMAGE_EXTENSIONS, VIDEO_EXTENSIONS,
    FileMetadata, IndexManager,
    setup_logging, compute_quick_hash, safe_move,
    load_ignore_patterns, should_ignore
)


class DuplicateChecker:
    """Checks for duplicates in a folder and moves them to a duplicate folder."""

    def __init__(
        self,
        source: Path,
        duplicate_path: Path,
        index_manager: IndexManager,
        dry_run: bool = False,
        recursive: bool = False,
        workers: int = None
    ):
        self.source = source
        self.duplicate_path = duplicate_path
        self.index_manager = index_manager
        self.dry_run = dry_run
        self.recursive = recursive
        self.workers = workers or max(2, (os.cpu_count() or 1) * 2)

        # Load ignore patterns from source folder
        self.ignore_patterns = load_ignore_patterns(source)
        if self.ignore_patterns:
            self.logger.info(f"Loaded {len(self.ignore_patterns)} ignore patterns from source")

        self.logger = logging.getLogger(__name__)
        self.stats = {"duplicates_moved": 0, "files_processed": 0}

    def process_files(self) -> List[Tuple[str, str, str, str]]:
        """Process all files and return duplicate report."""
        source_files = self._collect_source_files()
        self.logger.info(f"Found {len(source_files)} files to check")

        duplicate_rows = []

        # Process files with progress bar
        iterator = tqdm(source_files, desc="Checking", unit="file") if _HAS_TQDM else source_files

        for file_path in iterator:
            try:
                metadata = self._process_single_file(file_path)
                if metadata.is_corrupted:
                    self.logger.warning(f"Skipping corrupted file: {file_path}")
                    continue

                # Check for duplicate
                if self._is_duplicate(metadata):
                    # Move to duplicate folder
                    moved_to = safe_move(metadata.path, self.duplicate_path / metadata.path.name, self.dry_run)
                    metadata.moved_path = moved_to
                    duplicate_rows.append((metadata.path.name, str(metadata.path), str(moved_to)))
                    self.stats["duplicates_moved"] += 1
                    self.logger.info(f"Moved duplicate: {metadata.path} -> {moved_to}")
                else:
                    # Add to index
                    if not self.dry_run:
                        self.index_manager.add_file(metadata.path, metadata.file_hash)
                    self.logger.debug(f"Added to index: {metadata.path}")

                self.stats["files_processed"] += 1

            except Exception as e:
                self.logger.exception(f"Error processing {file_path}: {e}")

        self.logger.info(f"Summary: processed={self.stats['files_processed']}, duplicates_moved={self.stats['duplicates_moved']}")
        return duplicate_rows

    def _collect_source_files(self) -> List[Path]:
        """Collect all source files to process."""
        source_files = []
        if self.recursive:
            for p in self.source.rglob("*"):
                # Check if folder should be ignored
                if should_ignore(p, self.ignore_patterns):
                    continue
                if p.is_file() and self._is_supported_file(p):
                    # Check if file should be ignored
                    if should_ignore(p, self.ignore_patterns):
                        continue
                    source_files.append(p)
        else:
            for p in self.source.iterdir():
                # Check if folder should be ignored
                if should_ignore(p, self.ignore_patterns):
                    continue
                if p.is_file() and self._is_supported_file(p):
                    # Check if file should be ignored
                    if should_ignore(p, self.ignore_patterns):
                        continue
                    source_files.append(p)
        return source_files

    def _is_supported_file(self, path: Path) -> bool:
        """Check if file extension is supported."""
        ext = path.suffix.lower()
        return ext in IMAGE_EXTENSIONS or ext in VIDEO_EXTENSIONS

    def _process_single_file(self, file_path: Path) -> FileMetadata:
        """Process a single file and extract its metadata."""
        try:
            # Compute content hash
            file_hash = compute_quick_hash(file_path)
            if not file_hash:
                return FileMetadata(
                    path=file_path,
                    file_hash="",
                    size=0,
                    mtime=0,
                    is_video=False,
                    is_corrupted=True,
                    error_message="hash_failed"
                )

            return FileMetadata(
                path=file_path,
                file_hash=file_hash,
                size=file_path.stat().st_size,
                mtime=file_path.stat().st_mtime,
                is_video=file_path.suffix.lower() in VIDEO_EXTENSIONS,
                is_corrupted=False
            )

        except Exception as e:
            return FileMetadata(
                path=file_path,
                file_hash="",
                size=0,
                mtime=0,
                is_video=False,
                is_corrupted=True,
                error_message=str(e)
            )

    def _is_duplicate(self, metadata: FileMetadata) -> bool:
        """Check if the file is a duplicate based on the index."""
        return self.index_manager.is_duplicate_hash(metadata.file_hash) is not None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check for duplicates in a folder and move them to Duplicate folder")
    parser.add_argument("--source", "-s", required=True, help="Source folder to scan")
    parser.add_argument("--duplicate", "-d", default=None, help="Duplicate folder (default: <source>/Duplicate)")
    parser.add_argument("--index-file", default=None, help="Index file (default: <source>/.duplicate_index.db)")
    parser.add_argument("--log", help="Log file path (default: <source>/duplicate_checker.log)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--dry-run", action="store_true", help="Don't move files; just show planned actions")
    parser.add_argument("--recursive", action="store_true", help="Scan subdirectories recursively")
    parser.add_argument("--report", "-r", default="duplicate_report.csv", help="CSV report path for moved duplicates")

    args = parser.parse_args(argv)

    source = Path(args.source).expanduser().resolve()
    if args.duplicate:
        duplicate_path = Path(args.duplicate).expanduser().resolve()
    else:
        duplicate_path = source / "Duplicate"

    if args.index_file:
        index_file = Path(args.index_file).expanduser().resolve()
    else:
        index_file = source / ".duplicate_index.db"

    # Setup logging
    if args.log:
        log_path = Path(args.log).expanduser().resolve()
    else:
        log_path = source / "duplicate_checker.log"
    setup_logging(log_path, args.verbose)

    logging.getLogger(__name__).info(f"Log file: {log_path}")

    if not source.exists() or not source.is_dir():
        logging.getLogger(__name__).error(f"Source folder does not exist or is not a directory: {source}")
        sys.exit(2)

    # Create index manager
    index_manager = IndexManager(index_file)

    # Create and run checker
    checker = DuplicateChecker(
        source=source,
        duplicate_path=duplicate_path,
        index_manager=index_manager,
        dry_run=args.dry_run,
        recursive=args.recursive,
    )

    duplicate_rows = checker.process_files()

    # Write report
    if not args.dry_run and duplicate_rows:
        report_path = Path(args.report).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["filename", "original_path", "moved_to"])
            for row in duplicate_rows:
                writer.writerow(row)
        logging.getLogger(__name__).info(f"Report written to: {report_path}")


if __name__ == "__main__":
    main()