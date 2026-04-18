#!/usr/bin/env python3
"""SortPhotos: rename and move photos by their metadata (EXIF).

Features:
- Rename files to `YYYY-MM-DD HH.MM.SS.ext` using EXIF DateTimeOriginal or fallback to DateTime/file mtime
- Move unique photos to `dest/YYYY/MM/`
- Files with timestamps within time tolerance get incremental suffixes (-1, -2, etc.) instead of being quarantined
- Optional perceptual hashing for visual similarity detection
- Only identical content (same hash) gets quarantined as true duplicates
- Log corrupted/unreadable files to a CSV report for later processing

Usage: python sort_photos.py --source /path/to/photos --dest /path/to/sorted [--use-perceptual-hash]
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import shutil
import sys
from typing import Optional, Dict, List, Tuple
import concurrent.futures
try:
    from tqdm import tqdm
    _HAS_TQDM = True
except Exception:
    tqdm = None  # type: ignore
    _HAS_TQDM = False

from photo_utils import (
    IMAGE_EXTENSIONS, VIDEO_EXTENSIONS,
    FileMetadata, IndexManager, DuplicateHandler,
    get_video_datetime, setup_logging, get_exif_data, file_mtime_datetime,
    safe_move, format_filename, compute_quick_hash,
    load_ignore_patterns, should_ignore
)





def rebuild_db_from_dest(index_file: Path, dest: Path, workers: Optional[int] = None) -> None:
    """Rebuild the index database by scanning the destination directory.
    
    Args:
        index_file: Path to the index database file.
        dest: Destination directory to scan recursively.
        workers: Number of worker threads for parallel hash computation. Defaults to CPU count.
        
    Raises:
        ValueError: If dest directory does not exist.
    """
    logger = logging.getLogger(__name__)
    
    if not dest.is_dir():
        raise ValueError(f"Destination directory does not exist: {dest}")
    
    logger.info(f"Rebuilding index file {index_file} for directory: {dest}")
    
    # Remove existing index file
    if index_file.exists():
        try:
            index_file.unlink()
            logger.debug(f"Deleted existing index file: {index_file}")
        except Exception as e:
            logger.error(f"Failed to delete existing index file {index_file}: {e}")
            raise
    
    # Create new index manager (this creates the empty db)
    try:
        index_manager = IndexManager(index_file)
        logger.debug(f"Created new index manager with database: {index_file}")
    except Exception as e:
        logger.error(f"Failed to create index manager: {e}")
        raise
    
    def _is_supported_file(path: Path) -> bool:
        """Check if file extension is supported."""
        ext = path.suffix.lower()
        return ext in IMAGE_EXTENSIONS or ext in VIDEO_EXTENSIONS
    
    def _extract_datetime(path: Path) -> Optional[datetime]:
        """Extract datetime from file metadata.
        
        Tries EXIF data for images, video metadata for videos, and falls back to mtime.
        Returns None if extraction fails.
        """
        try:
            ext = path.suffix.lower()
            
            # Try EXIF for images
            if ext in IMAGE_EXTENSIONS:
                try:
                    dt, _, _, _ = get_exif_data(path)
                    if dt:
                        logger.debug(f"Extracted EXIF datetime from {path.name}: {dt}")
                        return dt
                except Exception as e:
                    logger.debug(f"Failed to extract EXIF data from {path.name}: {e}")
            
            # Try video metadata extraction
            elif ext in VIDEO_EXTENSIONS:
                try:
                    dt = get_video_datetime(path)
                    if dt:
                        logger.debug(f"Extracted video datetime from {path.name}: {dt}")
                        return dt
                except Exception as e:
                    logger.debug(f"Failed to extract video datetime from {path.name}: {e}")
            
            # Fallback to file modification time
            dt = file_mtime_datetime(path)
            logger.debug(f"Using file mtime as fallback for {path.name}: {dt}")
            return dt
            
        except Exception as e:
            logger.warning(f"Error extracting datetime from {path}: {e}")
            return None
    
    def _process_file(path: Path) -> Optional[FileMetadata]:
        """Process a single file: compute hash and extract metadata.
        
        Returns FileMetadata if successful, None if file is corrupted or unsupported.
        """
        try:
            if not path.is_file():
                return None
            
            # Compute hash
            try:
                file_hash = compute_quick_hash(path)
                if not file_hash:
                    logger.warning(f"Failed to compute hash for {path.name}")
                    return None
            except Exception as e:
                logger.error(f"Error computing hash for {path}: {e}")
                return None
            
            # Get file stats
            try:
                stat_info = path.stat()
                size = stat_info.st_size
                mtime = stat_info.st_mtime
            except Exception as e:
                logger.error(f"Error getting file stats for {path}: {e}")
                return None
            
            # Extract datetime
            dt = _extract_datetime(path)
            
            # Create metadata
            metadata = FileMetadata(
                path=path,
                file_hash=file_hash,
                size=size,
                mtime=mtime,
                datetime=dt,
                is_video=path.suffix.lower() in VIDEO_EXTENSIONS,
            )
            
            return metadata
            
        except Exception as e:
            logger.error(f"Unexpected error processing file {path}: {e}")
            return None
    
    # Scan destination directory to collect all supported files
    logger.info("Scanning destination directory for supported files...")
    files_to_process: List[Path] = []
    
    # Load ignore patterns
    ignore_patterns = load_ignore_patterns(dest)
    if ignore_patterns:
        logger.info(f"Loaded {len(ignore_patterns)} ignore patterns")
    
    try:
        for root, dirs, files in os.walk(dest):
            # Check if folder should be ignored
            if should_ignore(Path(root), ignore_patterns):
                continue
            for file in files:
                path = Path(root) / file
                if _is_supported_file(path):
                    # Check if file should be ignored
                    if should_ignore(path, ignore_patterns):
                        continue
                    files_to_process.append(path)
    except Exception as e:
        logger.error(f"Error scanning directory {dest}: {e}")
        raise
    
    logger.info(f"Found {len(files_to_process)} supported files to process")
    
    if not files_to_process:
        logger.info("No supported files found in destination directory")
        return
    
    # Process files in parallel with progress tracking
    stats = {
        "total": len(files_to_process),
        "processed": 0,
        "added": 0,
        "failed": 0,
        "skipped": 0,
    }
    
    max_workers = workers or min(4, (os.cpu_count() or 1) + 1)
    
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            logger.info(f"Processing files with {max_workers} worker threads...")
            
            # Submit all files for processing
            future_to_path = {executor.submit(_process_file, path): path for path in files_to_process}
            
            # Use tqdm for progress bar if available
            iterator = tqdm(concurrent.futures.as_completed(future_to_path), 
                          total=len(future_to_path), 
                          desc="Processing") if _HAS_TQDM else concurrent.futures.as_completed(future_to_path)
            
            for future in iterator:
                path = future_to_path[future]
                stats["processed"] += 1
                
                try:
                    metadata = future.result()
                    if metadata:
                        # Add to database
                        try:
                            index_manager.add_file(metadata)
                            stats["added"] += 1
                            logger.debug(f"Added to index: {path.name} (hash: {metadata.file_hash[:16]}...)")
                        except Exception as e:
                            logger.error(f"Failed to add {path.name} to index: {e}")
                            stats["failed"] += 1
                    else:
                        logger.debug(f"Skipped processing {path.name} (unable to compute metadata)")
                        stats["skipped"] += 1
                except Exception as e:
                    logger.error(f"Error processing result for {path}: {e}")
                    stats["failed"] += 1
    
    except Exception as e:
        logger.error(f"Error during parallel processing: {e}")
        raise
    
    # Log final statistics
    logger.info(
        f"Database rebuild complete: {stats['added']} added, "
        f"{stats['failed']} failed, {stats['skipped']} skipped "
        f"(total scanned: {stats['total']})"
    )



class PhotoProcessor:
    """Main processor for sorting and organizing photo/video files."""

    def __init__(
        self,
        source: Path,
        dest: Path,
        duplicate_path: Path,
        quarantine_path: Path,
        index_manager: IndexManager,
        duplicate_handler: DuplicateHandler,
        dry_run: bool = False,
        recursive: bool = False,
        workers: Optional[int] = None
    ):
        self.source = source
        self.dest = dest
        self.duplicate_path = duplicate_path
        self.quarantine_path = quarantine_path
        self.index_manager = index_manager
        self.duplicate_handler = duplicate_handler
        self.dry_run = dry_run
        self.recursive = recursive
        self.workers = workers or max(2, (os.cpu_count() or 1))

        # Load ignore patterns from source folder
        self.ignore_patterns = load_ignore_patterns(source)
        if self.ignore_patterns:
            self.logger.info(f"Loaded {len(self.ignore_patterns)} ignore patterns from source")

        self.logger = logging.getLogger(__name__)
        self.stats = defaultdict(int)
        self.logger.debug(f"Initialized PhotoProcessor with source={source}, dest={dest}, dry_run={dry_run}, recursive={recursive}, workers={self.workers}")

    def process_files(self) -> Tuple[List[Tuple[str, str, str, str]], List[Tuple[str, str, str]], List[Tuple[str, str, str, str]], List[Tuple[str, str, str, str]]]:
        """Process all files and return reports."""
        source_files = self._collect_source_files()
        self.logger.info(f"Found {len(source_files)} files to process")

        corrupted_rows = []
        duplicate_rows = []
        name_collision_rows = []
        events_rows = []

        # Process metadata in parallel and handle duplicate hashes immediately
        metadata_list = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(self._process_single_file, f): f for f in source_files}
            for future in tqdm(concurrent.futures.as_completed(futures), total=len(source_files), desc="Extracting metadata", unit="file") if _HAS_TQDM else concurrent.futures.as_completed(futures):
                file_path = futures[future]
                try:
                    metadata = future.result()
                    metadata_list.append(metadata)
                except Exception as e:
                    self.logger.exception(f"Error extracting metadata for {file_path}: {e}")
                    # Create corrupted metadata
                    metadata_list.append(FileMetadata(
                        path=file_path,
                        datetime=None,
                        file_hash="",
                        size=0,
                        mtime=0,
                        is_video=False,
                        is_corrupted=True,
                        error_message=str(e)
                    ))

        # Process placements sequentially
        for metadata in tqdm(metadata_list, desc="Placing files", unit="file") if _HAS_TQDM else metadata_list:
            if metadata.is_corrupted:
                corrupted_rows.append((metadata.path.name, str(metadata.path), metadata.error_message))
                events_rows.append(("corrupted", metadata.path.name, str(metadata.path), "", metadata.error_message))
                self.stats["corrupted"] += 1
                logging.warning(f"File is corrupted/unreadable: {metadata.path}, error: {metadata.error_message}")
                continue

            # Handle duplicates and move file
            result = self._handle_file_placement(metadata)
            events_rows.extend(result["events"])
            duplicate_rows.extend(result["duplicates"])
            name_collision_rows.extend(result["collisions"])

        self.logger.info(f"Summary: moved={self.stats['moved']}, quarantined_duplicates={self.stats['quarantined_duplicates']}, corrupted={self.stats['corrupted']}")
        print(f"Summary: moved={self.stats['moved']}, quarantined_duplicates={self.stats['quarantined_duplicates']}, corrupted={self.stats['corrupted']}")
        
        # Flush remaining database batch
        self.index_manager.flush_batch()
        
        return events_rows, corrupted_rows, duplicate_rows, name_collision_rows

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
        """Process a single file and extract its metadata and handle exact duplicates."""
        try:
            # Compute content hash
            file_hash = compute_quick_hash(file_path)
            self.logger.debug(f"Computed hash for {file_path.name}: {file_hash[:16]}...")
            if not file_hash:
                self.logger.warning(f"Failed to compute hash for {file_path}, treating as corrupted")
                return FileMetadata(
                    path=file_path,
                    datetime=None,
                    file_hash="",
                    size=0,
                    mtime=0,
                    is_video=False,
                    is_corrupted=True,
                    error_message="hash_failed",
                    make=None,
                    model=None,
                    software=None
                )

            # Check if exact duplicate
            existing_path = self.index_manager.is_duplicate_hash(file_hash)
            self.logger.debug(f"Checking for duplicate hash {file_hash[:16]}...")
            if existing_path:
                # Move to duplicate folder immediately
                moved_to = self._move_to_folder(file_path, existing_path, self.duplicate_path, "exact duplicate")
                self.stats["quarantined_duplicates"] += 1
                return FileMetadata(
                    path=file_path,
                    datetime=None,
                    file_hash=file_hash,
                    size=moved_to.stat().st_size,
                    mtime=moved_to.stat().st_mtime,
                    is_video=False,
                    is_corrupted=False,
                    make=None,
                    model=None,
                    software=None,
                    moved_path=moved_to,
                )

            # Extract datetime and camera info
            self.logger.debug(f"Extracting datetime and camera info for {file_path.name}")
            is_video = file_path.suffix.lower() in VIDEO_EXTENSIONS
            dt, make, model, software = None, None, None, None
            if is_video:
                dt = get_video_datetime(file_path)
            else:
                try:
                    dt, make, model, software = get_exif_data(file_path)
                    logging.debug(f"EXIF data for {file_path.name}: datetime={dt}, make={make}, model={model}")
                except Exception as e:
                    self.logger.warning(f"Failed to extract EXIF data for {file_path}: {e}")
                    return FileMetadata(
                        path=file_path,
                        datetime=None,
                        file_hash=file_hash,
                        size=file_path.stat().st_size,
                        mtime=file_path.stat().st_mtime,
                        is_video=is_video,
                        is_corrupted=True,
                        error_message=f"unreadable: {e}",
                        make=make,
                        model=model,
                        software=software
                    )

            if dt is None:
                dt = file_mtime_datetime(file_path)

            return FileMetadata(
                path=file_path,
                datetime=dt,
                file_hash=file_hash,
                size=file_path.stat().st_size,
                mtime=file_path.stat().st_mtime,
                is_video=is_video,
                is_corrupted=False,
                make=make,
                model=model,
                software=software
            )

        except Exception as e:
            self.logger.exception(f"Unexpected error processing {file_path}: {e}")
            return FileMetadata(
                path=file_path,
                datetime=None,
                file_hash="",
                size=0,
                mtime=0,
                is_video=False,
                is_corrupted=True,
                error_message=str(e),
                make=None,
                model=None,
                software=None
            )

    def _handle_file_placement(self, metadata: FileMetadata) -> Dict[str, List]:
        """Handle file placement and duplicate detection.
        
        Uses should_quarantine_as_duplicate to consistently handle all conflicts:
        - Files matching others in the same datetime group
        - Files conflicting with existing destination files
        """
        events = []
        duplicates = []
        collisions = []

        # Check if file has already been quarantined as an exact duplicate during metadata extraction
        if metadata.moved_path and metadata.moved_path.parent == self.duplicate_path:
            self.logger.debug(f"File {metadata.path} was already moved to duplicate folder during metadata extraction, skipping placement")
            duplicate_reason = "exact duplicate"
            # Get the original file from index that this is a duplicate of
            existing_path = self.index_manager.is_duplicate_hash(metadata.file_hash)
            duplicate_of = str(existing_path) if existing_path else "Unknown"
            events.append(("Duplicated file", metadata.path.name, str(metadata.path), str(metadata.moved_path), duplicate_reason))
            duplicates.append((metadata.path.name, str(metadata.path), duplicate_of, duplicate_reason, str(metadata.moved_path)))
            return {"events": events, "duplicates": duplicates, "collisions": collisions}

        # Check against timestamp group (same datetime)
        suffix_number = self.duplicate_handler.get_filename_suffix(metadata)
        if suffix_number > 0:
            self.logger.debug(f"File {metadata.path} has same datetime as previous file(s), checking for duplicates")
            for count, existing_metadata in self.duplicate_handler.timestamp_groups[metadata.datetime][:-1]:
                is_duplicate, duplicate_reason = self.duplicate_handler.should_quarantine_as_duplicate(metadata, existing_metadata)
                if is_duplicate:
                    self.duplicate_handler.timestamp_groups[metadata.datetime].pop()
                    self.logger.info(f"Quarantine (timestamp match): {metadata.path} - {duplicate_reason}")
                    moved_to = self._move_to_folder(metadata.path, None, self.quarantine_path, duplicate_reason)
                    metadata.moved_path = moved_to
                    events.append(("Duplicated timestamp", metadata.path.name, str(metadata.path), str(moved_to), duplicate_reason))
                    duplicates.append((metadata.path.name, str(metadata.path), str(existing_metadata.path), duplicate_reason, str(moved_to)))
                    self.stats["quarantined_duplicates"] += 1
                    return {"events": events, "duplicates": duplicates, "collisions": collisions}

        # Determine target path
        dt = metadata.datetime
        base_name = format_filename(dt, metadata.path.suffix)
        if suffix_number > 0:
            stem = Path(base_name).stem
            final_name = f"{stem}-{suffix_number}{Path(base_name).suffix}"
        else:
            final_name = base_name

        year = dt.strftime("%Y")
        if metadata.is_video:
            target_dir = self.dest / year / "Video"
        else:
            month = dt.strftime("%m")
            target_dir = self.dest / year / month

        target_path = target_dir / final_name

        # Check if destination exists and if it's a duplicate
        if target_path.exists():
            self.logger.debug(f"Destination exists: {target_path}")
            target_metadata = self._get_target_metadata(target_path)
            if target_metadata:
                is_duplicate, duplicate_reason = self.duplicate_handler.should_quarantine_as_duplicate(metadata, target_metadata)
                if is_duplicate:
                    if suffix_number > 0:
                        self.duplicate_handler.timestamp_groups[metadata.datetime].pop()
                    self.logger.info(f"Quarantine (destination match): {metadata.path} - {duplicate_reason}")
                    moved_to = self._move_to_folder(metadata.path, None, self.quarantine_path, duplicate_reason)
                    metadata.moved_path = moved_to
                    events.append(("Duplicated target", metadata.path.name, str(metadata.path), str(moved_to), duplicate_reason))
                    duplicates.append((metadata.path.name, str(metadata.path), str(target_path), duplicate_reason, str(moved_to)))
                    self.stats["quarantined_duplicates"] += 1
                    return {"events": events, "duplicates": duplicates, "collisions": collisions}

            # Not a duplicate - name collision, move with suffix
            self.logger.debug(f"Name collision at {target_path}, moving with suffix")
            moved_to = safe_move(metadata.path, target_path, self.dry_run)
            metadata.moved_path = moved_to
            events.append(("Moved with name collision", metadata.path.name, str(metadata.path), str(moved_to), "name_collision"))
            collisions.append((metadata.path.name, str(metadata.path), str(target_path), str(moved_to)))
            if not self.dry_run:
                self.index_manager.add_file(metadata)
            self.stats["moved"] += 1
        else:
            # Normal move - no conflict
            self.logger.debug(f"Moving to: {target_path}")
            moved_to = safe_move(metadata.path, target_path, self.dry_run)
            metadata.moved_path = moved_to
            events.append(("Moved", metadata.path.name, str(metadata.path), str(moved_to), "Normal move"))
            if not self.dry_run:
                self.index_manager.add_file(metadata)
            self.stats["moved"] += 1

        moved_to = moved_to if target_path.exists() or self.dry_run else target_path
        metadata.moved_path = moved_to
        if suffix_number >= 0 and metadata.datetime in self.duplicate_handler.timestamp_groups:
            self.duplicate_handler.timestamp_groups[metadata.datetime][-1] = (suffix_number, metadata)

        return {"events": events, "duplicates": duplicates, "collisions": collisions}

    def _get_target_hash(self, target_path: Path) -> str:
        """Get the hash of an existing target file."""
        return compute_quick_hash(target_path)

    def _get_target_metadata(self, target_path: Path) -> Optional[FileMetadata]:
        """Reconstruct FileMetadata for an existing target file for duplicate comparison."""
        try:
            is_video = target_path.suffix.lower() in VIDEO_EXTENSIONS
            file_hash = compute_quick_hash(target_path)
            
            # Extract datetime and camera info
            dt, make, model, software = None, None, None, None
            if is_video:
                dt = get_video_datetime(target_path)
            else:
                try:
                    dt, make, model, software = get_exif_data(target_path)
                except Exception:
                    pass
            
            if dt is None:
                dt = file_mtime_datetime(target_path)
            
            return FileMetadata(
                path=target_path,
                file_hash=file_hash,
                size=target_path.stat().st_size,
                mtime=target_path.stat().st_mtime,
                is_video=is_video,
                datetime=dt,
                is_corrupted=False,
                make=make,
                model=model,
                software=software
            )
        except Exception as e:
            self.logger.debug(f"Could not reconstruct metadata for {target_path}: {e}")
            return None

    def _move_to_folder(self, src: Path, existing: Optional[Path], folder: Path, reason: str) -> Path:
        """Move a file to a quarantine/duplicate folder. Returns the path of the moved file."""
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / src.name
        # Handle name collisions in quarantine folder
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            i = 1
            while (folder / f"{stem}_{i}{suffix}").exists():
                i += 1
            target = folder / f"{stem}_{i}{suffix}"
            self.logger.warning(f"Name collision in quarantine folder, new target: {target}")
        
        if not self.dry_run:
            shutil.move(str(src), str(target))
        self.logger.info(f"[{'DRY' if self.dry_run else 'MOVED'}] {reason}: {src} -> {target}")
        return target




def main(argv=None):
    parser = argparse.ArgumentParser(description="Sort and rename photos by metadata")
    parser.add_argument("--source", "-s", required=True, help="Source folder to scan")
    parser.add_argument("--dest", "-d", default=None, help="Destination base folder (default: source)")
    parser.add_argument("--report", "-r", default="sort_report.csv", help="CSV report path for corrupted files")
    parser.add_argument(
        "--corrupt-report",
        default=None,
        help="CSV report path for corrupted/unreadable files (overrides --report)",
    )
    parser.add_argument(
        "--events-report",
        default=None,
        help="CSV report path for events (audit of all actions)",
    )
    parser.add_argument(
        "--duplicate-report",
        default=None,
        help="CSV report path for deleted content-duplicates (default: <dest>/duplicate_report.csv)",
    )
    parser.add_argument(
        "--name-report",
        default=None,
        help="CSV report path for name collisions (default: <dest>/name_collision_report.csv)",
    )
    parser.add_argument(
        "--quarantine",
        default=None,
        help="Quarantine folder for potential duplicates (default: <dest>/Quarantine)",
    )
    parser.add_argument(
        "--duplicate",
        default=None,
        help="Duplicate folder for confirmed exact duplicates (default: <dest>/Duplicate)",
    )
    parser.add_argument(
        "--index-file",
        default=None,
        help="SQLite database file (default: <dest>/.sort_photos_index.db)",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        default=False,
        help="Rebuild index (ignore existing index file and start fresh)",
    )
    parser.add_argument("--log", help="Log file path (default: <dest>/sort_photos.log)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose (debug) logging to console")
    parser.add_argument("--dry-run", action="store_true", help="Don't move or rename files; just show planned actions")
    parser.add_argument("--recursive", action="store_true", default=True, help="Scan subdirectories recursively")
    args = parser.parse_args(argv)

    source = Path(args.source).expanduser().resolve()
    if args.dest:
        dest = Path(args.dest).expanduser().resolve()
    else:
        dest = source

    # events and corrupt report paths
    if args.events_report:
        events_report = Path(args.events_report).expanduser().resolve()
    else:
        events_report = dest / "event_report.csv"

    if args.corrupt_report:
        corrupt_report = Path(args.corrupt_report).expanduser().resolve()
    else:
        corrupt_report = Path(args.report).expanduser().resolve()

    if args.duplicate_report:
        duplicate_report = Path(args.duplicate_report).expanduser().resolve()
    else:
        duplicate_report = dest / "duplicate_report.csv"

    if args.name_report:
        name_report = Path(args.name_report).expanduser().resolve()
    else:
        name_report = dest / "name_collision_report.csv"

    if args.quarantine:
        quarantine_path = Path(args.quarantine).expanduser().resolve()
    else:
        quarantine_path = dest / "Quarantine"

    if args.duplicate:
        duplicate_path = Path(args.duplicate).expanduser().resolve()
    else:
        duplicate_path = dest / "Duplicate"
    if args.index_file:
        index_file = Path(args.index_file).expanduser().resolve()
    else:
        index_file = dest / ".sort_photos_index.db"
    # configure logging
    if args.log:
        log_path = Path(args.log).expanduser().resolve()
    else:
        log_path = dest / "sort_photos.log"
    setup_logging(log_path, args.verbose)

    logging.getLogger(__name__).info(f"Log file: {log_path}")

    if not source.exists() or not source.is_dir():
        logging.getLogger(__name__).error(f"Source folder does not exist or is not a directory: {source}")
        sys.exit(2)

    # Create managers
    if args.rebuild_index:
        rebuild_db_from_dest(index_file, dest)
        sys.exit(0)

    index_manager = IndexManager(index_file)
    duplicate_handler = DuplicateHandler()

    # Create and run processor
    processor = PhotoProcessor(
        source=source,
        dest=dest,
        duplicate_path=duplicate_path,
        quarantine_path=quarantine_path,
        index_manager=index_manager,
        duplicate_handler=duplicate_handler,
        dry_run=args.dry_run,
        recursive=args.recursive,
        workers=None  # Use default number of workers based on CPU count
    )

    events_rows, corrupted_rows, duplicate_rows, name_collision_rows = processor.process_files()

    # Write reports
    if events_report and not args.dry_run and events_rows != []:
        events_report.parent.mkdir(parents=True, exist_ok=True)
        with events_report.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["event", "filename", "src", "dest", "note"])
            for ev in events_rows:
                writer.writerow(ev)

    if corrupt_report and not args.dry_run and corrupted_rows != []:
        corrupt_report.parent.mkdir(parents=True, exist_ok=True)
        with corrupt_report.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["filename", "path", "error"])
            for row in corrupted_rows:
                writer.writerow(row)

    if duplicate_report and not args.dry_run and duplicate_rows != []:
        duplicate_report.parent.mkdir(parents=True, exist_ok=True)
        with duplicate_report.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["filename", "from path", "duplicate of", "reason", "to path"])
            for row in duplicate_rows:
                writer.writerow(row)

    if name_report and not args.dry_run and name_collision_rows != []:
        name_report.parent.mkdir(parents=True, exist_ok=True)
        with name_report.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["filename", "original_path", "conflicted_target", "moved_to"])
            for row in name_collision_rows:
                writer.writerow(row)


if __name__ == "__main__":
    main()
