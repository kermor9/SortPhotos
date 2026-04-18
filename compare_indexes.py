#!/usr/bin/env python3
"""Compare two index databases and report differences.

Compares two .sort_photos_index.db files from different folders and generates reports for:
- Files only in the first database
- Files only in the second database  
- Files present in both databases (duplicates by hash)

Usage: python compare_indexes.py --db1 /path/to/index1.db --db2 /path/to/index2.db [--output /path/to/reports]
"""
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
from pathlib import Path
from typing import Dict, Tuple, List, Optional
from datetime import datetime

try:
    from tqdm import tqdm
    _HAS_TQDM = True
except ImportError:
    tqdm = None  # type: ignore
    _HAS_TQDM = False


def setup_logging(log_path: Optional[Path] = None, verbose: bool = False) -> None:
    """Setup logging configuration."""
    log_level = logging.DEBUG if verbose else logging.INFO
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    handlers = [logging.StreamHandler()]
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path))
    
    logging.basicConfig(level=log_level, format=log_format, handlers=handlers)


def load_index_db(db_path: Path) -> Dict[str, Tuple[int, float, str, Optional[str], str]]:
    """Load index database and return dict of hash -> (size, mtime, sha, datetime, path).
    
    Returns a dictionary mapping file hash to file information for easy comparison.
    """
    logger = logging.getLogger(__name__)
    
    if not db_path.exists():
        logger.error(f"Index file does not exist: {db_path}")
        raise FileNotFoundError(f"Index file not found: {db_path}")
    
    index_data = {}  # hash -> (size, mtime, sha, datetime, path)
    
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        
        # Try to read with datetime column
        try:
            cur.execute("SELECT path, size, mtime, sha, datetime FROM files")
            rows = cur.fetchall()
            for path, size, mtime, sha, dt in rows:
                if sha:  # Only index files with valid hashes
                    index_data[sha] = (size, mtime, sha, dt, path)
        except Exception:
            # Fallback for older DB without datetime column
            logger.warning(f"Database {db_path} does not have datetime column, reading without it")
            cur.execute("SELECT path, size, mtime, sha FROM files")
            for path, size, mtime, sha in cur.fetchall():
                if sha:  # Only index files with valid hashes
                    index_data[sha] = (size, mtime, sha, None, path)
        
        logger.info(f"Loaded {len(index_data)} files from {db_path}")
        return index_data
    
    except Exception as e:
        logger.error(f"Error loading index database {db_path}: {e}")
        raise
    finally:
        conn.close()


def compare_indexes(
    index1: Dict[str, Tuple[int, float, str, Optional[str], str]],
    index2: Dict[str, Tuple[int, float, str, Optional[str], str]],
) -> Tuple[List, List, List]:
    """Compare two indexes by hash.
    
    Returns:
        - only_in_1: List of files only in index1
        - only_in_2: List of files only in index2
        - in_both: List of files present in both (duplicates)
    """
    logger = logging.getLogger(__name__)
    
    hashes1 = set(index1.keys())
    hashes2 = set(index2.keys())
    
    # Files only in index1
    only_in_1_hashes = hashes1 - hashes2
    only_in_1 = [
        (index1[h][1], h[:16], index1[h][0], index1[h][3], index1[h][4])
        for h in only_in_1_hashes
    ]
    
    # Files only in index2
    only_in_2_hashes = hashes2 - hashes1
    only_in_2 = [
        (index2[h][1], h[:16], index2[h][0], index2[h][3], index2[h][4])
        for h in only_in_2_hashes
    ]
    
    # Files in both (duplicates by hash)
    in_both_hashes = hashes1 & hashes2
    in_both = [
        (
            h[:16],
            index1[h][0],
            index1[h][3],
            index1[h][4],  # path in db1
            index2[h][0],
            index2[h][3],
            index2[h][4],  # path in db2
        )
        for h in in_both_hashes
    ]
    
    logger.info(f"Comparison results:")
    logger.info(f"  Only in index1: {len(only_in_1)} files")
    logger.info(f"  Only in index2: {len(only_in_2)} files")
    logger.info(f"  In both (duplicates): {len(in_both)} files")
    
    return only_in_1, only_in_2, in_both


