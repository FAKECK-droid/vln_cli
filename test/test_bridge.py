from copy import deepcopy

from vln_cli.bridge import replaceable_telemetry


def snapshot(batch=1):
    return {"type": "interaction_update", "batch_id": batch,
            "request": None, "session": {"request_status": {
                "state": "idle", "request_elapsed_ms": batch}, "context_status": {}},
            "tools": {"queue": {"waiting": [], "running": []}, "definitions": []},
            "response": {"messages": [], "stream": {"response_id": "", "state": "idle"}}}


def test_only_redundant_telemetry_can_be_coalesced():
    before, after = snapshot(), snapshot(2)
    assert replaceable_telemetry(before, after)
    for section, key, value in [
        ("response", "function_calls", [{"call_id": "c1"}]),
        ("response", "tool_outputs", [{"call_id": "c1"}]),
        ("response", "id", "r1"),
        ("tools", "finished_calls", [{"call_id": "c1", "state": "finished"}]),
    ]:
        changed = deepcopy(after)
        changed[section][key] = value
        assert not replaceable_telemetry(before, changed)
        assert not replaceable_telemetry(changed, snapshot(3))


def test_request_state_and_tool_boundaries_survive_coalescing():
    before = snapshot()
    changed = snapshot(2)
    changed["request"] = {"model": "m"}
    assert not replaceable_telemetry(before, changed)
    changed = snapshot(2)
    changed["session"]["request_status"]["state"] = "generating"
    assert not replaceable_telemetry(before, changed)
    changed = snapshot(2)
    changed["tools"]["queue"]["running"] = [{"call_id": "c1", "run_ms": 10}]
    assert not replaceable_telemetry(before, changed)
    later = deepcopy(changed)
    later["batch_id"] = 3
    later["tools"]["queue"]["running"][0]["run_ms"] = 20
    assert replaceable_telemetry(changed, later)
    assert not replaceable_telemetry(later, changed)
    # 同参数的下一轮耗时回退也必须保留，不能掩盖请求边界。
    later["session"]["request_status"]["request_elapsed_ms"] = 0
    assert not replaceable_telemetry(changed, later)


def test_decode_and_protocol_errors_keep_topic_component_and_exception():
    import threading
    from collections import deque
    from types import SimpleNamespace
    from vln_cli.bridge import RosBridge
    from vln_cli.state import ViewState
    bridge = RosBridge.__new__(RosBridge)
    bridge._lock = threading.Lock()
    bridge._incoming, bridge._errors = deque(), deque()
    bridge._received, bridge._coalesced = 0, 0
    bridge.output_topic = "/test/output"
    bridge._receive(SimpleNamespace(data="{"))
    bridge._receive(SimpleNamespace(data='{"type":"wrong","batch_id":2}'))
    errors = []
    bridge.poll(ViewState().apply, errors.append)
    assert len(errors) == 2
    assert any("vln_cli.RosBridge" in e and "/test/output" in e and "JSONDecodeError" in e for e in errors)
    assert any("vln_cli.ViewState" in e and "batch_id=2" in e and "ValueError" in e for e in errors)
