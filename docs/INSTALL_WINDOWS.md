# Windows Installation and Local Operation

## Source mode

1. Install Python 3.11 or later.
2. Clone [AshleySama/pocket-memory-agent](https://github.com/AshleySama/pocket-memory-agent).
3. Run `setup_venv.bat` from the repository root. It installs the full `requirements.txt` dependency set, including retained-RAG requirements, and verifies imports. Model assets are a separate step; see [MODELS.md](MODELS.md).
4. Run `run_dev.bat`.

The first launch creates a local data directory. Development runs can use `--data-dir .\dev-data` to avoid mixing test content with personal content.

## Complete Windows package

Extract the full offline ZIP to a new folder, then run `PocketMemory.exe` from that folder. Keep the executable, its runtime directory and its models together. To try an existing library safely, restore a complete backup into a separate data folder.

## Software updates

Complete offline packages include the model assets. Program-only update packages reuse the installed models and keep the existing library and configuration. Do not use a program-only ZIP as a fresh installation or manually copy individual runtime files over an existing installation.

Before an upgrade, save edits and create a complete backup from the data settings. Keep the original installation, notes, attachments, models and transaction backups until the upgraded version is confirmed to work.

Some enterprise SentinelOne installations quarantine the staged update component. This remains a known compatibility issue; local upgrade and rollback checks do not establish approval by an enterprise security policy. If a security alert appears, stop repeated attempts, keep the diagnostic and ask the security administrator to review it. Do not disable protection or repeatedly restore and execute quarantined files. If a file is missing, unreadable or fails its manifest/hash check, the application must stop the update rather than install incomplete files.

A normal change to valid release information before execution requires a fresh update check. It is distinct from a component-integrity failure. Updating this source repository does not automatically publish a new installer or change the download feed.

## Desktop runtime

PyWebView uses Microsoft Edge WebView2 when it is available. If WebView2 is unavailable, the application may open in the system browser instead. The local service remains bound to `127.0.0.1`, `localhost`, or `::1` only.

## Models are optional components

Basic note editing, local files, backups, and keyword search work without local model files. OCR, semantic retrieval, automatic organization, and local Q&A require their corresponding model assets. Follow [MODELS.md](MODELS.md) rather than copying a model from an untrusted source.

## Reading, editing and save recovery

A note initially opens for reading. Choose Edit to change content and Read when finished. The formatting toolbar appears while editing. Existing notes autosave after a short pause. Check the save status after editing. If a save fails, retry or open the draft box to recover a locally retained draft. Deletion requires confirmation.

## Data location and backups

Use the in-app data settings to switch or migrate a data directory. Switching enters another data directory without copying the active knowledge base; migration copies data to a new target. Use the application's complete-backup action before upgrades, experiments, or manual file operations.

Do not delete only the `notes` directory to clear a library. Notes, attachments, and SQLite metadata/indexes must stay consistent.
