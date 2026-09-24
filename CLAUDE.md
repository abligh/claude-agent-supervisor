# claude-agent-supervisor

`claude-agents`: one Python file (standard library only) that keeps a Claude
Code agent running per directory under `AGENTS_ROOT`, each an interactive
`claude --remote-control NAME [--channels …]` session in tmux, resumed into
the same conversation on every start. See README.md.

```
claude-agents                  the program (run, list, attach, new, fork, adopt, stop,
                               start, restart, retire, revive)
claude-agents.service          systemd unit (runs `claude-agents run`)
config.example.env             host-wide settings; AGENTS_* are ours, the rest
                               goes into each agent's environment
tests/test_claude_agents.py    unit tests: python3 -m pytest tests
```

Facts it depends on (Claude Code 2.1.281; re-check on upgrades):

- Conversations are filed under `~/.claude/projects/<slug>/<session>.jsonl`,
  slug = the directory's real path with every non-alphanumeric character
  replaced by `-`; `--resume <id>` only finds them under the current
  directory's slug. A copied conversation resumes in a new directory with
  the same session ID.
- A running session writes `~/.claude/sessions/<pid>.json` with its
  `sessionId`; the conversation file appears at startup.
- `--resume` keeps the session ID and rejoins the same claude.ai Remote
  Control session; `--fork-session` gives a new ID.
- Workspace trust is inherited by subdirectories of a trusted directory.
- `claude remote-control` (the server) rejects `--channels`; the interactive
  `--remote-control` accepts it. Unapproved channels need a confirmation on
  every start, so unattended agents need `allowedChannelPlugins`.
- Ctrl-C twice is Claude Code's clean exit.
