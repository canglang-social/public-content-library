# Contributing

Start with an issue or a focused pull request describing the observable problem, proposed behavior and source evidence. Never attach real credentials, private source lists, browsing cookies or personal library databases.

Install the project in a virtual environment and run `python -m unittest discover -s tests -v`. Use authored synthetic fixtures and temporary directories. Source outages belong in coverage/error reporting, not silently successful tests. Preserve old editions, authorship, provenance and user annotations when adding adapters.

Run `python -m bridge.validate` in an isolated native environment for changes to ArchiveBox publication or rendering. This checks publication idempotence, old-rendition integrity, annotations, search and restoration. See `docs/archivebox.md` for setup. Core tests alone do not establish native integration compatibility.

Describe what was tested and what remains unverified. Include storage migration and rollback implications for changes to paths, identifiers or persisted formats.
