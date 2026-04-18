# PhotoSort

A robust Python application for sorting and organizing photos and videos by metadata with advanced duplicate detection capabilities.

## Features

### Core Functionality
- **Metadata-based renaming**: Rename files to `YYYY-MM-DD HH.MM.SS.ext` using EXIF `DateTimeOriginal` or video metadata
- **Organized storage**: Move files to `DEST/YYYY/MM/` (images) or `DEST/YYYY/Video/` (videos)
- **Content-based deduplication**: SHA-1 hashing prevents duplicate files from being stored
- **Safe duplicate handling**: Duplicates are quarantined instead of deleted for manual review

### Duplicate Detection
- **Exact duplicates**: SHA-1 hash-based detection of identical file content
- **Metadata duplicates**: Camera make/model + timestamp matching for identifying duplicates from same camera
- **EXIF timestamp extraction**: Precise datetime extraction from photo/video metadata

### Standalone Duplicate Checker
- **Duplicate detection**: Scan folders for duplicates against the index and move them to a Duplicate folder
- **Index management**: Uses the same persistent index for tracking processed files
- **Flexible scanning**: Recursive or flat directory scanning

### Reporting & Auditing
- **Comprehensive reports**: CSV reports for corrupted files, duplicates, and name collisions
- **Audit trail**: Optional events log tracking all file operations
- **Progress tracking**: Real-time progress bars with file processing statistics

### Performance & Reliability
- **Index caching**: SQLite index avoids re-processing destination files
- **Batch database writes**: Efficient bulk insertions for fast processing of large collections
- **Concurrent processing**: Multi-threaded file operations for improved performance on modern CPUs
- **Lazy evaluation**: Expensive computations performed only when necessary
- **Robust error handling**: Graceful handling of corrupted or unreadable files
- **Dry-run mode**: Preview all actions before making changes

## Installation

### Requirements
- Python 3.8+
- FFmpeg (for video metadata extraction)
- Dependencies listed in `requirements.txt`

### Setup
```bash
# Create virtual environment
python -m venv .venv

# Activate environment
.venv\Scripts\activate  # Windows
# or
source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt
```

## Quick Start

### Basic Usage
```powershell
# Dry-run to preview actions (recommended first!)
python sort_photos.py --source "C:\Photos\Unsorted" --dest "C:\Photos\Sorted" --dry-run --recursive

# Full processing run
python sort_photos.py --source "C:\Photos\Unsorted" --dest "C:\Photos\Sorted" --recursive
```

## Duplicate Checker

The `duplicate_checker.py` script provides standalone duplicate detection functionality. It scans a folder structure, checks files against the index, and moves duplicates to a Duplicate folder.

### Basic Usage
```powershell
# Check for duplicates in a folder
python duplicate_checker.py --source "C:\Photos\ToCheck" --recursive

# Specify custom duplicate folder and index
python duplicate_checker.py --source "C:\Photos\ToCheck" --duplicate "C:\Photos\Duplicates" --index-file "C:\Photos\.photo_index.db" --recursive
```

### Command Line Options
- `--source, -s`: Source directory to scan (required)
- `--duplicate, -d`: Directory for duplicate files (default: `SOURCE/Duplicate`)
- `--index-file`: Index file path (default: `SOURCE/.duplicate_index.db`)
- `--recursive`: Scan subdirectories recursively
- `--dry-run`: Preview actions without moving files
- `--report, -r`: CSV report for moved duplicates (default: `duplicate_report.csv`)
- `--log`: Log file path (default: `SOURCE/duplicate_checker.log`)
- `--verbose`: Enable verbose logging

## Command Line Options

### Required Arguments
- `--source, -s`: Source directory to scan for photos/videos

### Optional Arguments
- `--dest, -d`: Destination base directory (default: same as source)
- `--recursive`: Scan subdirectories recursively
- `--dry-run`: Preview actions without modifying files

### Indexing & Performance
- `--index-file`: Path to index file (default: `DEST/.sort_photos_index.db`)
- `--rebuild-index`: Rebuild index by scanning destination directory

### Reporting
- `--events-report`: CSV file for audit trail of all operations
- `--corrupt-report`: CSV file for corrupted/unreadable files
- `--duplicate-report`: CSV file for quarantined duplicates
- `--name-report`: CSV file for name collision reports

### Other Options
- `--quarantine`: Quarantine directory for potential duplicates (default: `DEST/Quarantine`)
- `--duplicate`: Duplicate directory for confirmed exact duplicates (default: `DEST/Duplicate`)
- `--log`: Custom log file path (default: `DEST/sort_photos.log`)
- `--verbose`: Enable detailed logging output

## File Organization

### Directory Structure
```
DEST/
├── 2023/
│   ├── 01/           # Images: YYYY/MM/
│   ├── 02/
│   └── Video/        # Videos: YYYY/Video/
├── 2024/
│   ├── 01/
│   ├── 02/
│   └── Video/
├── Duplicate/        # Confirmed exact duplicates
└── Quarantine/       # Potential duplicates for inspection
```

### File Naming
- Images: `2024-01-15 14.30.45.jpg`
- Videos: `2024-01-15 14.30.45.mp4`
- Duplicates get suffixes: `2024-01-15 14.30.45-1.jpg`

