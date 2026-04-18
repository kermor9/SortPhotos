#!/usr/bin/env python3
"""Test script to check duplication of files in test/ folder."""

import argparse
import sys
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import Optional
import hashlib
try:
    import imagehash
except ImportError:
    imagehash = None
from PIL import Image, ExifTags

# Copy necessary code from sort_photos.py

EXIF_DT_TAGS = {"DateTimeOriginal", "DateTime", "DateTimeDigitized"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".heic", ".gif"}

@dataclass
class FileMetadata:
    """Represents metadata for a media file."""
    path: Path
    datetime: Optional[datetime]
    file_hash: str
    perceptual_hash: Optional[str]
    size: int
    mtime: float
    is_video: bool
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
        return ext in IMAGE_EXTENSIONS

class DuplicateHandler:
    """Handles duplicate detection and file naming logic."""

    def __init__(self, time_tolerance: float, use_perceptual_hash: bool, use_ssim: bool = False, ssim_threshold: float = 0.9):
        self.time_tolerance = time_tolerance
        self.use_perceptual_hash = use_perceptual_hash
        self.use_ssim = use_ssim
        self.ssim_threshold = ssim_threshold

    def should_quarantine_as_duplicate(self, file1: FileMetadata, file2: FileMetadata) -> bool:
        """Check if two files should be considered duplicates for quarantining."""
        if file1.file_hash == file2.file_hash:
            return True

        if self.use_perceptual_hash:
            if file1.perceptual_hash is None:
                file1.perceptual_hash = compute_perceptual_hash(file1.path)
            if file2.perceptual_hash is None:
                file2.perceptual_hash = compute_perceptual_hash(file2.path)
            if file1.perceptual_hash and file2.perceptual_hash and file1.perceptual_hash == file2.perceptual_hash:
                return True

        # Additional check: SSIM similarity for images
        if self.use_ssim and not file1.is_video and not file2.is_video:
            ssim_score = compute_ssim_similarity(file1.path, file2.path)
            if ssim_score >= self.ssim_threshold:
                return True

        # Additional check: same datetime (for metadata duplicates)
        if file1.datetime and file2.datetime and file1.datetime == file2.datetime:
            return True

        return False

def compute_quick_hash(path: Path, chunk_size: int = 8192) -> str:
    # full-file SHA1 hash used for content deduplication
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

def compute_perceptual_hash(path: Path) -> str:
    # Compute phash (perceptual hash) for visual similarity detection with higher precision
    try:
        import imagehash
        
        with Image.open(path) as img:
            # Convert to grayscale and resize for consistent hashing (increased to 16x16 for more precision)
            # img_gray = img.convert('L').resize((32, 32), Image.Resampling.LANCZOS)
            # Compute phash for better similarity detection
            phash = imagehash.phash(img, hash_size=8, highfreq_factor=4)
            return str(phash)
    except Exception:
        return ""


def compute_ssim_similarity(path1: Path, path2: Path) -> float:
    """Compute structural similarity index between two images."""
    try:
        from skimage.metrics import structural_similarity as ssim
    except ImportError:
        return 0.0
    if Image is None:
        return 0.0
    try:
        with Image.open(path1) as img1, Image.open(path2) as img2:
            # Convert to grayscale
            img1_gray = img1.convert('L')
            img2_gray = img2.convert('L')
            # Resize to the smaller size to compare
            size1 = img1_gray.size
            size2 = img2_gray.size
            min_width = min(size1[0], size2[0])
            min_height = min(size1[1], size2[1])
            img1_gray = img1_gray.resize((min_width, min_height))
            img2_gray = img2_gray.resize((min_width, min_height))
            # Convert to numpy arrays
            import numpy as np
            arr1 = np.array(img1_gray)
            arr2 = np.array(img2_gray)
            score, _ = ssim(arr1, arr2, full=True)
            print(f"SSIM score between {path1} and {path2}: {score}")
            return score
    except Exception:
        return 0.0


