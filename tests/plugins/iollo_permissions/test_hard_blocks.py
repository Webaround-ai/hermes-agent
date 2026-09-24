"""Tier 4: hard blocks happen in code, before any judge call or approval, and survive yolo."""

import os

import pytest

from tests.plugins.iollo_permissions.conftest import FIXTURE_POLICY, StubJudge, use_judge

TIER4_COMMANDS = [
    "dd if=/dev/zero of=/dev/disk2",
    "mkfs.ext4 /dev/sdb1",
    "diskutil eraseDisk JHFS+ Blank disk2",
    "diskutil apfs deleteContainer disk3",
    "csrutil disable",
    "spctl --master-disable",
    "fdesetup disable",
    "tccutil reset All",
    "security export -k login.keychain -o keys.p12",
    "security dump-keychain",
    "security find-generic-password -s example -w",
    "sudo ls",
    # quoting and wrapping tricks
    "d''d if=/dev/zero of=/dev/disk2",
    '"diskutil" eraseDisk JHFS+ Blank disk2',
    "di\\skutil eraseDisk JHFS+ Blank disk2",
    'bash -c "csrutil disable"',
    "echo ok; /usr/sbin/diskutil eraseDisk JHFS+ Blank disk2",
    "env FOO=1 dd if=/dev/zero of=/dev/disk2",
    "sudo -u root dd if=/dev/zero of=/dev/disk2",
]


def _terminal(plugin, box, command, **extra):
    return plugin._on_pre_tool_call(tool_name="terminal", args={"command": command, "workdir": str(box.ws), **extra},
                                    session_id="s1")


@pytest.mark.parametrize("command", TIER4_COMMANDS)
def test_tier4_command_blocks_without_judge(plugin, box, command):
    result = _terminal(plugin, box, command)
    assert result["action"] == "block"
    assert result["message"].startswith("Stopped before ") and result["message"].endswith("Iollo never does this.")
    assert box.judge.calls == 0


def _destructive(box):
    ws, out = box.ws, box.outside
    (out / "notes.txt").write_text("keep me")
    (ws / "link").symlink_to(out)
    return [
        "rm -rf /etc",
        "r''m -rf /etc",
        "rm${IFS}-rf${IFS}/etc",
        "'rm' -rf /etc",
        "cd / && rm -rf etc",
        f"rm -rf {ws}/../outside",
        f"rm -rf {ws}/link/",
        "rm -rf link/",
        f"find {out} -delete",
        f"find {out} -name '*.txt' | xargs rm",
        f"find {out} -exec rm {{}} +",
        f"echo x > {out}/notes.txt",
        f"mv {out}/notes.txt {ws}/",
        f"truncate -s 0 {out}/notes.txt",
        f"rm -rf {ws}",
        f"rm -rf {ws}/.versions",
        'rm -rf "$SOME_UNSET_VARIABLE_XYZ/data"',
        "sudo rm -rf /etc",
    ]


def test_destructive_targets_outside_the_workspace_block(plugin, box):
    for command in _destructive(box):
        result = _terminal(plugin, box, command)
        assert result and result["action"] == "block", command
        assert result["message"].startswith("Stopped before "), command
    assert box.judge.calls == 0


def test_sudo_wrapper_is_unwrapped_even_without_a_sudo_rule(plugin, box, tmp_path):
    policy = tmp_path / "no-sudo.yaml"
    policy.write_text("\n".join(line for line in FIXTURE_POLICY.read_text().splitlines() if "sudo" not in line))
    box.entry = {**box.entry, "path": str(policy)}
    result = _terminal(plugin, box, "sudo rm -rf /etc")
    assert result["action"] == "block" and "deleting" in result["message"]


