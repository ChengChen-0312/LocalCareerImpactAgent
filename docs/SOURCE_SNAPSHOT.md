# Source snapshot

This independent repository starts from development revision
`d23f9c3` (`fix: preserve intake and draft diagnostics and bound recovery`).

Product source, existing tests, environment manifests and lockfiles, and frontend
source were copied from that revision. Repository presentation consists of a new
README, this note, a Chinese project-experience draft, generic model-path examples,
and additional ignore rules. Existing test source is unchanged.

The original development repository and its history remain local. Local account
files, models, databases, candidate uploads, knowledge corpora, internal work logs,
and machine-specific planning documents are excluded from this snapshot.

## Acceptance context

A recorded real-API scenario at development revision `41ac5c6` completed the
nine-stage workflow, saved one validated report, matched nine reviewer suggestions
to nine decisions, and resolved two citations within its frozen snapshot.

Later adaptive stability work culminated in `d23f9c3`. It improved diagnosis and
bounded retries, but complex-input runs still failed at draft generation and at
assembly/final validation. Results collected across changing revisions are not a
fixed-version success rate. The historical passing scenario does not establish
reliability of every input or the factual accuracy of career forecasts.

The original local run receipts are excluded because this is a source release.
No fresh real-model run is claimed for the publication step. Background execution,
SSE, and the final-report frontend remain unfinished.
