"""Versions before overwrite, files_trash, the activity file, the policy loader and bundled discovery."""

import datetime as dt
import json
import sys

import pytest
import yaml

from tests.plugins.iollo_permissions.conftest import FIXTURE_POLICY


def _versions_of(box, path):
    today = dt.date.today().isoformat()
    base = box.ws / ".versions" / today / str(path).lstrip("/")
    return sorted(base.parent.glob(base.name + "*"))


def test_version_is_written_before_overwrite(plugin, box):
    target = box.ws / "notes.md"
    target.write_text("v1")
    assert plugin._on_pre_tool_call(tool_name="write_file", args={"path": str(target), "content": "v2"}) is None
    saved = _versions_of(box, target)
    assert [p.read_text() for p in saved] == ["v1"]
    target.write_text("v2")
    plugin._on_pre_tool_call(tool_name="patch", args={"path": str(target), "old_string": "v2", "new_string": "v3"})
    assert sorted(p.read_text() for p in _versions_of(box, target)) == ["v1", "v2"]


def test_write_root_files_are_versioned_too_and_new_or_outside_files_are_not(plugin, box):
    in_root = box.docs / "cv.txt"
    in_root.write_text("old")
    plugin._on_pre_tool_call(tool_name="write_file", args={"path": str(in_root), "content": "new"})
    assert [p.read_text() for p in _versions_of(box, in_root)] == ["old"]
    plugin._on_pre_tool_call(tool_name="write_file", args={"path": str(box.ws / "new.txt"), "content": "x"})
    outside = box.outside / "x.txt"
    outside.write_text("x")
    plugin._on_pre_tool_call(tool_name="write_file", args={"path": str(outside), "content": "y"})
    assert _versions_of(box, box.ws / "new.txt") == [] and _versions_of(box, outside) == []


def test_v4a_patch_paths_are_versioned(plugin, box):
    target = box.ws / "a.py"
    target.write_text("print(1)\n")
    patch = f"*** Begin Patch\n*** Update File: {target}\n@@\n-print(1)\n+print(2)\n*** End Patch\n"
    plugin._on_pre_tool_call(tool_name="patch", args={"mode": "patch", "patch": patch})
    assert [p.read_text() for p in _versions_of(box, target)] == ["print(1)\n"]


def test_versions_are_pruned_after_keep_days(plugin, box):
    from hermes_plugins.iollo_permissions import files
    settings = plugin._settings()
    versions = box.ws / ".versions"
    old = versions / (dt.date.today() - dt.timedelta(days=31)).isoformat()
    kept = versions / (dt.date.today() - dt.timedelta(days=29)).isoformat()
    for d in (old, kept):
        d.mkdir(parents=True)
        (d / "f").write_text("x")
    files.prune_versions(settings)
    assert not old.exists() and kept.exists()


def test_files_trash_moves_into_the_trash_directory_and_refuses_outside(plugin, box):
    doomed = box.ws / "old.txt"
    doomed.write_text("bye")
    outside = box.outside / "keep.txt"
    outside.write_text("keep")
    result = json.loads(plugin._files_trash({"paths": [str(doomed), str(outside), "../outside/keep.txt"]}))
    assert result["trashed"] == [str(doomed)]
    assert [r["path"] for r in result["refused"]] == [str(outside), "../outside/keep.txt"]
    assert not doomed.exists() and outside.exists()
    trashed = list((box.ws / ".trash").rglob("old.txt"))
    assert len(trashed) == 1 and trashed[0].read_text() == "bye"


def test_files_trash_uses_the_trash_command(plugin, box):
    record = box.root / "trash-args.json"
    box.entry = {**box.entry, "trash_command": [
        sys.executable, "-c", f"import json,sys; open({str(record)!r},'w').write(json.dumps(sys.argv[1:]))"]}
    doomed = box.docs / "draft.txt"
    doomed.write_text("x")
    result = json.loads(plugin._files_trash({"paths": [str(doomed)]}))
    assert result["trashed"] == [str(doomed)]
    assert json.loads(record.read_text()) == [str(doomed)]
    assert not (box.ws / ".trash").exists()


def test_files_trash_refuses_the_workspace_itself(plugin, box):
    result = json.loads(plugin._files_trash({"paths": [str(box.ws)]}))
    assert result["trashed"] == [] and box.ws.exists()


def test_activity_line_once_per_success_never_on_failure(plugin, box):
    args = {"target": "whatsapp:+10000000000", "message": "running late"}
    plugin._on_post_tool_call(tool_name="send_message", args=args, result="{}", status="ok", session_id="s1")
    plugin._on_post_tool_call(tool_name="send_message", args=args, result='{"error": "x"}', status="error",
                              session_id="s1")
    plugin._on_post_tool_call(tool_name="read_file", args={}, result="{}", status="ok", session_id="s1")
    lines = box.activity.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert set(entry) == {"did", "tool", "at"} and entry["tool"] == "send_message"
    assert "running late" not in lines[0] and "+10000000000" not in lines[0]


def test_policy_rejects_other_schema_versions(plugin, tmp_path):
    from hermes_plugins.iollo_permissions import policy
    data = yaml.safe_load(FIXTURE_POLICY.read_text())
    policy.parse_policy(data)
    with pytest.raises(policy.PolicyError):
        policy.parse_policy({**data, "version": 2})
    with pytest.raises(policy.PolicyError):
        policy.parse_policy({**data, "tier4": {"commands": ["("]}})


def test_bundled_plugin_is_off_by_default_and_loads_when_enabled(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    home = Path(os.environ["HERMES_HOME"])
    for key in list(sys.modules):
        if key.startswith(("hermes_plugins", "hermes_cli.plugins")):
            del sys.modules[key]
    from hermes_cli.plugins import _ensure_plugins_discovered
    mgr = _ensure_plugins_discovered(force=True)
    assert not mgr._plugins["iollo-permissions"].enabled

    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {
        "enabled": ["iollo-permissions"],
        "entries": {"iollo-permissions": {"settings": {"path": str(FIXTURE_POLICY), "workspace": str(tmp_path)}}},
    }}))
    for key in list(sys.modules):
        if key.startswith(("hermes_plugins", "hermes_cli.plugins")):
            del sys.modules[key]
    from hermes_cli.plugins import _ensure_plugins_discovered as rediscover
    mgr = rediscover(force=True)
    loaded = mgr._plugins["iollo-permissions"]
    assert loaded.enabled and loaded.error is None
    assert "files_trash" in loaded.tools_registered
    from hermes_cli.plugins import get_pre_tool_call_directive
    directive, message = get_pre_tool_call_directive("terminal", {"command": "diskutil eraseDisk JHFS+ X disk2"})
    assert directive == "block" and message.startswith("Stopped before erasing")
    from tools.registry import registry
    assert registry.get_entry("files_trash") is not None
