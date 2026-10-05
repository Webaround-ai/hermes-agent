"""``approvals.timeouts``: a per-rule wait (Iollo 2026-10-05: a purchase approval waits ten minutes, every other
approval keeps ``approvals.timeout``). Unanswered still means refused (fail closed), only later."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

PAY = "plugin_rule:iollo-tier3:pay:commit_purchase:0123456789abcdef"
CONFIG = {"timeout": 60, "timeouts": {"iollo-tier3:pay:": 600, "iollo-tier3:": 120, "bad": "soon"}}


def _config(value):
    return patch("tools.approval_context._get_approval_config", return_value=value)


def test_the_longest_matching_prefix_wins_and_others_keep_the_global_timeout():
    from tools.approval_context import _get_approval_timeout_for
    with _config(CONFIG):
        assert _get_approval_timeout_for(PAY) == 600
        assert _get_approval_timeout_for("iollo-tier3:pay:browser_click:x") == 600   # without the namespace too
        assert _get_approval_timeout_for("plugin_rule:iollo-tier3:delete:files_trash:x") == 120
        assert _get_approval_timeout_for("rm -rf") == 60 and _get_approval_timeout_for("") == 60
    with _config({"timeout": 60, "timeouts": "nonsense"}):
        assert _get_approval_timeout_for(PAY) == 60


def test_the_human_wait_ceiling_covers_the_longest_window():
    from tools.approval_human_wait import HUMAN_WAIT_MARGIN_S, human_wait_ceiling
    with _config(CONFIG):
        assert human_wait_ceiling() == 600 + HUMAN_WAIT_MARGIN_S
    with _config({"timeout": 60}):
        assert human_wait_ceiling() == 60 + HUMAN_WAIT_MARGIN_S


def test_the_gateway_wait_uses_the_rule_window_and_times_out_fail_closed():
    from tools import approval_gateway_wait as wait
    seen = []

    def window(key=""):
        seen.append(key)
        return 0 if key == PAY else 3600

    with patch("tools.approval_context._get_approval_timeout_for", side_effect=window):
        started = time.monotonic()
        state = wait._poll_event(threading.Event(), "s1", interrupt_log="%s", pattern_key=PAY)
    assert state == "timeout" and seen == [PAY] and time.monotonic() - started < 5
