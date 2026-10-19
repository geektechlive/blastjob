@AGENTS.md

## Claude Code notes

- The line above imports the shared rules; they apply in full. Edit rules in
  `AGENTS.md`, not here.
- A gitignored `CLAUDE.local.md` next to this file holds per-machine notes and overrides.
  Load it too and let it win where the two conflict. Never copy its content into a
  tracked file.
- Start repo changes with `EnterWorktree`, which puts the worktree under
  `.claude/worktrees/`.
