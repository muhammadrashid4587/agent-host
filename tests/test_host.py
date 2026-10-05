import asyncio

import pytest

from host import DESK_LIMIT, Host, HostError


async def test_notes_server_connects_on_startup(host):
    assert {"notes/add_note", "notes/list_notes", "notes/read_note"} <= set(host.offered_tools())
    assert {"spy/echo", "spy/secret", "spy/slow"} <= set(host.offered_tools())


async def test_allowed_call_is_forwarded_and_logged(host, spy_log):
    run = host.create_run("r", ["spy/echo"])
    call, status = await host.call(run.id, "spy/echo", {"text": "hi"})
    assert status == 200 and call.status == "ok" and call.allowed
    assert call.public()["text"] == "hi"
    assert spy_log() == [{"tool": "echo", "arguments": {"text": "hi"}}]


async def test_unchecked_tool_is_rejected_and_never_sent(host, spy_log):
    run = host.create_run("r", ["spy/echo"])
    call, status = await host.call(run.id, "spy/secret", {"text": "x"})
    assert status == 403 and call.status == "rejected" and not call.allowed
    assert spy_log() == []
    assert run.calls == [call]  # still in the log


async def test_bare_tool_name_resolves_within_the_run(host):
    run = host.create_run("r", ["spy/echo"])
    call, status = await host.call(run.id, "echo", {"text": "a"})
    assert status == 200 and call.tool == "spy/echo"


async def test_bad_arguments_are_rejected(host, spy_log):
    run = host.create_run("r", ["spy/echo"])
    _, status = await host.call(run.id, "spy/echo", ["not", "a", "dict"])
    assert status == 400 and spy_log() == []


async def test_tool_error_is_logged_as_error(host):
    run = host.create_run("r", ["notes/read_note"])
    call, status = await host.call(run.id, "notes/read_note", {"id": 999})
    assert status == 200 and call.status == "error" and "no note" in call.error


async def test_run_needs_offered_tools(host):
    with pytest.raises(HostError):
        host.create_run("r", [])
    with pytest.raises(HostError):
        host.create_run("r", ["nope/nothing"])


async def test_desk_limit_queues_runs_in_order(host, spy_log):
    runs = [host.create_run(f"r{i}", ["spy/echo"]) for i in range(DESK_LIMIT + 2)]
    assert [r.state for r in runs] == ["running"] * DESK_LIMIT + ["waiting"] * 2
    _, status = await host.call(runs[-1].id, "spy/echo", {"text": "too early"})
    assert status == 409 and spy_log() == []
    host.finish(runs[0].id)
    assert runs[DESK_LIMIT].state == "running" and runs[-1].state == "waiting"


async def test_cancel_interrupts_an_inflight_call(host):
    run = host.create_run("r", ["spy/slow"])
    pending = asyncio.create_task(host.call(run.id, "spy/slow", {"seconds": 30}))
    await asyncio.sleep(0.5)
    host.cancel(run.id)
    call, status = await asyncio.wait_for(pending, 5)
    assert status == 409 and call.status == "cancelled"
    assert run.outcome == "cancelled"


async def test_cancel_a_waiting_run(host):
    runs = [host.create_run(f"r{i}", ["spy/echo"]) for i in range(DESK_LIMIT + 1)]
    host.cancel(runs[-1].id)
    assert runs[-1].state == "done" and runs[-1].outcome == "cancelled"
    with pytest.raises(HostError):
        host.cancel(runs[-1].id)


async def test_run_times_out(host, fast_timeout):
    run = host.create_run("r", ["spy/echo"])
    await asyncio.wait_for(run.stopped.wait(), 3)
    assert run.outcome == "failed" and "timed out" in run.reason


async def test_bad_command_is_not_connected(host):
    before = len(host.servers)
    with pytest.raises(HostError) as e:
        await host.add_server("python -c 'print(42)'")
    assert "42" in str(e.value) or "exited" in str(e.value)
    assert len(host.servers) == before


async def test_restart_keeps_the_board(tmp_path, spy_log):
    db = tmp_path / "keep.sqlite"
    h = Host(db)
    await h.start()
    await h.add_server("python tests/spy_server.py", "spy")
    done = h.create_run("done", ["spy/echo"])
    await h.call(done.id, "spy/echo", {"text": "kept"})
    h.finish(done.id)
    live = h.create_run("live", ["spy/echo"])
    await h.stop()

    h2 = Host(db)
    await h2.start()
    try:
        assert h2.runs[done.id].outcome == "completed"
        assert h2.runs[done.id].calls[0].public()["text"] == "kept"
        assert h2.runs[live.id].outcome == "failed"
        for _ in range(50):  # added servers reconnect in the background
            if "spy/echo" in h2.offered_tools():
                break
            await asyncio.sleep(0.1)
        assert "spy/echo" in h2.offered_tools()
    finally:
        await h2.stop()


async def test_call_limit_rejects_calls_past_the_budget(host, spy_log):
    run = host.create_run("r", ["spy/echo"], max_calls=2)
    await host.call(run.id, "spy/secret", {"text": "rejected calls don't count"})
    for text in ("one", "two"):
        _, status = await host.call(run.id, "spy/echo", {"text": text})
        assert status == 200
    call, status = await host.call(run.id, "spy/echo", {"text": "three"})
    assert status == 429 and call.status == "rejected" and "all 2" in call.error
    assert [c["arguments"]["text"] for c in spy_log()] == ["one", "two"]
    assert run.public()["used_calls"] == 2


async def test_run_picks_its_own_timeout(host):
    run = host.create_run("r", ["spy/echo"], timeout=1)
    assert run.public()["deadline"] == pytest.approx(run.started_at + 1)
    await asyncio.wait_for(run.stopped.wait(), 3)
    assert run.outcome == "failed" and "after 1 s" in run.reason


async def test_limits_are_checked(host):
    with pytest.raises(HostError):
        host.create_run("r", ["spy/echo"], max_calls=0)
    with pytest.raises(HostError):
        host.create_run("r", ["spy/echo"], timeout=10_000)


async def test_limits_survive_a_restart(tmp_path, spy_log):
    db = tmp_path / "limits.sqlite"
    h = Host(db)
    await h.start()
    run = h.create_run("r", ["notes/list_notes"], max_calls=3, timeout=30)
    h.finish(run.id)
    await h.stop()
    h2 = Host(db)
    await h2.start()
    try:
        assert (h2.runs[run.id].max_calls, h2.runs[run.id].timeout) == (3, 30)
    finally:
        await h2.stop()
