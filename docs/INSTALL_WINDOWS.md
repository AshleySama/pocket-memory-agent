# Windows Installation and Local Operation

## Source mode

1. Install Python 3.11 or later.
2. Clone the repository.
3. Run `setup_venv.bat` from the repository root.
4. Run `run_dev.bat`.

The first launch creates a local data directory. Development runs can use `--data-dir .\dev-data` to avoid mixing test content with personal content.

## Desktop runtime

PyWebView uses Microsoft Edge WebView2 when it is available. If WebView2 is unavailable, the application may open in the system browser instead. The local service remains bound to `127.0.0.1`, `localhost`, or `::1` only.

## Models are optional components

Basic note editing, local files, backups, and keyword search work without local model files. OCR, semantic retrieval, automatic organization, and local Q&A require their corresponding model assets. Follow [MODELS.md](MODELS.md) rather than copying a model from an untrusted source.

## Data location and backups

Use the in-app data settings to switch or migrate a data directory. Switching enters another data directory without copying the active knowledge base; migration copies data to a new target. Use the application's complete-backup action before upgrades, experiments, or manual file operations.

Do not delete only the `notes` directory to clear a library. Notes, attachments, and SQLite metadata/indexes must stay consistent.
