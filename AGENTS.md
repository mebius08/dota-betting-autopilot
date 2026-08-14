# Workspace hygiene

- Run project commands with `E:\VS_code\project_folder\dota-betting-autopilot` as the working directory.
- Never create clones, worktrees, caches, or temporary directories as siblings under `E:\VS_code\project_folder`.
- Put disposable artifacts in `local-data\tool-cache` or an operating-system temporary directory.
- Remove any task-created temporary worktrees or directories before finishing.
- Never place disposable files outside the repository to work around test or cache behavior.
