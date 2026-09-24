# claude-agent-supervisor

Long-lived [Claude Code](https://code.claude.com) agents, one per directory,
kept running by systemd. Each agent is:

* an interactive `claude --remote-control NAME` session, so **claude.ai and
  the Claude app** list it and can open it;
* optionally listening on **channels** (`--channels`: Telegram, Discord, XMPP,
  your own), so events are pushed into it as they happen;
* in its own window of the supervisor's **tmux** server, for local access
  over ssh (`claude-agents attach NAME`): debugging, and commands that only
  work at the terminal;
* **resumed into the same conversation** whenever it starts: after a crash,
  a reboot, or Remote Control giving up during a network outage.

## Why not `claude remote-control`?

The Remote Control server (`claude remote-control --spawn …`) runs sessions
as headless children with a fixed command line. It rejects `--channels` and
cannot pass it to its sessions (`Error: Unknown argument: --channels`, Claude
Code 2.1.281), so **its sessions never receive channel events**. The
interactive form, `claude --remote-control NAME`, takes `--channels` like any
interactive session. It needs a terminal, which tmux provides, and something
to keep it running and resume it, which is this.

If you don't need channels, the Remote Control server may be all you need.
This adds channels, plus local terminal access and forks, while keeping its
property that nothing has to be recorded per agent.

## Nothing to record per agent

* **An agent is a directory** at the top of `AGENTS_ROOT`, or a symlink there
  to a directory anywhere (a git worktree, say). Its name is the entry's name.
  Create one and the supervisor starts it within seconds.
* **Its conversation is found, not configured**: the one the supervisor last
  ran there, or else the newest Claude Code keeps for that directory. Claude
  Code files conversations by directory; that is what `--resume` searches. An
  empty directory starts a new conversation.
* **It appears in the app by itself.** Each session registers with claude.ai
  as it starts, and after a restart it rejoins the same claude.ai session.
* The supervisor's only state is its own, in `~/.local/state/claude-agents/`:
  each agent's last conversation, a fork waiting to start, and whether it is
  stopped.

**Only `AGENTS_ROOT` counts.** The supervisor never looks at git: a
repository can have any number of worktrees anywhere, and none of them is an
agent unless it has an entry in `AGENTS_ROOT`. So keep that directory for
agents only (never `~`). Anything a tool creates there comes to life as an
agent. Hidden entries (`.name`) are ignored.

## Commands

```
claude-agents list                        agents, state, session IDs, claude.ai links
claude-agents attach NAME                 its terminal (tmux; detach with C-b d)

claude-agents new NAME [--dir PATH]       add an agent (a directory, or a symlink to PATH)
claude-agents fork PARENT CHILD [--dir PATH]
claude-agents stop NAME                   pause it; it stays stopped
claude-agents start NAME                  let it run again (resuming its conversation)
claude-agents restart NAME                clean stop; the supervisor resumes it at once
claude-agents retire NAME                 stop it and move it to AGENTS_ROOT/.retired
claude-agents revive NAME                 move it back; it resumes where it left off
claude-agents adopt NAME (--session ID | --dir PATH) [--move] [--dry-run]
                                          take over a session running elsewhere
```

`mkdir`, `git worktree add` and `ln -s` in `AGENTS_ROOT` work as well as
`new`. **Don't rename or move an agent's directory by hand.** Its
conversation is filed under the directory's path, so a moved agent starts
afresh. `retire`/`revive` move it and put it back at the same path. Nothing is
ever deleted: a retired agent keeps its files and its conversation.

**Forking.** `fork PARENT CHILD` makes CHILD's directory: a new git worktree
of the parent's repository on a branch named CHILD if the parent is in one,
else a plain directory, or `--dir`. It files a copy of the parent's
conversation under the child's directory (`--resume` searches by directory),
and the child starts with `--resume <parent> --fork-session`. The child has
the parent's history but a session of its own: its own claude.ai session, and
its own identity on any channel that derives one from the session. The
parent carries on untouched. An agent can fork itself by running the command.

**What can't be done**: creating an agent from the Claude app's "new
session". That belongs to the Remote Control server. Create agents by
command, or ask an agent to.

## Setup

1. Put `claude-agents` in `~/.local/bin/`, `config.example.env` in
   `~/.config/claude-agents/config.env` (then edit), and
   `claude-agents.service` in `/etc/systemd/system/` (adjust `User=` and the
   paths).
2. **Trust `AGENTS_ROOT` once**: run `claude` in it and accept the
   workspace-trust prompt. Directories created inside it are trusted from
   then on. A symlinked agent runs in its target, which needs the same (or a
   trusted parent).
3. **Channels must be approved** for unattended use. With
   `--dangerously-load-development-channels`, Claude Code asks for
   confirmation on every start, which nobody is there to give. Install the
   channel as a plugin, and approve it in `/etc/claude-code/managed-settings.json`:

   ```json
   { "channelsEnabled": true,
     "allowedChannelPlugins": [ { "plugin": "xmpp", "marketplace": "xmpp-mcp" } ] }
   ```

   This list replaces Claude Code's built-in list of approved channels, so
   include any official ones you use. Name the channels in `AGENTS_CHANNELS`.
   The supervisor enables those plugins for its agents only, not for every
   Claude session on the machine.
4. `systemctl enable --now claude-agents`.

## Adopting existing sessions

`adopt` takes over a session that runs elsewhere, the Remote Control
server's sessions included, with its conversation:

```
claude-agents adopt reviewer --session 3f2a…          # or --dir <its directory>
claude-agents adopt reviewer --session 3f2a… --move   # relocate it into AGENTS_ROOT
claude-agents adopt reviewer --session 3f2a… --dry-run
```

It finds the session's directory, and the process running it, from Claude
Code's session files (`~/.claude/sessions/*.json` lists every running
session's ID, directory and name). Then it:

1. checks everything that could go wrong, before touching anything;
2. stops that process (`SIGTERM`, then `SIGKILL` after 30 s), so the
   conversation never runs in two processes;
3. pins the session, and with `--move` files a copy of the conversation under
   the new path;
4. makes the entry: a symlink to the directory, or with `--move` the
   directory itself, moved into `AGENTS_ROOT`. A git worktree is moved by
   git, keeping its branch and any uncommitted work, and is unlocked first:
   the Remote Control server locks its worktrees. The supervisor then resumes
   it within seconds.

**`--move` or not?** For a session of the Remote Control server, move it.
Afterwards, that server's copy of the session has no directory to run in, so
a stray message to the old session in the app cannot quietly start a second
copy of the conversation beside the adopted one. Nothing is left depending on
what the server does with its worktrees. A symlink leaves the directory
where it is. That's right for directories you own, and the only option for a
repository's main checkout, or across filesystems (`git worktree move`
cannot cross them; `adopt` says so before stopping anything).

**An agent can adopt itself** (`claude-agents adopt NAME --dir "$PWD"
--move`): `adopt` sees it is running inside the session it stops, detaches,
lets the current reply finish (`--grace`, 10 s), and carries on in the
background, logging to `~/.local/state/claude-agents/adopt-NAME.log`.

If the last step fails, `adopt` undoes what it prepared. The session is
left stopped where it was, and resumes there as before.

The adopted agent comes back as a new claude.ai session, with the same
conversation and session ID. Use it from then on.

## Tests

`python3 -m pytest tests` (standard library only; needs pytest and git).
These cover what the supervisor decides, and `adopt`/`retire` against real
git worktrees. It has also been exercised end to end under systemd (Claude
Code 2.1.281): adding agents, channel delivery, forks, supervisor restart,
`kill -9` of an agent, retiring, adopting a session from outside by symlink,
and a session in a locked worktree adopting itself with `--move`. In each
case the agent came back with the same session ID and remembered what it
had been told.
