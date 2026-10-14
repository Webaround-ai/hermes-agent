"""A running job's fire claim against owner-side changes to its record (iollo task incident,
2026-10-14: a browser purchase run cut mid-tool with "lost its durable fire claim ownership").

* A rewrite of the record through the jobs API keeps the claim; a rewrite that drops the claim
  while the run is live is re-adopted, never once another run has completed.
* A deleted record ends the run at the next tool boundary, not mid-tool; a re-owned claim (another
  run is live) still interrupts at once.
* The heartbeat logs WHY it lost the claim, at WARNING.
"""

import logging
import threading
import time

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _claimed_job(name="iollo-task-t1"):
    from cron.jobs import claim_job_for_fire, create_job, get_job

    job = create_job(prompt="buy the thing", schedule="every 1h", name=name)
    assert claim_job_for_fire(job["id"]) is True
    return get_job(job["id"])


def test_running_claim_survives_record_rewrites(temp_home):
    from cron import jobs

    job = _claimed_job()
    owner = job["fire_claim"]["by"]
    started = job.get("last_run_at")

    jobs.update_job(job["id"], {"prompt": "buy the thing, size M", "name": "iollo-task-t1"})
    assert jobs.fire_claim_heartbeat_status(job["id"], expected_owner=owner) == (jobs.FIRE_CLAIM_OK, "")

    def drop_claim(all_jobs, _i, record):         # a writer that rewrote the record without its claim
        record["fire_claim"] = None
        jobs.save_jobs(all_jobs)

    jobs._with_job(job["id"], drop_claim)
    status, _ = jobs.fire_claim_heartbeat_status(
        job["id"], expected_owner=owner, run_started_last_run_at=started)
    assert status == jobs.FIRE_CLAIM_READOPTED
    assert jobs.heartbeat_fire_claim(job["id"], expected_owner=owner) is True

    # Once a run has completed (last_run_at stamped, claim cleared) nothing is re-claimed.
    assert jobs.mark_job_run(job["id"], True, expected_fire_owner=owner) is True
    status, _ = jobs.fire_claim_heartbeat_status(
        job["id"], expected_owner=owner, run_started_last_run_at=started)
    assert status == jobs.FIRE_CLAIM_CLEARED
    assert jobs.get_job(job["id"])["fire_claim"] is None


@pytest.mark.parametrize("change, reason, graceful", [
    ("delete", "record_missing", True),
    ("steal", "owner_mismatch", False),
])
def test_heartbeat_names_why_the_claim_was_lost(temp_home, monkeypatch, caplog, change, reason, graceful):
    import cron.scheduler as sched
    from cron import jobs

    monkeypatch.setattr(sched, "_RUN_CLAIM_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(sched, "_FIRE_CLAIM_MISS_CONFIRM_SECONDS", 0.01)
    job = _claimed_job()

    def run(lost):
        if change == "delete":
            assert jobs.remove_job(job["id"]) is True
        else:
            def re_own(all_jobs, _i, record):
                record["fire_claim"] = {**record["fire_claim"], "by": "replacement:deadbeef"}
                jobs.save_jobs(all_jobs)
            jobs._with_job(job["id"], re_own)
        assert lost.wait(5)
        return lost.reason, lost.graceful

    with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
        assert sched._run_with_fire_claim_heartbeat(job, run) == (reason, graceful)
    assert f"fire claim ownership lost (reason={reason}" in caplog.text


class _ToolAgent:
    """Runs one long tool, then keeps thinking until interrupted."""

    def __init__(self):
        self._current_tool = None
        self.tool_may_finish = threading.Event()
        self.in_tool = threading.Event()
        self.interrupted_during = []

    def interrupt(self, _message=None, **_kw):
        self.interrupted_during.append(self._current_tool)

    def run_conversation(self, _prompt, task_id=None):
        self._current_tool = "browser_exec"
        self.in_tool.set()
        self.tool_may_finish.wait(10)
        self._current_tool = None
        deadline = time.monotonic() + 10
        while not self.interrupted_during and time.monotonic() < deadline:
            time.sleep(0.01)
        return {"final_response": "stopped"}


@pytest.mark.parametrize("reason, waits_for_tool", [("record_missing", True), ("owner_mismatch", False)])
def test_deleted_record_stops_at_the_next_tool_boundary(monkeypatch, reason, waits_for_tool):
    import cron.scheduler as sched

    monkeypatch.setattr(sched, "_CRON_MONITOR_POLL_SECONDS", 0.05)
    monkeypatch.setattr(sched, "_RECORD_DELETED_POLL_SECONDS", 0.02)
    agent = _ToolAgent()
    lost = sched._ClaimLostEvent()
    outcome = {}

    def watch():
        try:
            sched._run_agent_with_watchdog(agent, "p", {"id": "j1"}, "j1", "iollo-task-t1", "cron:j1:x", lost)
        except RuntimeError as error:
            outcome["error"] = str(error)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    assert agent.in_tool.wait(5)
    lost.latch(reason)
    time.sleep(0.3)
    mid_tool = list(agent.interrupted_during)
    agent.tool_may_finish.set()
    thread.join(10)

    assert f"lost its durable fire claim ownership ({reason})" in outcome["error"]
    if waits_for_tool:
        assert mid_tool == [] and agent.interrupted_during == [None]
    else:
        assert mid_tool == ["browser_exec"]