def write_report(
    report_path: Path,
    data: List,
    headers: List[str],
    title: str,
) -> None:
    """Write comparison report to CSV file."""
    logger = logging.getLogger(__name__)
    
    if not data:
        logger.info(f"No data to write for {title}")
        return
    
    report_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        with report_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in data:
                writer.writerow(row)
        
        logger.info(f"Wrote {len(data)} rows to {report_path}")
    except Exception as e:
        logger.error(f"Error writing report {report_path}: {e}")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Compare two index databases and report differences"
    )
    parser.add_argument(
        "--db1",
        required=True,
        help="Path to first index database (.sort_photos_index.db)",
    )
    parser.add_argument(
        "--db2",
        required=True,
        help="Path to second index database (.sort_photos_index.db)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output directory for reports (default: current directory)",
    )
    parser.add_argument(
        "--only-in-db1",
        default=None,
        help="CSV report path for files only in DB1 (default: only_in_db1.csv)",
    )
    parser.add_argument(
        "--only-in-db2",
        default=None,
        help="CSV report path for files only in DB2 (default: only_in_db2.csv)",
    )
    parser.add_argument(
        "--duplicates",
        default=None,
        help="CSV report path for files in both DBs (default: duplicates.csv)",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="Log file path (default: compare_indexes.log in output directory)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose (debug) logging",
    )
    
    args = parser.parse_args(argv)
    
    # Setup paths
    db1_path = Path(args.db1).expanduser().resolve()
    db2_path = Path(args.db2).expanduser().resolve()
    
    if args.output:
        output_dir = Path(args.output).expanduser().resolve()
    else:
        output_dir = Path.cwd()
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Setup logging
    if args.log:
        log_path = Path(args.log).expanduser().resolve()
    else:
        log_path = output_dir / "compare_indexes.log"
    
    setup_logging(log_path, args.verbose)
    logger = logging.getLogger(__name__)
    
    logger.info(f"Comparing indexes:")
    logger.info(f"  DB1: {db1_path}")
    logger.info(f"  DB2: {db2_path}")
    
    # Validate inputs
    if not db1_path.exists():
        logger.error(f"DB1 does not exist: {db1_path}")
        return 1
    if not db2_path.exists():
        logger.error(f"DB2 does not exist: {db2_path}")
        return 1
    
    # Load indexes
    try:
        logger.info("Loading first index database...")
        index1 = load_index_db(db1_path)
        
        logger.info("Loading second index database...")
        index2 = load_index_db(db2_path)
    except Exception as e:
        logger.error(f"Failed to load indexes: {e}")
        return 1
    
    # Compare indexes
    logger.info("Comparing indexes...")
    only_in_1, only_in_2, in_both = compare_indexes(index1, index2)
    
    # Determine report paths
    if args.only_in_db1:
        report_only_in_1 = Path(args.only_in_db1).expanduser().resolve()
    else:
        report_only_in_1 = output_dir / "only_in_db1.csv"
    
    if args.only_in_db2:
        report_only_in_2 = Path(args.only_in_db2).expanduser().resolve()
    else:
        report_only_in_2 = output_dir / "only_in_db2.csv"
    
    if args.duplicates:
        report_duplicates = Path(args.duplicates).expanduser().resolve()
    else:
        report_duplicates = output_dir / "duplicates.csv"
    
    # Write reports
    logger.info("Writing reports...")
    
    write_report(
        report_only_in_1,
        only_in_1,
        ["mtime", "hash", "size", "datetime", "path"],
        "Files only in DB1",
    )
    
    write_report(
        report_only_in_2,
        only_in_2,
        ["mtime", "hash", "size", "datetime", "path"],
        "Files only in DB2",
    )
    
    write_report(
        report_duplicates,
        in_both,
        ["hash", "size_db1", "datetime_db1", "path_db1", "size_db2", "datetime_db2", "path_db2"],
        "Files in both DBs (duplicates by hash)",
    )
    
    logger.info("Comparison complete!")
    print(f"\nComparison Results:")
    print(f"  Files only in DB1: {len(only_in_1)}")
    print(f"  Files only in DB2: {len(only_in_2)}")
    print(f"  Files in both (duplicates): {len(in_both)}")
    print(f"\nReports written to:")
    print(f"  {report_only_in_1}")
    print(f"  {report_only_in_2}")
    print(f"  {report_duplicates}")
    
    return 0


if __name__ == "__main__":
    exit(main())
