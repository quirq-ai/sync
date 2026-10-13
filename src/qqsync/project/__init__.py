"""qqsync.project: keep working clones up to date and manage who has access to their repo.

The other kind of sync. The manifest's pins are read-only copies at fixed commits; a project is a
clone someone edits, which follows a branch. Nothing here reads infra/repo.toml.
"""
