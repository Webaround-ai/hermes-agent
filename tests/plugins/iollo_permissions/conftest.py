"""Shared fixtures for the iollo-permissions plugin: the module, a sandbox layout and a stub judge."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

FIXTURE_POLICY = Path(__file__).with_name("permissions.yaml")
PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "iollo-permissions"


def load_plugin():
    """Import the bundled plugin package the way the loader names it (``hermes_plugins.iollo_permissions``)."""
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    name = "hermes_plugins.iollo_permissions"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN_DIR)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class StubJudge:
    """Canned approval-model answers; counts calls and keeps the last messages."""

    def __init__(self, answer="APPROVE", delay=0.0):
        self.answer, self.delay, self.calls, self.messages = answer, delay, 0, None

    def __call__(self, messages, timeout):
        import time
        self.calls += 1
        self.messages = messages
        if self.delay:
            time.sleep(self.delay)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@pytest.fixture
def plugin():
    return load_plugin()


@pytest.fixture
def box(tmp_path, monkeypatch, plugin):
    """A workspace, one write root and an outside directory, with the plugin configured for them.
    Runs as the box; scratch roots are emptied so tmp_path itself counts as "outside"."""
    root = tmp_path.resolve()
    layout = types.SimpleNamespace(
        root=root, ws=root / "ws", docs=root / "Documents", outside=root / "outside",
        activity=root / "profile" / "activity.jsonl",
    )
    for d in (layout.ws, layout.docs, layout.outside):
        d.mkdir()
    entry = {"path": str(FIXTURE_POLICY), "workspace": str(layout.ws), "write_roots": [str(layout.docs)],
             "scratch_roots": [], "activity": str(layout.activity), "judge_timeout_s": 0.5}
    layout.entry = entry
    from hermes_plugins.iollo_permissions import policy
    monkeypatch.setattr(policy, "_platform_key", lambda: "box")
    monkeypatch.setattr(plugin, "_config_entry", lambda: layout.entry)
    layout.judge = StubJudge("APPROVE")
    from hermes_plugins.iollo_permissions.judge import Judge
    plugin._reset_for_tests(Judge(call=layout.judge))
    return layout


def use_judge(plugin, stub):
    from hermes_plugins.iollo_permissions.judge import Judge
    plugin._reset_for_tests(Judge(call=stub))
    return stub
