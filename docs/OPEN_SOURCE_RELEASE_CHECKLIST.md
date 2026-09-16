# Open-Source Release Checklist

Run this checklist before creating a public GitHub repository or publishing a release tag.

## Repository hygiene

- [ ] Publish this clean source tree, not the private development repository or its full history.
- [ ] Confirm `git status --ignored` contains no `data/`, `models/`, database, package, output, or user-library files staged for commit.
- [ ] Search the staged tree for emails, account IDs, local paths, API keys, access tokens, customer names, screenshots, and company-specific material.
- [ ] Confirm all public test fixtures are synthetic, de-identified, or sourced from material that may be redistributed.
- [ ] Keep real-user acceptance sets, raw feedback, release smoke outputs, competition content, and development handoff records private.

## Ownership and licensing

- [ ] Confirm the maintainers own or are authorized to publish every source file, image, logo, screenshot, font, and icon included in the repository.
- [ ] Confirm Apache-2.0 is the intended source-code license for all contributors.
- [ ] Generate a license inventory for the exact release virtual environment, for example with `pip-licenses`, and archive it with the release record.
- [ ] Review the exact model files and OCR assets separately before redistributing them in a binary package.
- [ ] Keep model weights outside Git. Link to trusted upstream sources instead.

## Engineering baseline

- [ ] Run `python -m unittest discover -s tests -q`.
- [ ] Exercise source mode on a clean Windows profile.
- [ ] Exercise document import, source opening, data-directory switching, and complete backup.
- [ ] Run model-backed OCR, semantic retrieval, and local-Q&A smoke tests with the intended model files.
- [ ] Build and smoke-test the exact packaged EXE on a clean Windows machine.
- [ ] Record known limitations and unresolved defects in GitHub Issues or release notes.

## GitHub setup

- [ ] Create a new public repository from this directory and protect the default branch.
- [ ] Enable private vulnerability reporting.
- [ ] Enable the included GitHub Actions workflow and verify its first run.
- [ ] Add a concise project description, topics, and a Beta disclaimer.
- [ ] Create issue labels such as `bug`, `enhancement`, `rag`, `documentation`, and `good first issue`.

## Suggested first public release wording

> Pocket Memory is a Windows-first local knowledge workspace in Beta. It is designed for notes and document retrieval with source-backed local Q&A. Please verify important information in the cited source and avoid posting private material in public issues.
