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

This Beta freezes the existing 4B model choice. Do not replace weights to compare source changes. Packaged local-model Q&A, OCR, document parsing, child-tab writes and backup/restore were exercised. The strict historical quality smoke remains failed: a correct table answer includes an adjacent row in displayed evidence; a correct missing-field refusal cites the inspected table rather than returning zero sources. No RAG code was changed to make these checks pass. Target-machine and business acceptance remain pending.

## Desktop runtime

PyWebView uses Microsoft Edge WebView2 when it is available. If WebView2 is unavailable, the application may open in the system browser instead. The local service remains bound to `127.0.0.1`, `localhost`, or `::1` only.

## Models are optional components

Basic note editing, local files, backups, and keyword search work without local model files. OCR, semantic retrieval, automatic organization, and local Q&A require their corresponding model assets. Follow [MODELS.md](MODELS.md) rather than copying a model from an untrusted source.

## Reading, editing and save recovery

A note initially opens for reading. Choose Edit to change content and Done when finished. The formatting toolbar appears while editing. Check the displayed saving/saved/error state before leaving; after a save failure, retry from the same page. Failed drafts are retained in page memory only, so avoid refreshing or closing the page before a successful save. Deletion requires confirmation.

## Data location and backups

Use the in-app data settings to switch or migrate a data directory. Switching enters another data directory without copying the active knowledge base; migration copies data to a new target. Use the application's complete-backup action before upgrades, experiments, or manual file operations.

Do not delete only the `notes` directory to clear a library. Notes, attachments, and SQLite metadata/indexes must stay consistent.