## Duplicate Detection

### Exact Duplicates
Files with identical SHA-1 hashes are moved to the `Duplicate/` folder.

### Metadata Duplicates
Files with the same timestamp AND same camera (Make/Model) are considered metadata duplicates and moved to `Quarantine/` for review.

## Supported Formats

### Image Formats
- JPG/JPEG
- PNG
- TIFF/TIF
- HEIC
- GIF
- BMP
- WebP

### Video Formats
- MP4
- MOV
- AVI
- MKV
- WMV
- MPG
- WebM

## Reports

### Events Report (`--events-report`)
Comprehensive audit trail with columns: `event,filename,src,dest,note`

Event types:
- `moved`: File successfully moved to destination
- `moved_collision`: File moved with suffix due to name conflict
- `quarantine_content`: File quarantined due to content duplicate
- `quarantine_metadata`: File quarantined due to metadata duplicate
- `quarantine_exists`: File quarantined due to existing destination file
- `corrupt`: File could not be processed

### Corrupt Report (`--corrupt-report`)
Lists unreadable/corrupted files with columns: `filename,path,error`

### Duplicate Report (`--duplicate-report`)
Lists quarantined duplicates with columns: `filename,path,kept_at,quarantined_to`

### Name Collision Report (`--name-report`)
Lists files that received suffixes due to naming conflicts

## Performance Tips

1. **First-time setup**: Run with `--rebuild-index` to efficiently scan the destination directory:
   ```powershell
   python sort_photos.py --source "C:\Photos\Unsorted" --dest "C:\Photos\Sorted" --rebuild-index
   ```

2. **Batch processing**: The tool uses:
   - Batch database writes for 10-100x faster indexing of large collections
   - Multi-threaded metadata extraction (auto-scales to CPU count)
   - Streaming progress bars for real-time feedback

3. **Large collections (>10,000 files)**:
   - Ensure destination has plenty of disk space
   - Close other disk-intensive applications
   - Monitor memory usage if processing >50,000 files at once

4. **Always test with `--dry-run`** before large-scale processing

## Examples

### Complete Workflow
```powershell
# 1. Preview actions
python sort_photos.py -s "C:\Photos\Unsorted" -d "C:\Photos\Sorted" --dry-run --recursive --verbose

# 2. Build index and process
python sort_photos.py -s "C:\Photos\Unsorted" -d "C:\Photos\Sorted" --rebuild-index --recursive

# 3. Generate reports
python sort_photos.py -s "C:\Photos\Unsorted" -d "C:\Photos\Sorted" --events-report "C:\Photos\Sorted\events.csv" --corrupt-report "C:\Photos\Sorted\corrupt.csv" --duplicate-report "C:\Photos\Sorted\duplicates.csv"
```

### Batch Processing
```powershell
# Process multiple source directories
foreach ($source in @("C:\Camera1", "C:\Camera2", "D:\Backup")) {
    python sort_photos.py -s $source -d "C:\Photos\Sorted" --recursive
}
```

## Architecture

The application uses an object-oriented design with these main components:

- **`FileMetadata`**: Dataclass holding file information and metadata
- **`IndexManager`**: Manages persistent index for tracking processed files
- **`DuplicateHandler`**: Handles duplicate detection logic
- **`PhotoProcessor`**: Main orchestrator for file processing workflow

## Troubleshooting

### Common Issues

**"Module not found" errors**
- Ensure virtual environment is activated
- Run `pip install -r requirements.txt`

**No video timestamps**
- Install FFmpeg and ensure `ffprobe` is in PATH
- Videos will fall back to file modification time

**Slow processing**
- Rebuild index: `--rebuild-index`

**Permission errors**
- Ensure write access to destination directory
- Close files in destination before running

### Logs
Check `DEST/sort_photos.log` for detailed execution information. Use `--verbose` for additional debug output.

## License

This software is provided as-is. Always backup your photos before processing and test with `--dry-run` first.

## Sync Index

The `sync_index.py` script syncs the index database with the actual filesystem. Use this after making changes outside the tool (e.g., deleting files via a viewer).

### Basic Usage
```powershell
# Sync index with filesystem
python sync_index.py --dest "C:\Photos\Sorted"

# Preview changes first
python sync_index.py --dest "C:\Photos\Sorted" --dry-run

# Custom report location
python sync_index.py --dest "C:\Photos\Sorted" --report "C:\custom_report.csv"
```

### What It Does
- **Removes** entries for files that no longer exist on disk
- **Adds** entries for new files that exist on disk but not in the database
- **Computes SHA-1 hashes** for new files so the database stays complete

## Ignore Patterns

Create a `.sort_photos_ignore` file in your source or destination folder to exclude specific folders or files:

```text
# Ignore folder by name (anywhere in the tree)
Topics
Favorites
MyPicks

# Ignore files by pattern
*.tmp
*.bak
```

### Features
- **Folder names**: Ignores any folder with that name anywhere in the tree
- **File patterns**: Uses glob-style matching (`*.tmp`, `*.bak`)
- **Comments**: Lines starting with `#` are ignored
- **Scope**: Works in both source and destination folders

## Contributing

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a pull request
