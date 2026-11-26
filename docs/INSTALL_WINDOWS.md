# Windows Installation and Local Operation

## Source mode

1. Install Python 3.11 or later.
2. Clone [AshleySama/pocket-memory-agent](https://github.com/AshleySama/pocket-memory-agent).
3. Run `setup_venv.bat` from the repository root. It installs the full `requirements.txt` dependency set, including retained-RAG requirements, and verifies imports. Model assets are a separate step; see [MODELS.md](MODELS.md).
4. Run `run_dev.bat`.

The first launch creates a local data directory. Development runs can use `--data-dir .\dev-data` to avoid mixing test content with personal content.

## Separate Windows Beta package

For **1.2.0-beta.1**, extract the complete package to a new folder. Keep the previous stable installation and original library in place.

- Start with `Start-Beta.bat`. It sets `LOCALAPPDATA` to the package-local `beta-profile` and runs the app with `--data-dir` pointing to package-local `data-beta`.
- Use `Stop-Beta.bat` to stop the Beta background process. Closing the desktop window does not necessarily stop the service.
- Directly double-clicking `PocketMemory.exe` bypasses this profile isolation and can reuse the current Windows user's previous configuration.
- To try an existing library, make a complete backup, stop the Beta, and restore that backup into a separate test copy. For the default launcher, the restored database, notes and attachments should sit directly inside `data-beta`. Keep the original library unchanged.
- In-app switching can select another backup copy for a session. The next `Start-Beta.bat` launch explicitly selects `data-beta` again.

This historical Beta uses the local 4B model. A correct answer may still have imprecise evidence scope; verify important facts in the original source. See [RAG limitations](RAG_LIMITATIONS_AND_EVALUATION.md).

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
