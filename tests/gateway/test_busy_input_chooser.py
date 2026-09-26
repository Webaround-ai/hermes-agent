"""busy_input_chooser: a plugin may pick the busy mode per incoming message (gateway/run_busy.py).

Contracts: no plugin -> the configured mode exactly as before; a valid answer overrides it ("append" is
steer); a slow (> 400 ms), raising or off-list answer -> the configured mode; the interrupt->queue
demotions (active subagents, compression in flight) still win over the chooser.
"""

import threading
import time

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import plugins
from hermes_cli.plugins import PluginManager


class _Agent:
    def __init__(self, children=()):
        self.payload = None
        self._active_children = list(children)
        self._active_children_lock = threading.Lock()

    def steer(self, text):
        self.payload = text
        return True


def _event(text="also add the totals row"):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c", user_id="u", chat_type="dm")
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source, message_id="m")


def _install(monkeypatch, *callbacks):
    mgr = PluginManager()
    mgr._discovered = True
    if callbacks:
        mgr._hooks["busy_input_chooser"] = list(callbacks)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: mgr)


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    r = GatewayRunner(config=GatewayConfig())

    async def _no_compression(_key):
        return False

    monkeypatch.setattr(r, "_session_has_compression_in_flight", _no_compression)
    return r


def _running(runner, agent, goal="build the monthly sales report", elapsed=12.0):
    turn = runner._session_state("key").turn
    turn.agent = agent
    turn.event = _event(goal)
    turn.started_ts = time.time() - elapsed
    return agent


@pytest.mark.asyncio
async def test_no_plugin_keeps_the_configured_mode(runner, monkeypatch):
    _install(monkeypatch)
    agent = _running(runner, _Agent())
    outcome = await runner._resolve_busy_steer_or_redirect(_event(), "key", "queue", agent)
    assert outcome.effective_mode == "queue" and not outcome.steered and agent.payload is None


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["steer", "append", " Append "])
async def test_chooser_overrides_the_configured_mode(runner, monkeypatch, answer):
    seen = {}

    def chooser(**kw):
        seen.update(kw)
        return answer

    _install(monkeypatch, chooser)
    agent = _running(runner, _Agent())
    outcome = await runner._resolve_busy_steer_or_redirect(_event(), "key", "queue", agent)
    assert outcome.effective_mode == "steer" and outcome.steered is True
    assert agent.payload.endswith("also add the totals row")
    assert seen["text"] == "also add the totals row" and seen["configured_mode"] == "queue"
    assert seen["running_goal"] == "build the monthly sales report"
    assert 11 <= seen["elapsed_seconds"] <= 60 and seen["session_key"] == "key"


@pytest.mark.asyncio
async def test_demotion_for_active_subagents_beats_the_chooser(runner, monkeypatch):
    _install(monkeypatch, lambda **_: "interrupt")
    agent = _running(runner, _Agent(children=[_Agent()]))
    outcome = await runner._resolve_busy_steer_or_redirect(_event(), "key", "steer", agent)
    assert outcome.effective_mode == "queue" and outcome.demoted_for_subagents is True


@pytest.mark.asyncio
@pytest.mark.parametrize("behaviour", ["slow", "raises", "off_list"])
async def test_broken_chooser_falls_back_to_the_configured_mode(runner, monkeypatch, behaviour):
    def chooser(**_):
        if behaviour == "slow":
            time.sleep(2)
            return "interrupt"
        if behaviour == "raises":
            raise RuntimeError("chooser down")
        return "shout"

    _install(monkeypatch, chooser)
    agent = _running(runner, _Agent())
    started = time.monotonic()
    outcome = await runner._resolve_busy_steer_or_redirect(_event(), "key", "queue", agent)
    assert outcome.effective_mode == "queue" and agent.payload is None
    assert time.monotonic() - started < 1.0  # 400 ms hard deadline, never the plugin's 2 s
