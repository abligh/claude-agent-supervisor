"""Unit tests for claude-agents, the supervisor.

No tmux and no Claude Code: these cover what the supervisor *decides* — which
entries are agents, what it runs for each, how forks, renames, retirement,
adoption and the conversion of the old layout lay things out on disk.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "claude-agents"
_loader = importlib.machinery.SourceFileLoader("claude_agents", str(SCRIPT))
_spec = importlib.util.spec_from_loader("claude_agents", _loader)
ca = importlib.util.module_from_spec(_spec)
sys.modules["claude_agents"] = ca  # dataclasses look their module up
_loader.exec_module(ca)


@pytest.fixture(autouse=True)
def no_tmux(monkeypatch: pytest.MonkeyPatch) -> list:
    """Never touch a real tmux server: the machine running the tests may be
    running real agents under the same socket name."""
    calls: list = []

    def fake(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(ca, "tmux", fake)
    return calls


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CLAUDE_AGENTS_STATE", str(tmp_path / "state"))
    (tmp_path / "agents").mkdir()
    return tmp_path


def _cfg(home: Path, **kw):
    return ca.Config(root=home / "agents", poll=0, **kw)


def _conversation(real: Path, session: str, *, age: float = 0.0, titles=()) -> Path:
    d = ca.project_dir(real.resolve())
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{session}.jsonl"
    lines = [{"type": "user", "cwd": str(real)}]
    lines += [{"type": "custom-title", "customTitle": t, "sessionId": session} for t in titles]
    f.write_text("".join(json.dumps(x) + "\n" for x in lines))
    t = time.time() - age
    os.utime(f, (t, t))
    return f


def _agent(home: Path, name: str, *, sid: str | None = None, conversation: bool = True, **meta):
    a = ca.create(_cfg(home), name, sid=sid, **meta)
    if conversation:
        _conversation(a.real, a.sid)
    return a


def _args(**kw):
    return argparse.Namespace(**kw)


def _git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    return path


def _worktrees(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                          capture_output=True, text=True).stdout


# --- basics ----------------------------------------------------------------------


def test_slug_matches_claude_codes_project_folders() -> None:
    path = "/home/me/src/project/.claude/worktrees/bridge-cse_01Fd5gTSC"
    assert ca.slug(path) == "-home-me-src-project--claude-worktrees-bridge-cse-01Fd5gTSC"


def test_config_splits_own_settings_from_the_agents_environment(tmp_path: Path) -> None:
    conf = tmp_path / "config.env"
    conf.write_text(
        "# comment\n"
        "AGENTS_ROOT=~/crew\n"
        "AGENTS_CLAUDE_ARGS=--model sonnet\n"
        "AGENTS_CHANNELS=plugin:xmpp@xmpp-mcp, server:local\n"
        "XMPP_JID={session}.{host}@agents.example.com\n"
        "XMPP_CHANNEL_ALLOW='*@agents.example.com,*@example.com'\n"
    )
    cfg = ca.load_config(conf)
    assert cfg.root == Path("~/crew").expanduser()
    assert cfg.extra_args == ["--model", "sonnet"]
    assert cfg.channels == ["plugin:xmpp@xmpp-mcp", "server:local"]
    assert cfg.plugins == ["xmpp@xmpp-mcp"]
    assert cfg.session_env == {
        "XMPP_JID": "{session}.{host}@agents.example.com",
        "XMPP_CHANNEL_ALLOW": "*@agents.example.com,*@example.com",
    }


def test_an_agent_is_a_json_and_a_worktree_named_by_its_session(home: Path) -> None:
    root = home / "agents"
    a = _agent(home, "reviewer")
    assert (root / f"{a.sid}.json").exists() and (root / f"{a.sid}.worktree").is_dir()
    (home / "elsewhere").mkdir()
    b = ca.create(_cfg(home), "builder", dir=str(home / "elsewhere"))
    # Not agents: a JSON with no worktree, a stray file, a hidden entry.
    lone = str(uuid.uuid4())
    (root / f"{lone}.json").write_text('{"name": "lone"}')
    (root / "notes.txt").write_text("x")
    (root / ".retired").mkdir()
    agents = {x.name: x for x in ca.list_agents(root)}
    assert set(agents) == {"builder", "reviewer"}
    assert agents["builder"].real == (home / "elsewhere").resolve() and b.worktree.is_symlink()
    with pytest.raises(SystemExit, match="already an agent called"):
        ca.create(_cfg(home), "Reviewer")


def test_new_can_make_a_git_worktree(home: Path) -> None:
    repo = _git_repo(home / "repo")
    a = ca.create(_cfg(home), "sre", repo=str(repo))
    assert str(a.worktree.resolve()) in _worktrees(repo)
    assert subprocess.run(["git", "-C", str(a.worktree), "branch", "--show-current"],
                          capture_output=True, text=True).stdout.strip() == "sre"


# --- what runs ---------------------------------------------------------------------


def test_command_line(home: Path) -> None:
    cfg = _cfg(home, extra_args=["--model", "sonnet"],
               channels=["plugin:xmpp@xmpp-mcp", "plugin:other@market"])
    a = _agent(home, "reviewer", conversation=False)
    fresh = ca.claude_args(a, cfg)
    assert fresh[:5] == ["claude", "--remote-control", "reviewer", "--name", "reviewer"]
    i = fresh.index("--channels")
    assert fresh[i + 1:i + 3] == ["plugin:xmpp@xmpp-mcp", "plugin:other@market"]
    assert json.loads(fresh[fresh.index("--settings") + 1]) == {
        "enabledPlugins": {"xmpp@xmpp-mcp": True, "other@market": True}}
    # A new agent's conversation is created with its ID: the ID is known first.
    assert fresh[fresh.index("--session-id") + 1] == a.sid and "--resume" not in fresh
    assert fresh[-2:] == ["--model", "sonnet"]
    _conversation(a.real, a.sid)
    resumed = ca.claude_args(a, cfg)
    assert resumed[resumed.index("--resume") + 1] == a.sid and "--session-id" not in resumed
    bare = ca.claude_args(a, _cfg(home))
    assert "--channels" not in bare and "--settings" not in bare


def test_resolve_by_name_id_or_prefix(home: Path) -> None:
    a = _agent(home, "Reviewer", sid="aaaa1111-0000-4000-8000-000000000001")
    b = _agent(home, "builder", sid="aaaa2222-0000-4000-8000-000000000002")
    cfg = _cfg(home)
    assert ca.resolve(cfg, "reviewer").sid == a.sid  # names ignore case
    assert ca.resolve(cfg, b.sid).name == "builder"
    assert ca.resolve(cfg, "aaaa2").name == "builder"
    with pytest.raises(SystemExit, match="ambiguous"):
        ca.resolve(cfg, "aaaa")
    with pytest.raises(SystemExit, match="no agent"):
        ca.resolve(cfg, "nobody")


def test_the_json_follows_the_sessions_name(home: Path, monkeypatch) -> None:
    a = _agent(home, "claude-dev")
    live = {"sessionId": a.sid, "name": "claude-dev2"}
    monkeypatch.setattr(ca, "live_session", lambda pid: live)
    sup = ca.Supervisor(_cfg(home))
    sup._follow(a, ca.Pane(False, 1234, ""))
    assert ca.resolve(_cfg(home), "claude-dev2").sid == a.sid
    # The next start passes the new name, so it is not undone.
    args = ca.claude_args(ca.resolve(_cfg(home), a.sid), _cfg(home))
    assert args[args.index("--name") + 1] == "claude-dev2"
    # Another session's file (a recycled PID) changes nothing.
    live = {"sessionId": str(uuid.uuid4()), "name": "impostor"}
    sup._follow(ca.resolve(_cfg(home), a.sid), ca.Pane(False, 1234, ""))
    assert ca.resolve(_cfg(home), a.sid).name == "claude-dev2"


def test_rename_while_stopped_waits_for_the_next_start(home: Path) -> None:
    a = _agent(home, "old")
    _conversation(a.real, a.sid, titles=("old",))
    ca.cmd_rename(_cfg(home), _args(agent="old", name="new"))
    args = ca.claude_args(ca.resolve(_cfg(home), a.sid), _cfg(home))
    assert args[args.index("--name") + 1] == "new"




def test_rename_while_running_types_rename_into_the_session(home: Path, monkeypatch,
                                                             no_tmux) -> None:
    a = _agent(home, "old")
    monkeypatch.setattr(ca, "pane", lambda name: ca.Pane(False, 1234, ""))
    ca.cmd_rename(_cfg(home), _args(agent="old", name="new"))
    assert ("send-keys", "-t", f"={a.sid}:", "-l", "/rename new") in no_tmux
    assert ca.resolve(_cfg(home), a.sid).name == "old"  # until the session confirms it


def test_stop_and_start_are_kept_in_the_json(home: Path) -> None:
    a = _agent(home, "reviewer")
    ca.cmd_stop(_cfg(home), _args(agent="reviewer"))
    assert ca.resolve(_cfg(home), a.sid).meta.get("stopped") is True
    ca.cmd_start(_cfg(home), _args(agent="reviewer"))
    assert "stopped" not in ca.resolve(_cfg(home), a.sid).meta


# --- forks -------------------------------------------------------------------------


def test_fork_starts_from_a_copy_of_the_parents_conversation(home: Path) -> None:
    parent = _agent(home, "parent")
    side = ca.project_dir(parent.real) / parent.sid / "subagents"
    side.mkdir(parents=True)
    (side / "a.jsonl").write_text("{}\n")
    ca.cmd_fork(_cfg(home), _args(agent="parent", name="child", dir=None))
    child = ca.resolve(_cfg(home), "child")
    assert child.sid != parent.sid and child.meta["fork_from"] == parent.sid
    copied = ca.project_dir(child.real)
    assert (copied / f"{parent.sid}.jsonl").exists()
    assert (copied / parent.sid / "subagents" / "a.jsonl").exists()
    args = ca.claude_args(child, _cfg(home))
    assert args[args.index("--resume") + 1] == parent.sid and "--fork-session" in args
    assert args[args.index("--session-id") + 1] == child.sid  # its own, known ID
    assert args[args.index("--name") + 1] == "child"
    # Once its own conversation exists, it is resumed like any other.
    _conversation(child.real, child.sid)
    assert "--fork-session" not in ca.claude_args(child, _cfg(home))


def test_fork_of_a_git_worktree_is_a_new_worktree(home: Path) -> None:
    repo = _git_repo(home / "repo")
    parent = ca.create(_cfg(home), "parent", repo=str(repo))
    _conversation(parent.real, parent.sid)
    ca.cmd_fork(_cfg(home), _args(agent="parent", name="child", dir=None))
    child = ca.resolve(_cfg(home), "child")
    assert str(child.worktree.resolve()) in _worktrees(repo)


def test_fork_refuses_a_parent_with_no_conversation(home: Path) -> None:
    _agent(home, "parent", conversation=False)
    with pytest.raises(SystemExit, match="no conversation to fork"):
        ca.cmd_fork(_cfg(home), _args(agent="parent", name="child", dir=None))
    assert [a.name for a in ca.list_agents(home / "agents")] == ["parent"]


# --- retire / revive ---------------------------------------------------------------


def test_retire_and_revive_keep_files_and_conversation(home: Path) -> None:
    a = _agent(home, "reviewer")
    (a.worktree / "work.txt").write_text("uncommitted")
    (home / "elsewhere").mkdir()
    b = ca.create(_cfg(home), "builder", dir=str(home / "elsewhere"))
    cfg = _cfg(home)
    for name in ("reviewer", "builder"):
        ca.cmd_retire(cfg, _args(agent=name))
    assert ca.list_agents(home / "agents") == []
    assert (home / "agents" / ".retired" / f"{a.sid}.worktree" / "work.txt").exists()
    assert (home / "elsewhere").is_dir()  # a symlink's target is never touched
    for name in ("reviewer", b.sid[:6]):
        ca.cmd_revive(cfg, _args(agent=name))
    agents = {x.name: x for x in ca.list_agents(home / "agents")}
    assert set(agents) == {"builder", "reviewer"}
    back = agents["reviewer"]
    assert ca.has_transcript(back.real, back.sid)  # found again at the same path
    assert "stopped" not in back.meta


def test_retire_moves_a_git_worktree_with_git(home: Path) -> None:
    repo = _git_repo(home / "repo")
    a = ca.create(_cfg(home), "wt", repo=str(repo))
    ca.cmd_retire(_cfg(home), _args(agent="wt"))
    assert str((home / "agents" / ".retired" / f"{a.sid}.worktree").resolve()) in _worktrees(repo)
    ca.cmd_revive(_cfg(home), _args(agent="wt"))
    assert str(a.worktree.resolve()) in _worktrees(repo)


# --- converting the old layout -----------------------------------------------------


def test_the_old_layout_converts_itself(home: Path) -> None:
    root = home / "agents"
    repo = _git_repo(home / "repo")
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(root / "claude-dev")],
                   check=True)
    (root / "claude-dev" / "work.txt").write_text("keep me")
    sid = str(uuid.uuid4())
    # Renamed in its session since: the conversation's latest title is its name.
    _conversation(root / "claude-dev", sid, titles=("claude-dev", "claude-dev2"))
    state = home / "state" / "agents"
    state.mkdir(parents=True)
    (state / "claude-dev.json").write_text(json.dumps({"session": sid, "named": True}))
    (home / "elsewhere").mkdir()
    (root / "linked").symlink_to(home / "elsewhere")
    linked_sid = str(uuid.uuid4())
    _conversation(home / "elsewhere", linked_sid)

    ca.Supervisor(_cfg(home)).tick()

    agents = {a.name: a for a in ca.list_agents(root)}
    assert set(agents) == {"claude-dev2", "linked"}
    dev = agents["claude-dev2"]
    assert dev.sid == sid and (dev.worktree / "work.txt").read_text() == "keep me"
    assert ca.has_transcript(dev.real, sid)  # the conversation came along
    assert str(dev.real) in _worktrees(repo)
    assert not (root / "claude-dev").exists() and not (state / "claude-dev.json").exists()
    assert agents["linked"].sid == linked_sid and agents["linked"].worktree.is_symlink()


# --- adopt -------------------------------------------------------------------------


def _session_file(pid: int, session: str, cwd: Path, name: str | None = None) -> None:
    d = ca.claude_home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "sessionId": session, "cwd": str(cwd), "name": name}))


def _adopt_args(session=None, dir=None, name=None, move=False, dry_run=False):
    return _args(session=session, dir=dir, name=name, move=move, grace=0.0, wait=0.0,
                 dry_run=dry_run)


def test_find_session_live_then_from_its_records(home: Path) -> None:
    work = home / "work"
    work.mkdir()
    _conversation(work, "s1")
    proc = subprocess.Popen(["sleep", "30"])
    try:
        _session_file(proc.pid, "s1", work)
        assert ca.find_session("s1") == ([proc.pid], work)
    finally:
        proc.kill()
        proc.wait()
    assert ca.find_session("s1") == ([], work)
    assert ca.find_session("nope") == ([], None)


def test_adopt_by_symlink_stops_the_old_process_and_takes_its_name(home: Path) -> None:
    work = home / "work"
    work.mkdir()
    sid = str(uuid.uuid4())
    _conversation(work, sid)
    proc = subprocess.Popen(["sleep", "30"])
    _session_file(proc.pid, sid, work, name="Reviewer")
    ca.cmd_adopt(_cfg(home), _adopt_args(session=sid))
    assert proc.wait(timeout=5) is not None  # stopped
    a = ca.resolve(_cfg(home), sid)
    assert a.name == "Reviewer" and a.worktree.is_symlink() and a.real == work.resolve()
    assert a.meta["adopted_from"] == str(work.resolve())
    assert "--resume" in ca.claude_args(a, _cfg(home))


def test_adopt_move_relocates_a_locked_worktree_and_its_conversation(home: Path) -> None:
    repo = _git_repo(home / "repo")
    wt = repo / ".claude" / "worktrees" / "bridge-cse_X"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--lock", str(wt)], check=True)
    (wt / "uncommitted.txt").write_text("keep me")
    sid = str(uuid.uuid4())
    _conversation(wt, sid)
    ca.cmd_adopt(_cfg(home), _adopt_args(dir=str(wt), name="sre", move=True))
    a = ca.resolve(_cfg(home), "sre")
    assert a.sid == sid and not a.worktree.is_symlink()
    assert (a.worktree / "uncommitted.txt").read_text() == "keep me"
    assert str(a.real) in _worktrees(repo) and str(wt) not in _worktrees(repo)
    assert ca.has_transcript(a.real, sid)


def test_adopt_refuses_a_main_checkout_move_before_stopping_anything(home: Path) -> None:
    repo = _git_repo(home / "repo")
    sid = str(uuid.uuid4())
    _conversation(repo, sid)
    proc = subprocess.Popen(["sleep", "30"])
    try:
        _session_file(proc.pid, sid, repo)
        with pytest.raises(SystemExit, match="main checkout"):
            ca.cmd_adopt(_cfg(home), _adopt_args(session=sid, name="x", move=True))
        assert proc.poll() is None  # still running
        assert ca.list_agents(home / "agents") == []
    finally:
        proc.kill()
        proc.wait()


def test_adopt_dry_run_changes_nothing(home: Path) -> None:
    work = home / "work"
    work.mkdir()
    sid = str(uuid.uuid4())
    _conversation(work, sid)
    proc = subprocess.Popen(["sleep", "30"])
    try:
        _session_file(proc.pid, sid, work)
        ca.cmd_adopt(_cfg(home), _adopt_args(session=sid, name="x", dry_run=True))
        assert proc.poll() is None and ca.list_agents(home / "agents") == []
    finally:
        proc.kill()
        proc.wait()


def test_a_failed_move_is_rolled_back_and_the_worktree_relocked(home: Path) -> None:
    repo = _git_repo(home / "repo")
    wt = repo / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--lock", str(wt)], check=True)
    sid = str(uuid.uuid4())
    _conversation(wt, sid)
    root = home / "agents"
    root.chmod(0o555)  # passes the checks; git's move into it then fails
    try:
        with pytest.raises(SystemExit, match="Nothing was adopted"):
            ca.cmd_adopt(_cfg(home), _adopt_args(dir=str(wt), name="sre", move=True))
    finally:
        root.chmod(0o755)
    assert ca.list_agents(root) == []
    assert wt.is_dir() and ca.has_transcript(wt, sid)
    assert ca._is_locked(str(repo), wt)


def test_the_claude_ai_link_comes_from_the_conversation(home: Path) -> None:
    a = _agent(home, "reviewer")
    assert ca.remote_link(a.real, a.sid) is None
    with (ca.project_dir(a.real) / f"{a.sid}.jsonl").open("a") as f:
        for bridge in ("cse_OLD", "cse_01J8zb"):
            f.write(json.dumps({"type": "bridge-session", "bridgeSessionId": bridge}) + "\n")
    assert ca.remote_link(a.real, a.sid) == "https://claude.ai/code/session_01J8zb"


# --- the incident: a leftover old-layout directory must never touch a live agent ------


def test_a_leftover_old_directory_never_touches_the_live_conversation(home: Path) -> None:
    """A directory reappearing at an agent's old path (Docker recreating a bind
    mount did it) once made the supervisor copy the stale conversation there
    over the agent's live one, every tick."""
    root = home / "agents"
    live = _agent(home, "claude-dev")
    live_file = ca.project_dir(live.real) / f"{live.sid}.jsonl"
    live_file.write_text('{"said": "everything since"}\n')
    old = root / "claude-dev"
    (old / "tests").mkdir(parents=True)
    _conversation(old, live.sid, age=3600)  # the stale copy under the old path

    sup = ca.Supervisor(_cfg(home))
    sup.tick()
    sup.tick()

    assert live_file.read_text() == '{"said": "everything since"}\n'
    assert (old / "tests").is_dir()  # left alone, not moved or merged
    assert [a.sid for a in ca.list_agents(root)] == [live.sid]
    assert sup.reported == {"claude-dev"}  # reported, once


def test_an_empty_directory_is_not_turned_into_an_agent(home: Path) -> None:
    (home / "agents" / "scratch").mkdir()
    ca.Supervisor(_cfg(home)).tick()
    assert ca.list_agents(home / "agents") == []
    assert (home / "agents" / "scratch").is_dir()


def test_copying_never_overwrites_a_newer_conversation(home: Path) -> None:
    a, b = home / "a", home / "b"
    a.mkdir(), b.mkdir()
    _conversation(a, "s", age=100)
    newer = _conversation(b, "s")
    newer.write_text("newer\n")
    ca._copy_conversation("s", a, b)  # kept: the destination is newer
    assert newer.read_text() == "newer\n"
    os.utime(newer, (time.time() - 1000, time.time() - 1000))
    with pytest.raises(RuntimeError, match="not overwriting"):
        ca._copy_conversation("s", a, b)  # older, but not ours to replace
    ca._copy_conversation("s", a, b, replace_older=True)
    assert newer.read_text() != "newer\n"
