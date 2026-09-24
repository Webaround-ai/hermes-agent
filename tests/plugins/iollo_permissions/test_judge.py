"""The tier judge (APPROVE / ESCALATE / DENY), the keyword fallback and the smart-approval rubric."""

import json
import logging

import pytest

from tests.plugins.iollo_permissions.conftest import StubJudge, use_judge

CHECKOUT_SNAPSHOT = json.dumps({"success": True, "data": {"snapshot": "\n".join([
    '- heading "Your basket" [ref=e1]',
    '- text "Total: €42.50" [ref=e2]',
    '- textbox "Card number" [ref=e3]',
    '- textbox "Password" [ref=e4]',
    '- button "Place order" [ref=e5]',
])}})
FAKE_CARD = "4111 1111 1111 1111"  # the standard Visa test number


def _call(plugin, tool, args, session="s1"):
    return plugin._on_pre_tool_call(tool_name=tool, args=args, session_id=session)


def _see_checkout(plugin, session="s1"):
    plugin._on_post_tool_call(tool_name="browser_snapshot", args={}, result=CHECKOUT_SNAPSHOT, status="ok",
                              session_id=session)


def test_approve_is_tier1_no_directive(plugin, box):
    assert _call(plugin, "send_message", {"target": "whatsapp", "message": "hi"}) is None
    assert box.judge.calls == 1


def test_escalate_is_tier3_approve_directive(plugin, box):
    use_judge(plugin, StubJudge("ESCALATE"))
    _see_checkout(plugin)
    result = _call(plugin, "browser_click", {"ref": "@e5"})
    assert result["action"] == "approve"
    assert result["message"].startswith("tier3:pay: ")
    assert result["rule_key"].startswith("iollo-tier3:pay:browser_click:")


def test_escalate_without_a_detector_is_labelled_other(plugin, box):
    use_judge(plugin, StubJudge("ESCALATE"))
    result = _call(plugin, "send_message", {"target": "whatsapp", "message": "hi"})
    assert result["action"] == "approve" and result["message"].startswith("tier3:other: ")


def test_deny_is_a_hard_block(plugin, box):
    use_judge(plugin, StubJudge("DENY"))
    result = _call(plugin, "write_file", {"path": str(box.ws / "a.txt"), "content": "x"})
    assert result["action"] == "block" and result["message"].endswith("Iollo never does this.")


def test_rubric_in_system_prompt_and_arguments_redacted(plugin, box):
    stub = use_judge(plugin, StubJudge("APPROVE"))
    _see_checkout(plugin)
    _call(plugin, "browser_type", {"ref": "@e3", "text": "sk-ant-api03-" + "a" * 40})
    system, user = stub.messages[0]["content"], stub.messages[1]["content"]
    assert "<tier-rubric>" in system and "</tier-rubric>" in system
    assert "<tier-rubric>" not in user
    assert "tool: browser_type" in user and "detector: pay" in user
    assert "textbox Card number" in user
    assert "a" * 40 not in user


def test_verdicts_are_cached_per_session_and_call(plugin, box):
    args = {"target": "whatsapp", "message": "hi"}
    _call(plugin, "send_message", args)
    _call(plugin, "send_message", dict(args))
    assert box.judge.calls == 1
    _call(plugin, "send_message", args, session="s2")
    assert box.judge.calls == 2


def test_pure_reads_are_not_judged(plugin, box):
    for tool in ("read_file", "search_files", "browser_snapshot", "web_search"):
        assert _call(plugin, tool, {"path": "x", "query": "q"}) is None
    assert box.judge.calls == 0


@pytest.mark.parametrize("failure", ["timeout", "error", "garbage"])
def test_fallback_card_number_is_tier3_and_plain_send_is_tier1(plugin, box, caplog, failure):
    stub = {"timeout": StubJudge("APPROVE", delay=2.0), "error": StubJudge(RuntimeError("gateway down")),
            "garbage": StubJudge("Sure thing!")}[failure]
    use_judge(plugin, stub)
    box.entry = {**box.entry, "judge_timeout_s": 0.2}
    with caplog.at_level(logging.INFO):
        typed = _call(plugin, "browser_type", {"ref": "@e9", "text": FAKE_CARD})
        sent = _call(plugin, "send_message", {"target": "whatsapp", "message": "see you at 8"})
    assert typed["action"] == "approve" and typed["message"].startswith("tier3:pay: ")
    assert FAKE_CARD not in typed["message"]
    assert sent is None
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "judge_fallback" in logs
    assert FAKE_CARD not in logs and "see you at 8" not in logs


