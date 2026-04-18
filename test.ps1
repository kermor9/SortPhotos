# PowerShell test script for PhotoSort
# Sets up test environment and runs sort_photos with timing

# Continue on non-terminating errors
$ErrorActionPreference = 'Continue'

# Activate virtual environment
Write-Host "Activating virtual environment..." -ForegroundColor Cyan
. "$PSScriptRoot\.venv\Scripts\Activate.ps1"

# Setup test directories
Write-Host "Setting up test directories..." -ForegroundColor Cyan

# Ensure test\Original directory exists
$testOriginalPath = "$PSScriptRoot\test\Original"
if (-not (Test-Path $testOriginalPath)) {
    New-Item -ItemType Directory -Path $testOriginalPath -Force | Out-Null
}

# Copy source files to test\Original
Write-Host "Copying test files..." -ForegroundColor Cyan
Copy-Item -Path "$PSScriptRoot\Original\*" -Destination $testOriginalPath -Recurse -Force

# Clear previous test results
$testDestPath = "$PSScriptRoot\test_dest"
if (Test-Path $testDestPath) {
    Write-Host "Clearing previous results..." -ForegroundColor Cyan
    Remove-Item -Path "$testDestPath\*" -Recurse -Force -ErrorAction SilentlyContinue
}

# Run sort_photos with timing
Write-Host "Running PhotoSort..." -ForegroundColor Green
$sourcePath = "$PSScriptRoot\test\Original"
$destPath = $testDestPath

Write-Host "Source: $sourcePath" -ForegroundColor Gray
Write-Host "Destination: $destPath" -ForegroundColor Gray
Write-Host ""

$stopwatch = Measure-Command {
    python sort_photos.py -s $sourcePath -d $destPath --verbose --recursive
}

Write-Host ""
Write-Host "Processing time: $($stopwatch.TotalSeconds) seconds" -ForegroundColor Green
Write-Host "Test complete!" -ForegroundColor Cyan

