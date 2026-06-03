# Git Chunk Processor Dashboard

A browser-based dashboard for managing git chunk processing tasks with a visual UI.

## Project Contents

- `Git Chunk Processor Dashboard.py` - Main dashboard application script.
- `favicon.ico` - Icon file used when packaging the executable.
- `chunk_processor_state.json` - Dashboard state file.
- `processed_chunks.json` - Processed chunks tracking file.
- `chunk_processor_dashboard_runtime.json` - Runtime state file.
- `logs/` - Folder for log files.
- `dist/` - Output folder for packaged executable.
- `build/` - PyInstaller build artifacts.

## Usage

### Run from source

1. Open a terminal in this folder.
2. Run:
   ```powershell
   c:/python314/python.exe "Git Chunk Processor Dashboard.py"
   ```
3. Open the browser UI at `http://127.0.0.1:8765`.

### Run the packaged executable

1. Open `dist`.
2. Launch `Git Chunk Processor Dashboard.exe`.

## Build the executable

This project uses PyInstaller to create a single-file Windows executable with the icon.

Run the following command from the project folder:

```powershell
c:/python314/python.exe -m PyInstaller --noconfirm --onefile --windowed --icon=favicon.ico "Git Chunk Processor Dashboard.py"
```

After building, the executable will be available in `dist/`.

## Notes

- The dashboard stores JSON state files in the project directory.
- If no `processed_chunks.json` exists, it will be created as needed.
- The executable has been built using `favicon.ico` as the application icon.