def test_fallback_detectors(plugin, box):
    use_judge(plugin, StubJudge("?"))
    _see_checkout(plugin)
    assert _call(plugin, "browser_click", {"ref": "@e5"})["message"].startswith("tier3:pay:")
    assert _call(plugin, "browser_click", {"ref": "@e1"}) is None
    assert _call(plugin, "browser_type", {"ref": "@e4", "text": "hunter2"})["message"].startswith("tier3:system:")
    assert _call(plugin, "browser_vault_save_login", {"site": "example.com"})["message"].startswith("tier3:system:")
    assert _call(plugin, "terminal", {"command": "pmset sleep 0", "workdir": str(box.ws)})["message"].startswith(
        "tier3:system:")
    many = {"to": ", ".join(f"person{i}@example.com" for i in range(5)), "subject": "hi"}
    assert _call(plugin, "email_send", many)["message"].startswith("tier3:bulk_new:")
    assert _call(plugin, "email_send", {"to": "person0@example.com"}) is None


def test_bulk_new_window_and_known_recipients(plugin, box):
    use_judge(plugin, StubJudge("?"))
    for i in range(4):
        args = {"to": f"friend{i}@example.com"}
        assert _call(plugin, "email_send", args) is None
        plugin._on_post_tool_call(tool_name="email_send", args=args, result="{}", status="ok", session_id="s1")
    assert _call(plugin, "email_send", {"to": "stranger@example.com"})["message"].startswith("tier3:bulk_new:")
    from hermes_constants import get_hermes_home
    stored = (get_hermes_home() / "memories" / "known-recipients.json").read_text()
    assert len(json.loads(stored)["sha256"]) == 4
    assert "example.com" not in stored


def test_smart_approval_sends_the_same_rubric(plugin, box, monkeypatch):
    from tools import approval_smart
    unregister = approval_smart.register_rubric_provider(plugin._smart_rubric)
    seen = {}

    class _Resp:
        choices = [type("C", (), {"message": type("M", (), {"content": "ESCALATE"})()})()]

    def fake_call_llm(**kwargs):
        seen.update(kwargs)
        return _Resp()

    import agent.auxiliary_client as aux
    monkeypatch.setattr(aux, "call_llm", fake_call_llm)
    try:
        assert approval_smart._smart_approve("launchctl bootout system/x", "flagged") == "escalate"
    finally:
        unregister()
    assert "<tier-rubric>" in seen["messages"][0]["content"]
    assert "<tier-rubric>" not in seen["messages"][1]["content"]
    assert seen["task"] == "approval"


def test_smart_approval_without_the_plugin_is_unchanged(monkeypatch):
    from tools import approval_smart
    assert approval_smart._provider_rubrics() == ""


def test_acceptance_checkout_click_asks_once_email_send_asks_none(plugin, box, monkeypatch):
    """Brief 002 acceptance with a stub judge, through the real pre_tool_call resolution."""
    import tools.approval as approval
    from hermes_cli import plugins as hp
    monkeypatch.setattr(hp, "_get_pre_tool_call_directive_details", lambda tool_name, args, **kw: hp._PreToolCallDirective(
        **(plugin._on_pre_tool_call(tool_name=tool_name, args=args, session_id="s1") or {})))
    asked = []
    monkeypatch.setattr(approval, "request_tool_approval",
                        lambda tool, reason, **k: asked.append((tool, reason)) or {"approved": True})

    def judge(messages, timeout):
        return "ESCALATE" if "Place order" in messages[1]["content"] else "APPROVE"

    use_judge(plugin, judge)
    _see_checkout(plugin)
    assert hp.resolve_pre_tool_block("browser_click", {"ref": "@e5"}) is None
    assert hp.resolve_pre_tool_block("email_send", {"to": "ana@example.com", "body": "Friday?"}) is None
    assert len(asked) == 1 and asked[0][0] == "browser_click" and asked[0][1].startswith("tier3:pay: ")