def test_blocks_hold_with_judge_approving_and_under_yolo(plugin, box, monkeypatch):
    """Through the real pre_tool_call resolution: a block is never turned into a prompt or a pass."""
    import tools.approval as approval
    from hermes_cli import plugins as hp
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", True)
    monkeypatch.setattr(hp, "_get_pre_tool_call_directive_details", lambda tool_name, args, **kw: hp._PreToolCallDirective(
        **(plugin._on_pre_tool_call(tool_name=tool_name, args=args, **kw) or {})))
    asked = []
    monkeypatch.setattr(approval, "request_tool_approval", lambda *a, **k: asked.append(a) or {"approved": True})
    use_judge(plugin, box.judge)
    for command in ("rm -rf /etc", "diskutil eraseDisk JHFS+ Blank disk2"):
        message = hp.resolve_pre_tool_block("terminal", {"command": command, "workdir": str(box.ws)})
        assert message and message.startswith("Stopped before ")
    assert asked == [] and box.judge.calls == 0


@pytest.mark.parametrize("command", [
    "ls -la /etc", 'echo "rm -rf /etc"', "git status", "ls 2>/dev/null", "rm -rf build",
    "rm -rf ./*.txt", "cat /etc/hosts > copy.txt", "security find-generic-password -s example",
    "mkdir -p ../outside/newdir",
])
def test_ordinary_commands_are_not_hard_blocked(plugin, box, command):
    (box.ws / "build").mkdir()
    result = _terminal(plugin, box, command)
    assert result is None  # the stub judge APPROVEs
    assert box.judge.calls == 1


def _box_scratch(box):
    """The policy's own box scratch roots (/tmp, /root/work, $HERMES_HOME) instead of the empty override."""
    box.entry = {k: v for k, v in box.entry.items() if k != "scratch_roots"}


def test_deletes_in_tmp_and_hermes_home_are_tier1(plugin, box, monkeypatch):
    import os
    from pathlib import Path
    _box_scratch(box)
    home = Path(os.environ["HERMES_HOME"])
    (home / "cache").mkdir(exist_ok=True)
    for command in ("rm -rf /tmp/iollo-test-scratch", "rm -f /tmp/a.png /tmp/b.png", "echo x > /tmp/iollo-note.txt",
                    f"rm -rf {home}/cache/old", "rm -rf $HERMES_HOME/cache/old", "find /tmp/iollo-x -delete"):
        assert _terminal(plugin, box, command) is None, command
    assert box.judge.calls == 6


def test_etc_is_still_blocked_with_scratch_roots(plugin, box):
    _box_scratch(box)
    for command in ("rm -rf /etc", "rm -rf /tmp", "echo x > /etc/hosts", "find /etc -delete"):
        result = _terminal(plugin, box, command)
        assert result and result["action"] == "block", command
    box.entry = {**box.entry, "scratch_roots": ["$HERMES_HOME"]}  # pytest's HERMES_HOME lives under /tmp
    assert _terminal(plugin, box, "rm -rf $HERMES_HOME")["action"] == "block"
    assert box.judge.calls == 0


def test_scratch_roots_come_from_the_policy_and_expand_hermes_home(plugin, box):
    import os
    _box_scratch(box)
    settings = plugin._settings()
    assert os.path.realpath("/tmp") in settings.scratch_roots
    assert os.path.realpath(os.environ["HERMES_HOME"]) in settings.scratch_roots


def test_terminal_writes_into_a_write_root_point_to_the_file_tools(plugin, box):
    (box.docs / "a.txt").write_text("x")
    for command in (f"echo x > {box.docs}/a.txt", f"rm {box.docs}/a.txt", f"cp notes {box.docs}/"):
        result = _terminal(plugin, box, command)
        assert result["action"] == "block"
        assert "write_file or patch" in result["message"] and "files_trash" in result["message"]


def test_symlink_resolution_uses_realpath(plugin, box):
    from hermes_plugins.iollo_permissions import hardblock
    (box.ws / "escape").symlink_to(box.outside)
    targets = hardblock.terminal_targets("rm -rf escape/", str(box.ws))
    assert [t.path for t in targets] == [os.path.realpath(box.outside)]


def test_policy_that_cannot_be_read_fails_closed_for_the_terminal(plugin, box, tmp_path):
    box.entry = {**box.entry, "path": str(tmp_path / "missing.yaml")}
    assert _terminal(plugin, box, "ls")["action"] == "block"
    assert plugin._on_pre_tool_call(tool_name="read_file", args={"path": "x"}) is None
