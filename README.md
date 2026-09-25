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

## One agent, one session

An agent is two entries in `AGENTS_ROOT`, named by its Claude Code session
ID, which never changes:

```
~/agents/3f2a91c0-….json       its name, and what the supervisor notes about it
~/agents/3f2a91c0-….worktree   where it works: a directory, a git worktree,
                               or a symlink to one anywhere
```

* **The session ID is the key.** The conversation is that session: resumed by
  ID when it exists, otherwise started with that ID (`--session-id`), so the
  supervisor never has to guess which conversation belongs to which agent,
  and a directory never needs renaming.
* **The name lives in the JSON and follows the session.** Rename an agent
  anywhere (in the app, with `/rename`, or with `claude-agents rename`) and
  the supervisor copies the session's new name into the JSON within a poll.
  Every command takes the current name, the session ID, or an unambiguous
  prefix of it.
* **It appears in the app by itself.** Each session registers with claude.ai
  as it starts, under its name, and after a restart it rejoins the same
  claude.ai session.
* The JSON also records whether the agent is stopped, a fork waiting to
  start, and where an adopted agent came from.

**Only `AGENTS_ROOT` counts.** The supervisor never looks at git: a
repository can have any number of worktrees anywhere, and none is an agent
unless it has a pair in `AGENTS_ROOT`. Keep that directory for agents only.
Hidden entries (`.retired/`, …) are ignored.

**Converting the old layout.** Earlier versions used a directory named after
the agent (`~/agents/claude-dev`). The supervisor converts one of those by
itself: it stops the agent cleanly, moves its directory to
`<session-id>.worktree` (a git worktree by git), files its conversation
under the new path, writes the JSON, and resumes it.

## Commands

```
claude-agents list                          agents, state, session IDs, claude.ai links
claude-agents attach AGENT                  its terminal (tmux; detach with C-b d)

claude-agents new NAME [--dir PATH | --repo PATH]
                                            a new agent: in a new directory, a symlink
                                            to PATH, or a new git worktree of PATH
claude-agents fork AGENT NAME [--dir PATH]  a new agent from a copy of AGENT's conversation
claude-agents rename AGENT NAME
claude-agents stop AGENT                    pause it; it stays stopped
claude-agents start AGENT                   let it run again (resuming its conversation)
claude-agents restart AGENT                 clean stop; the supervisor resumes it at once
claude-agents retire AGENT                  stop it and move it to AGENTS_ROOT/.retired
claude-agents revive AGENT                  move it back; it resumes where it left off
claude-agents adopt (--session ID | --dir PATH) [--name NAME] [--move] [--dry-run]
                                            take over a session running elsewhere
```

In tmux, each agent is a session named by its session ID, with its one
window named after the agent and renamed when the agent is. Once attached,
`C-b s` (sessions, shown by their agent's name) or `C-b w` (windows) lists
every agent by name and switches between them.

`AGENT` is a name, a session ID or an ID prefix. **Don't move an agent's
worktree by hand.** Its conversation is filed under the worktree's path, so a
moved agent would start afresh. `retire`/`revive` move it with its
conversation. Nothing is ever deleted: a retired agent keeps its files and
its conversation.

**Renaming.** `rename` types `/rename NAME` into a running agent's session,
so the session renames itself and the supervisor follows. For a stopped
agent it changes the JSON, and the session takes the name when it next
starts.

**Forking.** `fork AGENT NAME` makes the child's worktree: a new git worktree
of the parent's repository on a branch named NAME if the parent is in one,
else a plain directory, or `--dir`. It files a copy of the parent's
conversation under the child's worktree (`--resume` searches by directory),
and the child starts with `--resume <parent> --fork-session --session-id
<its own>`. The child has the parent's history but a session of its own: its
own claude.ai session, and its own identity on any channel that derives one
from the session. The parent carries on untouched. An agent can fork itself
by running the command.

**Stuck at a prompt.** Remote Control doesn't pass every dialog to the app:
auto mode's confirmation after repeated blocks ("4 consecutive actions were
blocked … Do you want to proceed?") appears only in the terminal, so the app
shows an agent that simply never answers. With `AGENTS_ALERT_CMD` set, the
supervisor looks at each agent's screen on every poll. When one has sat at a
dialog for `AGENTS_ALERT_AFTER` seconds, it runs the command once with a
message naming the agent, the host and what the dialog asks. It runs it once
more when the dialog goes. The command decides where alerts go: chat, mail, a
push service (`config.example.env` sends them to a chat room). Answer the
dialog with `claude-agents attach AGENT`.

**Background forks.** Interrupting an agent at its terminal (Ctrl-C twice)
while it has a background task running makes Claude Code offer "move to
background and exit". That forks the session under Claude Code's daemon, and
the fork carries on with the agent's work, under its name, beside the agent
the supervisor restarts. The supervisor never stops agents that way: it uses
SIGTERM, which leaves no fork. It reports any fork of an agent it finds, once,
in its log and through `AGENTS_ALERT_CMD`, with the command to stop it. To
detach from an agent's terminal, use `C-b d`, not Ctrl-C.

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
claude-agents adopt --session 3f2a…            # or --dir <its directory>
claude-agents adopt --session 3f2a… --move     # relocate it into AGENTS_ROOT
claude-agents adopt --session 3f2a… --dry-run
```

It finds the session's directory, and the process running it, from Claude
Code's session files (`~/.claude/sessions/*.json` lists every running
session's ID, directory and name). Then it:

1. checks everything that could go wrong, before touching anything;
2. stops that process (`SIGTERM`, then `SIGKILL` after 30 s), so the
   conversation never runs in two processes;
3. with `--move`, files a copy of the conversation under the new path;
4. makes the pair: `<session-id>.worktree` as a symlink to the directory, or
   with `--move` the directory itself, moved into `AGENTS_ROOT`; then the
   JSON, named after the session's own name unless `--name` says otherwise. A git worktree is moved by
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

**An agent can adopt itself** (`claude-agents adopt --dir "$PWD" --move`): `adopt` sees it is running inside the session it stops, detaches,
lets the current reply finish (`--grace`, 10 s), and carries on in the
background, logging to `~/.local/state/claude-agents/adopt-<session-id>.log`.

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
