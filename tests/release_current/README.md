# Current release protection checks

Run from the repository root (Python dependencies from requirements-dev.txt and requirements.txt):

```powershell
python -m unittest discover -s tests/release_current -p "test_*.py" -v
node tests/frontend_current/run.cjs
```

These portable checks use repository source, temporary data and synthetic fixtures. They do not download or load the 4B model, call cloud services, or touch personal libraries. Test results are printed to stdout/stderr; capture them outside the source tree if needed.

Coverage: report content/charts/citation provenance/export, typed Excel calculations and source cells, draft persistence/conflicts, import retry and idempotency, update validation/model reuse/rollback/worker handoff/launcher shutdown, field projection and OCR scope/negative boundaries, browser queue/storage coordination.

The update HTTP tests use a real loopback HTTP server with fake application services; update workers exercise disposable files and fake executables/process functions. The process recovery test really exits a child process and, on Windows, tests file-sharing locks. JavaScript tests use fake APIs/storage/cache and selected editor function slices, not a browser IndexedDB implementation.

These checks supplement packaged EXE and browser validation; they are not a model-quality evaluation or human acceptance. Earlier raw regression answers and private-library tests are not distributed.

The frontend command runs five draft and import checks. To exercise another case, pass its test file directly to Node, for example `node tests/frontend_current/test_draft_navigation.cjs`.
