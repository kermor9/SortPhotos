#!/usr/bin/env python3
"""Sync the index database with the actual filesystem.

Compares the .sort_photos_index.db with the destination folder and updates the database:
- Removes entries for files that no longer exist on disk
- Adds entries for new files that exist on disk but not in the database

Files are identified by path. New files are hashed using SHA1 (same as compute_quick_hash).

Usage: python sync_index.py --dest /path/to/destination [--dry-run] [--verbose]
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional

try:
    from tqdm import tqdm
    _HAS_TQDM = True
except ImportError:
    tqdm = None  # type: ignore
    _HAS_TQDM = False

# Import from photo_utils
import sys
sys.path.insert(0, str(Path(__file__).parent))
from photo_utils import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, compute_quick_hash, load_ignore_patterns, should_ignore

# SQLite variable limit for chunked operations
SQL_MAX_VARS = 999

# Ignore file name
IGNORE_FILE = ".sort_photos_ignore"


def setup_logging(verbose: bool = False) -> None:
    """Setup logging configuration."""
    log_level = logging.DEBUG if verbose else logging.INFO
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logging.basicConfig(level=log_level, format=log_format)


def load_ignore_patterns(folder: Path) -> set[str]:
    """Load ignore patterns from .sort_photos_ignore file."""
    ignore_path = folder / IGNORE_FILE
    patterns = set()
    
    if not ignore_path.exists():
        return patterns
    
    try:
        with open(ignore_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.add(line)
    except Exception:
        pass
    
    return patterns


def should_ignore(path: Path, patterns: set[str]) -> bool:
    """Check if a path should be ignored based on patterns."""
    if not patterns:
        return False
    
    # Check each part of the path for folder name matches
    parts = path.parts
    for part in parts:
        if part in patterns:
            return True
    
    # Check file name against glob patterns
    if path.is_file():
        filename = path.name
        for pattern in patterns:
            if "*" in pattern or "?" in pattern:
                if fnmatch.fnmatch(filename, pattern):
                    return True
    
    return False


def get_index_db_path(dest_folder: Path) -> Path:
    """Get the path to the index database for the destination folder."""
    return dest_folder / ".sort_photos_index.db"


def load_db_paths(db_path: Path) -> Set[str]:
    """Load all file paths from the index database.
    
    Returns:
        Set of file paths stored in the database
    """
    logger = logging.getLogger(__name__)
    paths = set()
    
    if not db_path.exists():
        logger.warning(f"Index database not found: {db_path}")
        return paths
    
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        
        # Check if table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='files'")
        if not cur.fetchone():
            logger.warning("No 'files' table in database")
            conn.close()
            return paths
        
        # Load all paths
        cur.execute("SELECT path FROM files")
        for (path,) in cur.fetchall():
            if path:
                paths.add(path)
        
        conn.close()
        logger.info(f"Loaded {len(paths)} paths from database")
        
    except Exception as e:
        logger.error(f"Error loading database: {e}")
    
    return paths


def scan_filesystem(dest_folder: Path, ignore_patterns: set[str] = None) -> Set[str]:
    """Scan the destination folder for supported files.
    
    Args:
        dest_folder: Folder to scan
        ignore_patterns: Patterns to ignore
        
    Returns:
        Set of absolute file paths (as strings) found on disk
    """
    logger = logging.getLogger(__name__)
    paths = set()
    
    if ignore_patterns is None:
        ignore_patterns = set()
    
    supported_exts = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
    
    # Convert to absolute path to match database format
    dest_folder = dest_folder.resolve()
    
    # Use os.walk for efficiency
    for root, dirs, files in os.walk(dest_folder):
        # Skip the index database folder if it exists
        if '.sort_photos_index.db' in root:
            continue
        
        # Check if any folder in path should be ignored
        root_path = Path(root)
        if should_ignore(root_path, ignore_patterns):
            continue
            
        for filename in files:
            ext = Path(filename).suffix.lower()
            if ext in supported_exts:
                full_path = os.path.join(root, filename)
                # Check file against ignore patterns
                if ignore_patterns and should_ignore(Path(full_path), ignore_patterns):
                    continue
                paths.add(full_path)
    
    logger.info(f"Found {len(paths)} supported files on disk")
    return paths


def compute_file_hash(path: str) -> Tuple[Optional[int], Optional[float], Optional[str]]:
    """Compute file metadata: size, mtime, and SHA1 hash.
    
    Returns:
        Tuple of (size, mtime, sha) or (None, None, None) on error
    """
    try:
        p = Path(path)
        stat = p.stat()
        file_hash = compute_quick_hash(p)
        if file_hash:
            return (stat.st_size, stat.st_mtime, file_hash)
    except Exception:
        pass
    return (None, None, None)


def chunked_delete(cur, paths: List[str], batch_size: int = SQL_MAX_VARS) -> int:
    """Delete paths in chunks to avoid SQL variable limit."""
    total_deleted = 0
    path_list = list(paths)
    
    for i in range(0, len(path_list), batch_size):
        chunk = path_list[i:i + batch_size]
        placeholders = ",".join("?" * len(chunk))
        cur.execute(f"DELETE FROM files WHERE path IN ({placeholders})", chunk)
        total_deleted += cur.rowcount
    
    return total_deleted


def sync_index(
    dest_folder: Path,
    dry_run: bool = False,
    verbose: bool = False,
    report_path: Optional[Path] = None,
) -> Tuple[int, int, int]:
    """Sync the index database with the filesystem.
    
    Args:
        dest_folder: Destination folder containing the index database
        dry_run: If True, only report changes without applying them
        verbose: Enable verbose logging
        report_path: Path to save CSV report of changes
        
    Returns:
        Tuple of (files_removed, files_added, files_unchanged)
    """
    logger = logging.getLogger(__name__)
    
    db_path = get_index_db_path(dest_folder)
    if not db_path.exists():
        logger.error(f"Index database not found: {db_path}")
        return (0, 0, 0)
    
    # Load ignore patterns
    ignore_patterns = load_ignore_patterns(dest_folder)
    if ignore_patterns:
        logger.info(f"Loaded {len(ignore_patterns)} ignore patterns")
    
    # Load current state
    logger.info("Loading database index...")
    db_paths = load_db_paths(db_path)
    
    logger.info("Scanning filesystem...")
    disk_paths = scan_filesystem(dest_folder, ignore_patterns)
    
    # Normalize paths for comparison (ensure consistent format)
    # DB stores absolute paths, so normalize disk paths to match
    disk_paths_normalized = {os.path.normpath(p) for p in disk_paths}
    db_paths_normalized = {os.path.normpath(p) for p in db_paths}
    
    # Find differences
    to_remove = db_paths_normalized - disk_paths_normalized  # In DB but not on disk
    to_add = disk_paths_normalized - db_paths_normalized    # On disk but not in DB
    unchanged = db_paths_normalized & disk_paths_normalized
    
    logger.info(f"Analysis: {len(unchanged)} unchanged, {len(to_remove)} to remove, {len(to_add)} to add")
    
    if dry_run:
        logger.info("DRY RUN - No changes will be applied")
        if to_remove:
            logger.info(f"Would remove {len(to_remove)} files from database:")
            for p in sorted(to_remove)[:10]:
                logger.info(f"  - {p}")
            if len(to_remove) > 10:
                logger.info(f"  ... and {len(to_remove) - 10} more")
        if to_add:
            logger.info(f"Would add {len(to_add)} files to database:")
            for p in sorted(to_add)[:10]:
                logger.info(f"  + {p}")
            if len(to_add) > 10:
                logger.info(f"  ... and {len(to_add) - 10} more")
        return (len(to_remove), len(to_add), len(unchanged))
    
    # Apply changes
    files_removed = 0
    files_added = 0
    
    if to_remove or to_add:
        logger.info("Applying changes to database...")
        
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            
            # Remove deleted files (in chunks to avoid SQL limit)
            if to_remove:
                logger.info(f"Removing {len(to_remove)} deleted files from database...")
                files_removed = chunked_delete(cur, list(to_remove))
                logger.info(f"Removed {files_removed} entries")
            
            # Add new files
            if to_add:
                logger.info(f"Adding {len(to_add)} new files to database...")
                current_time = time.time()
                
                # Prepare batch insert
                batch_data = []
                progress = tqdm(to_add, desc="Hashing new files", disable=not _HAS_TQDM)
                
                for path in progress:
                    size, mtime, file_hash = compute_file_hash(path)
                    if file_hash:
                        batch_data.append((path, size, mtime, file_hash, current_time, None))
                    else:
                        logger.warning(f"Failed to hash: {path}")
                
                if batch_data:
                    cur.executemany(
                        "INSERT OR REPLACE INTO files (path, size, mtime, sha, updated_at, datetime) VALUES (?, ?, ?, ?, ?, ?)",
                        batch_data
                    )
                    files_added = len(batch_data)
                    logger.info(f"Added {files_added} entries")
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"Error applying changes: {e}")
            raise
    
    # Generate report
    if report_path and (to_remove or to_add):
        logger.info(f"Generating report: {report_path}")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(report_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["action", "path", "size", "mtime", "sha"])
            
            for path in sorted(to_remove):
                writer.writerow(["removed", path, "", "", ""])
            
            for path in sorted(to_add):
                size, mtime, file_hash = compute_file_hash(path)
                writer.writerow(["added", path, size or "", mtime or "", file_hash or ""])
    
    logger.info(f"Sync complete: {files_removed} removed, {files_added} added, {len(unchanged)} unchanged")
    
    return (files_removed, files_added, len(unchanged))


def main():
    parser = argparse.ArgumentParser(
        description="Sync the index database with the filesystem",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python sync_index.py --dest /path/to/photos
  python sync_index.py --dest /path/to/photos --dry-run
  python sync_index.py --dest /path/to/photos --verbose

Ignore file:
  Create .sort_photos_ignore in the destination folder with patterns to ignore:
    # Ignore folder by name
    Topics
    Favorites
    # Ignore files by pattern
    *.tmp
    *.bak
        """
    )
    parser.add_argument(
        "--dest",
        type=Path,
        required=True,
        help="Destination folder containing the index database"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without applying changes"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Path to save CSV report of changes"
    )
    
    args = parser.parse_args()
    
    setup_logging(verbose=args.verbose)
    
    if not args.dest.exists():
        print(f"Error: Destination folder does not exist: {args.dest}")
        return 1
    
    # Default report path is in the destination folder
    report_path = args.report
    if report_path is None:
        report_path = args.dest / "sync_report.csv"
    
    removed, added, unchanged = sync_index(
        dest_folder=args.dest,
        dry_run=args.dry_run,
        verbose=args.verbose,
        report_path=report_path,
    )
    
    print(f"\nSummary: {removed} removed, {added} added, {unchanged} unchanged")
    
    return 0


if __name__ == "__main__":
    exit(main())