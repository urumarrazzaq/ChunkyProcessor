# ChunkyProcessor

This script automates the process of parsing a log file, extracting chunk information, and performing Git operations (add, commit, push) for each chunk. It ensures that previously processed chunks are not repeated, and logs all operations for tracking. The script also backs up the log file with a timestamp in a dedicated directory.

## Features
- Parses a provided `.log` file to extract chunk data (files, file count, size).
- Automatically stages, commits, and pushes files to a Git repository.
- Tracks processed chunks and skips already processed ones.
- Backs up the original log file with a timestamp for record-keeping.

## Requirements
- Python 3.x
- Git installed on the system

## Installation

1. Clone or download the repository.
2. Ensure Python 3.x is installed.
3. Install any necessary dependencies via `pip` (if any).
4. Ensure you have Git configured and accessible from the terminal.