def get_exif_datetime(path: Path) -> Optional[datetime]:
    try:
        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img:
            exif = img._getexif() or {}
            if not exif:
                return None
            # map tag ids to names
            tag_map = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
            
            # print(tag_map)  # Debug: print all EXIF tags

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
                        return dt
                    except Exception:
                        continue
            
            # Fallback to basic datetime without sub-seconds
            for name in ("DateTimeOriginal", "DateTime", "DateTimeDigitized"):
                val = tag_map.get(name)
                if isinstance(val, str):
                    try:
                        # EXIF date format: 'YYYY:MM:DD HH:MM:SS'
                        dt = datetime.strptime(val, "%Y:%m:%d %H:%M:%S")
                        return dt
                    except Exception:
                        continue
            return None
    except Exception:
        # Let caller handle corrupted files
        raise

def file_mtime_datetime(path: Path) -> datetime:
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts)

def process_file(path: Path) -> FileMetadata:
    """Process a single file to get metadata."""
    try:
        stat = path.stat()
        size = stat.st_size
        mtime = stat.st_mtime
        
        # Get datetime
        dt = None
        is_video = False  # Assume images for now
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            try:
                dt = get_exif_datetime(path)
            except Exception:
                pass
        if dt is None:
            dt = file_mtime_datetime(path)
        
        # Compute hashes
        file_hash = compute_quick_hash(path)
        perceptual_hash = None
        
        return FileMetadata(
            path=path,
            datetime=dt,
            file_hash=file_hash,
            perceptual_hash=perceptual_hash,
            size=size,
            mtime=mtime,
            is_video=is_video,
            is_corrupted=False
        )
    except Exception as e:
        return FileMetadata(
            path=path,
            datetime=None,
            file_hash="",
            perceptual_hash=None,
            size=0,
            mtime=0,
            is_video=False,
            is_corrupted=True,
            error_message=str(e)
        )

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check if two files are duplicates")
    parser.add_argument("--file1", "-1", required=True, help="First file to check")
    parser.add_argument("--file2", "-2", required=True, help="Second file to check")
    parser.add_argument("--time-tolerance", type=float, default=0.0, help="Time tolerance in seconds")
    parser.add_argument("--use-perceptual-hash", action="store_true", help="Use perceptual hashing")
    parser.add_argument("--use-ssim", action="store_true", help="Use SSIM for image similarity")

    args = parser.parse_args()

    file1 = Path(args.file1).expanduser().resolve()
    file2 = Path(args.file2).expanduser().resolve()

    if not file1.exists() or not file1.is_file():
        print(f"File1 does not exist or is not a file: {file1}")
        sys.exit(1)
    if not file2.exists() or not file2.is_file():
        print(f"File2 does not exist or is not a file: {file2}")
        sys.exit(1)

    print(f"Checking files: {file1} and {file2}")
    
    meta1 = process_file(file1)
    meta2 = process_file(file2)
    
    print(f"\nFile 1: {meta1.path}")
    print(f"  Datetime: {meta1.datetime}")
    print(f"  File hash: {meta1.file_hash}")
    print(f"  Perceptual hash: {meta1.perceptual_hash}")
    print(f"  Size: {meta1.size}")
    
    print(f"\nFile 2: {meta2.path}")
    print(f"  Datetime: {meta2.datetime}")
    print(f"  File hash: {meta2.file_hash}")
    print(f"  Perceptual hash: {meta2.perceptual_hash}")
    print(f"  Size: {meta2.size}")

    print(f"\n Hamming distance between perceptual hashes: ", end="")
    if meta1.perceptual_hash and meta2.perceptual_hash:
        print(f"{imagehash.hex_to_hash(meta1.perceptual_hash) - imagehash.hex_to_hash(meta2.perceptual_hash)}")
    else:
        print("N/A")

    # Check duplication
    handler = DuplicateHandler(time_tolerance=args.time_tolerance, use_perceptual_hash=args.use_perceptual_hash, use_ssim=args.use_ssim)
    is_duplicate = handler.should_quarantine_as_duplicate(meta1, meta2)
    
    print(f"\nAre they duplicates? {is_duplicate}")
    
    if not is_duplicate:
        reasons = []
        if meta1.file_hash != meta2.file_hash:
            reasons.append("Different file hashes")
        if args.use_perceptual_hash and meta1.perceptual_hash != meta2.perceptual_hash:
            reasons.append("Different perceptual hashes")
        if meta1.datetime and meta2.datetime and abs((meta1.datetime - meta2.datetime).total_seconds()) > args.time_tolerance:
            reasons.append("Datetimes differ by more than tolerance")
        if reasons:
            print(f"Reasons not duplicate: {', '.join(reasons)}")
