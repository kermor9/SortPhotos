"""Photo utilities for sorting and duplicate detection.

Shared classes and functions for photo processing applications.
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path
import shutil
from typing import Optional, Union, Dict, List, Tuple
import sqlite3
import time
from dataclasses import dataclass, field

try:
    from PIL import Image, ExifTags
except ImportError:
    # pillow is optional; several routines gracefully degrade when it is
    # absent.  Log a warning so users know why image-related features may
    # not work.
    Image = None
    ExifTags = None
    logging.getLogger(__name__).warning(
        "Pillow library not available; install using 'pip install Pillow' "
        "to enable EXIF extraction and image hashing features."
    )



EXIF_DT_TAGS = {"DateTimeOriginal", "DateTime", "DateTimeDigitized"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".heic", ".gif", ".dng"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".mpg", ".mpeg", ".webm", ".3gp"}

IGNORE_FILE = ".sort_photos_ignore"


def load_ignore_patterns(folder: Path) -> set[str]:
    """Load ignore patterns from .sort_photos_ignore file.
    
    Args:
        folder: Folder to look for the ignore file
        
    Returns:
        Set of patterns (folder names or file globs) to ignore
    """
    ignore_path = folder / IGNORE_FILE
    patterns = set()
    
    if not ignore_path.exists():
        return patterns
    
    try:
        with open(ignore_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                # Skip comments and blank lines
                if line and not line.startswith("#"):
                    patterns.add(line)
    except Exception:
        pass
    
    return patterns


def should_ignore(path: Path, patterns: set[str]) -> bool:
    """Check if a path should be ignored based on patterns.
    
    Args:
        path: Path to check (file or directory)
        patterns: Set of ignore patterns
        
    Returns:
        True if path matches any ignore pattern
    """
    if not patterns:
        return False
    
    # Check each part of the path for folder name matches
    parts = path.parts
    for part in parts:
        if part in patterns:
            return True
    
    # Check file name against glob patterns
    if path.is_file():
        import fnmatch
        filename = path.name
        for pattern in patterns:
            # Only treat as file pattern if it contains wildcard
            if "*" in pattern or "?" in pattern:
                if fnmatch.fnmatch(filename, pattern):
                    return True
    
    return False


@dataclass
class FileMetadata:
    """Represents metadata for a media file."""
    # required fields (no defaults) come first so dataclass init ordering
    # rules are satisfied.  ``datetime`` is optional so it is given a
    # default of ``None`` and placed after the strictly required values.
    path: Path
    file_hash: str
    size: int
    mtime: float

    make: Optional[str] = None
    model: Optional[str] = None
    software: Optional[str] = None

    datetime: Optional[datetime] = field(default=None)
    # internal text storage for sqlite-compatible datetime representation
    _datetime: Optional[str] = field(default=None, init=False, repr=False)

    # path where the file was moved to (set after move); kept as Path when available
    moved_path: Optional[Path] = field(default=None)

    # fields with sensible defaults
    is_video: bool = False
    is_corrupted: bool = False
    error_message: str = ""

    @property
    def extension(self) -> str:
        """Get the file extension in lowercase."""
        return self.path.suffix.lower()

    @property
    def is_supported(self) -> bool:
        """Check if the file type is supported."""
        ext = self.extension
        return ext in IMAGE_EXTENSIONS or ext in VIDEO_EXTENSIONS

# Attach a safe property for `datetime` that converts to/from the internal
# ``_datetime`` text value in an SQLite-friendly format.  The property is
# assigned after the dataclass is created to avoid dataclass/property
# initialization ordering issues.

def _fm_get_datetime(self) -> Optional[datetime]:
    raw = getattr(self, "_datetime", None)
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None


def _fm_set_datetime(self, value: Optional[Union[datetime, str]]) -> None:
    if value is None:
        self._datetime = None
    elif isinstance(value, datetime):
        self._datetime = value.strftime("%Y-%m-%d %H:%M:%S")
    elif isinstance(value, str):
        self._datetime = value
    else:
        raise TypeError("datetime must be datetime, str, or None")


FileMetadata.datetime = property(_fm_get_datetime, _fm_set_datetime)

class IndexManager:
    """Manages the persistent index for tracking processed files."""

    def __init__(self, index_file: Path):
        self.index_file = index_file
        # path -> (size, mtime, sha, datetime_text)
        self.path_index: Dict[str, Tuple[int, float, str, Optional[str]]] = {}
        self.hash_index: Dict[str, str] = {}  # maps hash -> path for efficient duplicate detection

        self._batch_queue: List[FileMetadata] = []  # Queue for batch inserts
        self._batch_size = 100  # Commit every N files
        self._init_db()
        self._load_index()
        self.logger = logging.getLogger(__name__)

    def _init_db(self) -> None:
        """Initialize SQLite database if needed."""
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.index_file))
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    size INTEGER,
                    mtime REAL,
                    sha TEXT,
                    updated_at REAL,
                    datetime TEXT
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sha ON files(sha)")
            # If an older DB exists without the datetime column, try to add it.
            try:
                cur.execute("ALTER TABLE files ADD COLUMN datetime TEXT")
            except Exception:
                # column already exists or cannot be added; ignore
                pass
            conn.commit()
        finally:
            conn.close()

    def _load_index(self) -> None:
        """Load existing index data."""
        self._load_db_index()

    def _load_db_index(self) -> None:
        """Load index from SQLite database."""
        if not self.index_file.exists():
            self.logger.warning(f"Index file {self.index_file} does not exist.")
            return

        conn = sqlite3.connect(str(self.index_file))
        try:
            cur = conn.cursor()
            # Attempt to read datetime column if present
            try:
                cur.execute("SELECT path, size, mtime, sha, datetime FROM files")
                rows = cur.fetchall()
                for fn, size, mtime, sha, dt in rows:
                    self.path_index[fn] = (size, mtime, sha, dt)
                    if sha:
                        self.hash_index[sha] = fn
            except Exception:
                # Older DB without datetime column
                cur.execute("SELECT path, size, mtime, sha FROM files")
                for fn, size, mtime, sha in cur.fetchall():
                    self.path_index[fn] = (size, mtime, sha, None)
                    if sha:
                        self.hash_index[sha] = fn
        finally:
            conn.close()

    def add_file(self, metadata: FileMetadata) -> None:
        """Add a file to the index (queued for batch insertion)."""
        path = metadata.moved_path if metadata.moved_path else metadata.path
        size = metadata.size
        mtime = metadata.mtime
        file_hash = metadata.file_hash
        datetime_text = metadata._datetime

        # Check if already indexed
        existing = self.path_index.get(str(path))
        if existing:
            existing_size, existing_mtime, existing_hash, _ = existing
            if existing_size == size and existing_mtime == mtime and existing_hash == file_hash:
                self.logger.debug(f"File {path} already indexed, skipping.")
                return

        # Queue for batch insert
        self._batch_queue.append(metadata)

        # Update in-memory indices immediately
        self.path_index[str(path)] = (size, mtime, file_hash, datetime_text)
        if file_hash:
            self.hash_index[file_hash] = str(path)

        # Commit batch if threshold reached
        if len(self._batch_queue) >= self._batch_size:
            self.flush_batch()

    def flush_batch(self) -> None:
        """Flush all queued files to database in a single batch."""
        if not self._batch_queue:
            return
        
        try:
            conn = sqlite3.connect(str(self.index_file))
            cur = conn.cursor()
            
            for metadata in self._batch_queue:
                size = metadata.size
                mtime = metadata.mtime
                file_hash = metadata.file_hash
                datetime_text = metadata._datetime
                path = metadata.moved_path if metadata.moved_path else metadata.path
                
                cur.execute(
                    "INSERT OR REPLACE INTO files(path,size,mtime,sha,updated_at,datetime) VALUES (?,?,?,?,?,?)",
                    (str(path), size, mtime, file_hash, time.time(), datetime_text),
                )
            
            conn.commit()
            self.logger.debug(f"Flushed {len(self._batch_queue)} files to database")
            self._batch_queue.clear()
        except Exception as e:
            self.logger.error(f"Error flushing batch to database: {e}")
        finally:
            conn.close()

    def get_file_info(self, path: Path) -> Optional[Tuple[int, float, str, Optional[str]]]:
        """Get cached file information: (size, mtime, sha, datetime_text)."""
        return self.path_index.get(str(path))

    def is_duplicate_hash(self, file_hash: str) -> Optional[str]:
        """Check if hash is already in index. Returns the path if found, None otherwise."""
        existing_path = self.hash_index.get(file_hash)
        if existing_path:
            self.logger.debug(f"Hash {file_hash[:16]}... already exists in index: {existing_path}")
        else:
            self.logger.debug(f"Hash {file_hash[:16]}... not found in index")
        return existing_path


class DuplicateHandler:
    """Handles duplicate detection and file naming logic.
    
    Focuses on exact duplicates (same hash) and metadata duplicates 
    (same datetime and camera make/model).
    """

    def __init__(self):
        self.timestamp_groups: Dict[datetime, List[Tuple[int, FileMetadata]]] = {}  # {datetime: [(count, metadata), ...]}
        self.logger = logging.getLogger(__name__)

    def get_filename_suffix(self, metadata: FileMetadata) -> int:
        """Get the suffix number for a datetime group."""
        dt = metadata.datetime
        if dt in self.timestamp_groups:
            count, _ = self.timestamp_groups[dt][-1]  # get last entry
            self.timestamp_groups[dt].append((count + 1, metadata))
            self.logger.debug(f"File {metadata.path.name} assigned suffix {count + 1}")
            return count + 1

        # New group
        self.timestamp_groups[dt] = [(0, metadata)]
        return 0

    def should_quarantine_as_duplicate(self, file1: FileMetadata, file2: FileMetadata) -> Tuple[bool, str]:
        """Check if two files should be considered duplicates for quarantining.
        
        Returns:
            Tuple[bool, str]: (is_duplicate, reason)
        """
        # Different file sizes = different files
        if file1.size != file2.size:
            self.logger.info(f"Files have different sizes: {file1.size} vs {file2.size}")
            return False, "different file sizes"
        
        # Exact hash match = definite duplicate
        if file1.file_hash and file2.file_hash and file1.file_hash == file2.file_hash:
            self.logger.info(f"Files have identical hashes: {file1.file_hash[:16]}...")
            return True, "identical hash"
        
        # Same datetime + same camera = metadata duplicate
        if (file1.datetime and file2.datetime and 
            file1.datetime == file2.datetime and 
            file1.make == file2.make and 
            file1.model == file2.model and
            file1.make is not None and file1.model is not None):
            self.logger.info(f"Metadata duplicate: same datetime and camera ({file1.make} {file1.model})")
            return True, "identical metadata"
        
        return False, ""


def get_video_datetime(path: Path) -> Optional[datetime]:
    """Try to extract creation time from video using ffprobe. Return None if unavailable."""
    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not proc.stdout:
            return None
        data = json.loads(proc.stdout)
        # Search format tags then stream tags for creation_time
        candidates = []
        fmt_tags = data.get("format", {}).get("tags", {}) or {}
        if fmt_tags:
            candidates.append(fmt_tags)
        for s in data.get("streams", []) or []:
            stags = s.get("tags", {}) or {}
            if stags:
                candidates.append(stags)

        for tags in candidates:
            for key in ("creation_time", "com.apple.quicktime.creation_time"):
                if key in tags:
                    val = tags[key]
                    # Normalize trailing Z
                    if isinstance(val, str):
                        v = val.replace("Z", "+00:00") if val.endswith("Z") else val
                        try:
                            dt = datetime.fromisoformat(v)
                            return dt.replace(tzinfo=None)
                        except Exception:
                            # try common formats
                            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y:%m-%d %H:%M:%S"):
                                try:
                                    dt = datetime.strptime(val, fmt)
                                    return dt
                                except Exception:
                                    continue
        return None
    except Exception:
        return None


def get_exif_data(path: Path) -> Tuple[Optional[datetime], Optional[str], Optional[str], Optional[str]]:
    if Image is None:
        return None, None, None, None
    try:
        with Image.open(path) as img:
            img.verify()

        with Image.open(path) as img:
            exif = img.getexif() or {}

            if not exif:
                return None, None, None, None
            # map tag ids to names
            tag_map = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}

            # Extract camera info
            make = tag_map.get("Make")
            model = tag_map.get("Model")
            software = tag_map.get("Software")

            # Try to get datetime with sub-second precision
            for dt_name, subsec_name in [("DateTimeOriginal", "SubsecTimeOriginal"), 
                                       ("DateTime", "SubsecTime"), 
                                       ("DateTimeDigitized", "SubsecTimeDigitized")]:
                dt_val = tag_map.get(dt_name)
                subsec_val = tag_map.get(subsec_name)
                if isinstance(dt_val, str):
                    try:
                        # EXIF date format: 'YYYY:MM:DD HH:MM:SS'
                        dt = datetime.strptime(dt_val, "%Y:%m:%d %H:%M:%S")
                        if isinstance(subsec_val, str) and subsec_val.isdigit():
                            # Add microseconds (EXIF subsec is in hundredths or thousandths)
                            subsec_int = int(subsec_val)
                            # Assume subsec is in hundredths of a second (common for EXIF)
                            microseconds = subsec_int * 10000  # 0.01s = 10000 microseconds
                            dt = dt.replace(microsecond=min(microseconds, 999999))
                        return dt, make, model, software
                    except Exception:
                        continue
            
            # Fallback to basic datetime without sub-seconds
            for name in ("DateTimeOriginal", "DateTime", "DateTimeDigitized"):
                val = tag_map.get(name)
                if isinstance(val, str):
                    try:
                        # EXIF date format: 'YYYY:MM:DD HH:MM:SS'
                        dt = datetime.strptime(val, "%Y:%m:%d %H:%M:%S")
                        return dt, make, model, software
                    except Exception:
                        continue
            return None, None, None, None
    except Exception:
        # Let caller handle corrupted files
        raise


def file_mtime_datetime(path: Path) -> datetime:
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts)


def format_filename(dt: datetime, suffix: str) -> str:
    # Example: 2024-01-04 09.31.32.jpg
    timestr = dt.strftime("%Y-%m-%d %H.%M.%S")
    return f"{timestr}{suffix}"


def compute_quick_hash(path: Path, chunk_size: int = 8192) -> str:
    """Compute SHA1 hash of file content for deduplication.
    
    Args:
        path: File path to hash
        chunk_size: Buffer size for reading file in chunks (default 8KB for optimal performance)
        
    Returns:
        Hex string of SHA1 hash, or empty string on error
    """
    h = hashlib.sha1()
    try:
        with path.open("rb") as f:
            while True:
                data = f.read(chunk_size)
                if not data:
                    break
                h.update(data)
    except Exception:
        return ""
    return h.hexdigest()


def safe_move(src: Path, dest: Path, dry_run: bool) -> Path:
    logger = logging.getLogger(__name__)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # handle collisions by appending suffix
    candidate = dest
    i = 1
    while candidate.exists():
        stem = dest.stem
        suffix = f"-{i}"
        candidate = dest.with_name(f"{stem}{suffix}{dest.suffix}")
        i += 1
    if dry_run:
        logger.info(f"[DRY] Move: {src} -> {candidate}")
        return candidate
    logger.info(f"Move: {src} -> {candidate}")
    shutil.move(str(src), str(candidate))
    return candidate


def safe_rename_inplace(src: Path, new_name: str, dry_run: bool) -> Path:
    logger = logging.getLogger(__name__)
    target = src.with_name(new_name)
    i = 1
    candidate = target
    while candidate.exists() and candidate != src:
        stem = target.stem
        candidate = target.with_name(f"{stem}-{i}{target.suffix}")
        i += 1
    if dry_run:
        logger.info(f"[DRY] Rename in-place: {src} -> {candidate}")
        return candidate
    logger.info(f"Rename in-place: {src} -> {candidate}")
    src.rename(candidate)
    return candidate


def setup_logging(log_file: Union[Path, str], verbose: bool = False) -> None:
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    # Suppress PIL debug logs
    logging.getLogger('PIL').setLevel(logging.WARNING)

    # Ensure log directory exists
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    # File handler
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s l.%(lineno)d:%(funcName)-25s %(message)s", datefmt="%H:%M:%S")
    try:
        fh = logging.FileHandler(str(log_file), encoding="utf-8")
        fh.setLevel(logging.DEBUG if verbose else logging.INFO)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        # If file logging fails, continue without file logging
        print(f"Warning: Could not set up file logging to {log_file}. Continuing without file logging.")

    # # Console handler for verbose mode
    # if verbose:
    #     console_fmt = logging.Formatter("%(levelname)-7s %(message)s")
    #     ch = logging.StreamHandler()
    #     ch.setLevel(logging.DEBUG)
    #     ch.setFormatter(console_fmt)
    #     logger.addHandler(ch)

    # # remove any existing handlers to avoid duplicate logs (but keep the ones we just added)
    # existing_handlers = list(logger.handlers)
    # for h in existing_handlers[:-2]:  # Keep the last 2 handlers we added
    #     logger.removeHandler(h)