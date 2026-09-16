# Privacy and Data Boundaries

Pocket Memory is designed as a local-first desktop application:

- Notes, imported files, attachments, metadata, indexes, and configured model files are managed in local directories selected by the user.
- The default intelligent Q&A path loads a local model; it does not require a cloud LLM API key.
- The local HTTP service is restricted to loopback addresses.

## Important limits

- Local storage is not equivalent to a formal security certification, encryption guarantee, backup strategy, or organizational compliance program.
- The optional model downloader contacts its configured model host only after a user starts the download. Review its model source and license before use.
- The user is responsible for deciding what data may be imported, how the computer is protected, and where backups are stored.
- Do not expose the local service through port forwarding or an internet-facing proxy.

When reporting bugs publicly, use synthetic examples and remove notes, attachments, personal information, credentials, and organization-specific data.
