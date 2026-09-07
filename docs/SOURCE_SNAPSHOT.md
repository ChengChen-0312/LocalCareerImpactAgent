# Source snapshot

Updated 2026-09-07 from development revision
`03d1c54dfdebfe8ab1eb64a1482d313dc227f3e1`
(`docs: record four-case condition preservation pilot`).

Product source, existing tests, manifests and lockfiles, frontend, launch scripts,
README, MVP documentation and the recorded condition pilot are copied from this
revision. Personal model paths remain replaced with generic configuration values.
The private development repository, internal work logs and planning history remain
local. This GitHub repository keeps its own main-branch history.

Compared with the initial d23f9c3 snapshot, the application includes background
execution, progress streaming/polling, report rendering, launcher scripts,
structured generation, scoped evidence retrieval and attachment-aware language
selection. Consult docs/mvp/acceptance-record.md, grounded-demo.md and the
condition-pilot RESULTS.md for the limits of observed results. No fresh model
inference was run during this repository sync.

At the owner's explicit request, this update also includes a complete consistent
SQLite backup in data/database/localcareerimpact.sqlite3.gz. It contains existing
chat, extracted material, report and knowledge records, not just an empty schema.
The repository remains private. Actual account CSV, model weights, temporary
uploads and original knowledge source files are not included. Database restore
instructions and checksums are in data/database/README.md.

Existing tests were copied without edits. Code in the development checkout was
clean at export. Its 03d1c54 revision is the exact source boundary for this update.
