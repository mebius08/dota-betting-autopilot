# Workspace hygiene

- Run project commands from the root of the current Git repository on any operating system.
- Never create clones, worktrees, caches, or temporary directories as siblings of the repository.
- Put disposable artifacts in `local-data/tool-cache` or an operating-system temporary directory.
- Remove any task-created temporary worktrees or directories before finishing.
- Never place disposable files outside the repository to work around test or cache behavior.
