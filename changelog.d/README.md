# Changelog fragments

One file per change. Name it `<issue>.<type>.md` (for example `13.added.md`) or `+<slug>.<type>.md` when there is no issue. Types: `added`, `changed`, `fixed`, `removed`, `security`, `docs`. One or two sentences in the body, written for a user reading release notes.

`make changelog-draft` previews the assembled section. Releases run `towncrier build --version X.Y.Z`, which moves the fragments into `CHANGELOG.md`.
