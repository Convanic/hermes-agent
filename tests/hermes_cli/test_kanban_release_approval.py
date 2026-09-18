"""Release approval saga contracts: stale/replay/race/task binding and argv execution."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


def _present(conn, task_id: str, *, release: str = "2026.09.18-01+abc", message: str = "m1"):
    from hermes_cli.kanban_release_approval import present_release
    return present_release(
        conn,
        task_id=task_id,
        release_id=release,
        manifest_sha256="a" * 64,
        manual_test_cases_digest="b" * 64,
        workflow_status="Auf Dev zur Prüfung",
        active_dev_release_id=release,
        platform="telegram",
        chat_id="chat-1",
        thread_id="",
        actor_id="armin-1",
        presented_message_id=message,
        previous_release_id="2026.09.17-02+old",
        rollback_available=True,
    )


def _adapter(tmp_path: Path, calls: Path) -> list[str]:
    script = tmp_path / "promotion adapter ; safe.py"
    script.write_text(
        "import json,sys\n"
        f"p={str(calls)!r}\n"
        "open(p,'a',encoding='utf-8').write(json.dumps(sys.argv[1:])+'\\n')\n"
        "a=sys.argv; rid=a[a.index('--release-id')+1]\n"
        "print(json.dumps({'ok':True,'release_id':rid,'dev':{'result':'success','active_release_id':rid},"
        "'test':{'result':'success','active_release_id':rid},'production':{'result':'success','active_release_id':rid},"
        "'previous_release_id':'2026.09.17-02+old','rollback_available':True}))\n"
    )
    return [sys.executable, str(script), "literal ; $(not-shell)"]


def _approve(conn, argv, *, message="m1", now=100):
    from hermes_cli.kanban_release_approval import ApprovalContext, process_approval
    return process_approval(
        conn,
        text="freigegeben",
        context=ApprovalContext(
            platform="telegram", chat_id="chat-1", thread_id="", actor_id="armin-1",
            reply_to_message_id=message,
        ),
        promotion_argv=argv,
        now=now,
    )


def test_release_approval_is_bound_and_executes_literal_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    calls = tmp_path / "calls.jsonl"
    with connect() as conn:
        task = kb.create_task(conn, title="Release")
        gate = _present(conn, task)
        result = _approve(conn, _adapter(tmp_path, calls))
        assert result.ok and result.classification == "success"
        assert result.release_id == gate.release_id
        assert result.active_release_id == gate.release_id
        assert result.previous_release_id == "2026.09.17-02+old"
        assert result.rollback_available is True
        assert result.dev_result == result.test_result == result.production_result == "success"
        argv = json.loads(calls.read_text().splitlines()[0])
        assert argv[:2] == ["literal ; $(not-shell)", "promote"]
        assert argv[argv.index("--task-id") + 1] == task
        assert argv[argv.index("--release-id") + 1] == gate.release_id
        assert len(calls.read_text().splitlines()) == 1


def test_stale_release_replay_and_task_switch_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    calls = tmp_path / "calls.jsonl"
    argv = _adapter(tmp_path, calls)
    with connect() as conn:
        old_task = kb.create_task(conn, title="Old")
        _present(conn, old_task, message="old-message")
        new_task = kb.create_task(conn, title="New")
        _present(conn, new_task, release="2026.09.18-02+def", message="new-message")

        switched = _approve(conn, argv, message="old-message")
        assert not switched.ok and switched.classification == "stale_approval"
        accepted = _approve(conn, argv, message="new-message")
        assert accepted.ok
        replay = _approve(conn, argv, message="new-message", now=101)
        assert not replay.ok and replay.classification == "replay"
        assert len(calls.read_text().splitlines()) == 1


def test_manifest_dev_state_and_workflow_status_are_rechecked(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    calls = tmp_path / "calls.jsonl"
    with connect() as conn:
        task = kb.create_task(conn, title="Release")
        gate = _present(conn, task)
        conn.execute("UPDATE kanban_release_gates SET active_dev_release_id=? WHERE id=?", ("replacement", gate.gate_id))
        conn.commit()
        stale = _approve(conn, _adapter(tmp_path, calls))
        assert not stale.ok and stale.classification == "stale_approval"
        assert not calls.exists()

        other = kb.create_task(conn, title="Wrong state")
        gate2 = _present(conn, other, message="m2")
        conn.execute("UPDATE kanban_release_gates SET workflow_status=? WHERE id=?", ("Draft", gate2.gate_id))
        conn.commit()
        wrong = _approve(conn, _adapter(tmp_path, calls), message="m2")
        assert not wrong.ok and wrong.classification == "stale_approval"
        assert not calls.exists()


def test_non_exact_text_and_changed_current_release_state_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    calls = tmp_path / "calls.jsonl"
    argv = _adapter(tmp_path, calls)
    with connect() as conn:
        task = kb.create_task(conn, title="Release")
        gate = _present(conn, task)
        # The protocol token is byte-exact; gateways must not normalize it.
        from hermes_cli.kanban_release_approval import ApprovalContext, process_approval, update_release_state
        context = ApprovalContext("telegram", "chat-1", "", "armin-1", "m1")
        whitespace = process_approval(conn, text=" freigegeben", context=context,
                                      promotion_argv=argv, now=100)
        assert not whitespace.ok and whitespace.classification == "not_approval"
        update_release_state(conn, task_id=task, workflow_status="Auf Dev zur Prüfung",
                             active_dev_release_id="replacement",
                             manifest_sha256="c" * 64)
        stale = _approve(conn, argv, now=101)
        assert not stale.ok and stale.classification == "stale_approval"
        assert stale.release_id == gate.release_id
        assert not calls.exists()


def test_expired_promoting_claim_stays_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    calls = tmp_path / "calls.jsonl"
    with connect() as conn:
        task = kb.create_task(conn, title="Interrupted promotion")
        gate = _present(conn, task)
        from hermes_cli.kanban_release_approval import _operation_key
        row = conn.execute("SELECT * FROM kanban_release_gates WHERE id=?", (gate.gate_id,)).fetchone()
        conn.execute(
            "INSERT INTO kanban_release_sagas "
            "(operation_key,gate_id,state,owner_token,lease_expires,created_at,updated_at) "
            "VALUES (?,?,'promoting','dead-worker',1,1,1)",
            (_operation_key(row), gate.gate_id),
        )
        conn.execute("UPDATE kanban_release_gates SET gate_status='consuming' WHERE id=?", (gate.gate_id,))
        conn.commit()
        result = _approve(conn, _adapter(tmp_path, calls), now=100)
        assert not result.ok and result.classification == "in_progress"
        assert not calls.exists()


def test_parallel_double_delivery_invokes_adapter_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    db = kb.init_db()
    calls = tmp_path / "calls.jsonl"
    with connect(db) as conn:
        task = kb.create_task(conn, title="Race")
        _present(conn, task)

    barrier = threading.Barrier(2)
    results = []
    def run():
        with connect(db) as conn:
            barrier.wait()
            results.append(_approve(conn, _adapter(tmp_path, calls)))
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(result.ok for result in results) == 1
    assert {result.classification for result in results} <= {"success", "in_progress", "replay"}
    assert len(calls.read_text().splitlines()) == 1
