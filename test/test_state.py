import json
import re

from vln_cli.state import ViewState, user_input


def packet(batch=1, **values):
    return {"type": "interaction_update", "time_stamp": f"2026-10-28T06:00:{batch:02d}.000Z",
            "batch_id": batch, **values}


def part(text, identity="r1", output=0):
    return {"response_id": identity, "output_index": output, "content_index": 0,
            "type": "output_text", "text": text}



def finished(identity, state="finished", **values):
    return {"call_id": identity, "name": "capture_image", "sequence": 0, "blocking": False,
            "state": state, "run_ms": 0, "wait_ms": 0,
            **values}


def test_user_input_is_native_and_has_utc_milliseconds():
    inputs = user_input("前往门口")
    assert inputs[0]["role"] == "user"
    payload = json.loads(inputs[0]["content"][0]["text"])
    assert payload["source"] == "user" and payload["instruction"] == "前往门口"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", payload["time_stamp"])
    assert payload["pose"] is None


def test_replace_fields_null_request_and_preserve_model_metadata():
    state = ViewState()
    state.apply(packet(request={"model": "gpt-6.1-terra", "input": ["large"]},
                       session={"request_status": {"state": "running", "in_flight": True}}))
    state.apply(packet(2, request=None))
    assert state.request is None
    assert state.model_info == {"model": "gpt-6.1-terra"}
    assert state.request_status["in_flight"]
    state.apply(packet(3, session={"request_status": {"state": "idle"}}))
    assert state.request_status == {"state": "idle"}


def test_whole_stream_text_replaces_previous_text_and_duplicate_batch():
    state = ViewState()
    first = packet(response={"messages": [part("你")]})
    assert state.apply(first)
    assert not state.apply(first)
    # batch_id 不连续是服务端正常的空轮询行为。
    state.apply(packet(4, response={"messages": [part("你好")]}))
    state.apply(packet(5, response={"id": "r1", "status": "completed",
                                   "messages": [part("你好！")]}))
    assert len(state.messages) == 1
    message = next(iter(state.messages.values()))
    assert message.text == "你好！" and message.state == "complete"
    assert state.completed_count == 1
    assert not state.apply(packet(2, response={"messages": [part("旧数据")]}))


def test_restart_keeps_history_and_marks_unfinished_work_interrupted():
    state = ViewState()
    state.apply(packet(8, request={"model": "old"},
                       tools={"definitions": [{"name": "old"}], "queue": {
                           "waiting": [], "running": [{"call_id": "old-call", "name": "camera"}]}},
                       response={"messages": [part("旧会话")]}))
    restarted = packet(1, request=None, response={"messages": [part("新会话")]})
    restarted["time_stamp"] = "2026-10-28T06:01:00.000Z"
    state.apply(restarted)
    assert state.calls["old-call"]["state"] == "interrupted"
    assert state.model_info == {} and state.definitions == []
    assert [m.text for m in state.messages.values() if m.role == "assistant"] == ["旧会话", "新会话"]
    assert next(iter(state.requests.values()))["state"] == "interrupted"


def test_running_snapshot_wins_over_call_submission_and_results_decode():
    state = ViewState()
    state.apply(packet(tools={"queue": {"waiting": [], "running": [
        {"call_id": "c1", "name": "capture_image", "run_ms": 20}]}},
        response={"function_calls": [{"call_id": "c1", "name": "capture_image", "arguments": "{}"}]}))
    assert state.calls["c1"]["state"] == "running"
    state.apply(packet(2, tools={"queue": {"waiting": [], "running": []},
                                "finished_calls": [finished("c1", state="failed")]},
        response={"tool_outputs": [{"type": "function_call_output", "call_id": "c1",
                                   "output": [{"type": "input_text", "text": '{"ok": false}'}]}]}))
    assert state.calls["c1"]["state"] == "failed"
    assert state.calls["c1"]["result"] == {"ok": False}


def test_history_pruning_does_not_drop_active_calls():
    state = ViewState(history_limit=2)
    for i in range(10):
        state.local_message("user", str(i))
    assert [message.text for message in state.messages.values()] == ["8", "9"]
    state.apply(packet(tools={"queue": {
        "waiting": [], "running": [{"call_id": "active", "name": "camera"}]}}))
    for i in range(2, 10):
        state.apply(packet(i, tools={"finished_calls": [finished(str(i))]}))
    assert "active" in state.calls and len(state.calls) <= 3
    state.clear_chat()
    assert not state.messages and state.calls


def test_snapshot_contains_reconstructed_text_and_latest_state():
    state = ViewState()
    state.apply(packet(response={"messages": [part("完整文本")]}))
    exported = state.snapshot()
    assert exported["messages"][0]["text"] == "完整文本"
    assert exported["request"] is None


def test_old_full_baseline_cannot_roll_back_newer_state():
    state = ViewState()
    first = packet(request={"model": "old"})
    state.apply(first)
    state.apply(packet(2, request={"model": "new"}))
    assert not state.apply(first)
    assert state.model_info["model"] == "new"


def test_periodic_full_snapshots_keep_histories_and_do_not_duplicate_events():
    state = ViewState()
    call = {"call_id": "camera-1", "name": "capture_image", "arguments": "{}"}
    request = {"model": "test-model", "input": [{"role": "user", "content": "拍照"}]}
    for batch in range(1, 5):
        state.apply(packet(batch, request=request,
            session={"request_status": {"state": "generating", "request_elapsed_ms": batch * 10}},
            response={"function_calls": [call], "messages": [part("你好")]}))
    assert len(state.requests) == 1
    assert len(state.calls) == 1
    assert len([m for m in state.messages.values() if m.role == "tool"]) == 1
    assert next(m for m in state.messages.values() if m.role == "assistant").text == "你好"
    for batch in range(5, 9):
        state.apply(packet(batch, request=None,
            session={"request_status": {"state": "idle", "request_elapsed_ms": 70}},
            tools={"queue": {"waiting": [], "running": []},
                   "finished_calls": [finished("camera-1")]},
            response={"id": "r1", "status": "completed", "function_calls": [call],
                "messages": [part("你好")], "tool_outputs": [{
                    "call_id": "camera-1", "output": [{"type": "input_text", "text": '{"ok":true,"width":1280}'},
                    {"type": "input_image", "image_url": "data:image/jpeg;base64,PRIVATE_IMAGE"}]}]}))
    assert state.completed_count == 1
    assert len(state.requests) == 1 and next(iter(state.requests.values()))["state"] == "finished"
    tools = [m for m in state.messages.values() if m.role == "tool"]
    assert len(tools) == 1 and tools[0].state == "finished"
    assert "width：1280" in tools[0].text and "PRIVATE_IMAGE" not in tools[0].text
    assert not hasattr(state, "raw_history")


def test_empty_snapshot_arrays_do_not_erase_call_or_request_history():
    state = ViewState()
    state.apply(packet(request={"model": "first"}, response={"function_calls": [
        {"call_id": "c1", "name": "capture_image", "arguments": "{}"}]}))
    state.apply(packet(2, request=None, tools={"finished_calls": [finished("c1")]},
                       session={"request_status": {"state": "idle"}}))
    state.apply(packet(3, request=None,
        tools={"queue": {"waiting": [], "running": []}, "finished_calls": []},
        response={"id": None, "status": None, "function_calls": [], "tool_outputs": [], "messages": []}))
    assert len(state.requests) == 1 and state.calls["c1"]["state"] == "finished"


def test_identical_new_request_after_completion_is_a_separate_history_entry():
    state = ViewState()
    request = {"model": "same", "input": []}
    state.apply(packet(request=request, session={"request_status": {"request_elapsed_ms": 10, "in_flight": True}}))
    state.apply(packet(2, request=None, response={"id": "r1", "status": "completed"},
                       session={"request_status": {"state": "idle", "request_elapsed_ms": 500}}))
    state.apply(packet(3, request=request, session={"request_status": {
        "state": "generating", "request_elapsed_ms": 1, "in_flight": True, "current_response_id": ""}}))
    assert len(state.requests) == 2
    assert [r["state"] for r in state.requests.values()] == ["finished", "generating"]


def test_failed_tool_is_one_chat_entry_and_stale_queue_cannot_reopen_it():
    state = ViewState()
    state.apply(packet(response={"function_calls": [{"call_id": "c1", "name": "capture_image"}]}))
    message = next(iter(state.messages.values()))
    assert message.role == "tool" and "正在执行" in message.text
    state.apply(packet(2, tools={"finished_calls": [finished("c1", state="failed",
        code="IMAGE_TIMEOUT", message="等待首帧超时")]},
        response={"tool_outputs": [{"call_id": "c1", "output": [
        {"type": "input_text", "text": '{"ok":false,"code":"IMAGE_TIMEOUT","message":"等待首帧超时"}'}]}]}))
    state.apply(packet(3, tools={"queue": {"waiting": [], "running": [{"call_id": "c1"}]}}))
    assert len(state.messages) == 1
    assert message.state == "failed" and "等待首帧超时" in message.text


def test_snapshot_after_clear_does_not_recreate_old_chat():
    state = ViewState()
    response = {"id": "r1", "status": "completed", "messages": [part("旧文本")],
                "function_calls": [{"call_id": "c1", "name": "capture_image"}]}
    state.apply(packet(response=response))
    state.clear_chat()
    state.apply(packet(2, response=response))
    assert not state.messages
    assert state.calls and state.completed_count == 1


def test_request_history_is_bounded_and_received_payload_is_unchanged():
    state = ViewState(history_limit=2)
    for batch in range(1, 6):
        raw = packet(batch, request={"model": str(batch)},
                     session={"request_status": {"request_elapsed_ms": batch}})
        before = json.dumps(raw)
        state.apply(raw)
        assert json.dumps(raw) == before
    assert len(state.requests) == 2
    assert "raw_history" not in state.snapshot()
    assert [r["request"]["model"] for r in state.requests.values()] == ["4", "5"]


def test_late_join_full_replace_stream_marks_completed_text_without_final_event():
    state = ViewState()
    for batch in range(1, 5):
        state.apply(packet(batch, request=None, response={"id": None, "status": None,
            "stream": {"response_id": "r1", "state": "completed"},
            "messages": [part("已就绪")]}))
    assert len(state.messages) == 1 and state.completed_count == 1
    assert next(iter(state.messages.values())).state == "complete"



def test_clear_during_streaming_suppresses_repeated_snapshot_until_response_finishes():
    state = ViewState()
    state.apply(packet(response={"messages": [part("正在")]}))
    state.clear_chat()
    state.apply(packet(2, response={"messages": [part("正在生成")]}))
    assert not state.messages
    state.apply(packet(3, response={"id": "r1", "status": "completed", "messages": [part("已完成")]}))
    assert not state.messages



def test_periodic_telemetry_is_not_archived_or_deepcopied(monkeypatch):
    import vln_cli.state as state_module
    def forbidden_copy(value):
        raise AssertionError("普通遥测不应触发原始包深拷贝")
    monkeypatch.setattr(state_module, "deepcopy", forbidden_copy)
    state = ViewState()
    for batch in range(1, 30):
        state.apply(packet(batch, request=None,
            session={"request_status": {"state": "idle"}},
            tools={"definitions": [{"name": "capture_image", "description": "最新定义"}],
                   "queue": {"waiting": [], "running": []}}))
    assert not hasattr(state, "raw_history")
    assert not state.requests and not state.calls and not state.messages



def test_previous_response_completes_when_next_request_is_in_same_packet():
    state = ViewState()
    state.apply(packet(request={"model": "m", "input": []}, session={"request_status": {
        "state": "generating", "in_flight": True, "current_response_id": "r1", "request_elapsed_ms": 800}}))
    state.apply(packet(2, request={"model": "m", "previous_response_id": "r1", "input": [
        {"type": "function_call_output", "call_id": "c1", "output": "ok"}]},
        session={"request_status": {"state": "generating", "in_flight": True,
                                   "current_response_id": "r2", "request_elapsed_ms": 5},
                 "context_status": {"latest_response_id": "r1"}},
        response={"id": "r1", "status": "completed"}))
    first, second = state.requests.values()
    assert first["state"] == "finished" and first["response_id"] == "r1"
    assert first["elapsed_ms"] == 800  # 下一请求的 5ms 不能污染旧请求。
    assert second["state"] == "generating" and second["response_id"] == "r2"


def test_confirmed_chain_closes_missing_completion_and_old_records_stay_terminal():
    state = ViewState()
    state.apply(packet(request={"model": "m"}, session={"request_status": {
        "state": "generating", "current_response_id": "r1", "request_elapsed_ms": 300}}))
    state.apply(packet(2, request={"model": "m", "previous_response_id": "r1"},
        session={"request_status": {"state": "generating", "current_response_id": "r2", "request_elapsed_ms": 1}}))
    assert list(state.requests.values())[0]["state"] == "finished"
    state.apply(packet(3, request=None, session={"request_status": {"state": "idle",
        "current_response_id": "r2", "request_elapsed_ms": 400}}))
    assert [r["state"] for r in state.requests.values()] == ["finished", "finished"]


def test_missing_terminal_evidence_is_not_left_waiting_or_falsely_completed():
    state = ViewState()
    state.apply(packet(request={"model": "first"}, session={"request_status": {
        "state": "generating", "current_response_id": "r1", "request_elapsed_ms": 20}}))
    state.apply(packet(2, request={"model": "second"}, session={"request_status": {
        "state": "generating", "current_response_id": "r2", "request_elapsed_ms": 1}}))
    assert list(state.requests.values())[0]["state"] == "unobserved"


def test_input_pose_is_inside_business_json_and_keeps_native_fields():
    pose = {"frame_id": "map", "x": 1.2, "y": -2.3, "yaw": .5,
            "time_stamp": "2026-10-29T06:00:00.123Z"}
    item = user_input("检查门口", pose=pose)[0]
    assert set(item) == {"role", "content"}
    payload = json.loads(item["content"][0]["text"])
    assert payload["pose"] == pose
    assert payload["instruction"] == "检查门口"


def test_completion_timings_survive_stale_queue_and_fast_call_needs_no_active_sample():
    state = ViewState()
    terminal = {"call_id": "fast", "name": "capture_image", "state": "finished",
                "run_ms": 0, "wait_ms": 18,
                "sequence": 0, "blocking": False}
    state.apply(packet(tools={"finished_calls": [terminal]}, response={"tool_outputs": [
        {"call_id": "fast", "output": [{"type": "input_text", "text": '{"ok":true}'}]}]}))
    assert state.calls["fast"]["run_ms"] == 0
    state.apply(packet(2, tools={"queue": {"waiting": [], "running": [
        {"call_id": "fast", "run_ms": 999, "wait_ms": 999}]}}))
    assert state.calls["fast"]["state"] == "finished"
    assert state.calls["fast"]["run_ms"] == 0
    assert state.calls["fast"]["wait_ms"] == 18


def test_session_and_transport_errors_have_sources_and_both_are_kept_without_repetition():
    state = ViewState()
    status = {"last_error": {"code": "REQUEST_TIMEOUT", "message": "等待模型响应超时", "event": None},
              "transport": {"last_error": {"stage": "read", "message": "peer reset"}}}
    state.apply(packet(session={"request_status": status}))
    assert len(state.errors) == 2
    assert state.errors[0]["source"].startswith("SessionManager")
    assert state.errors[1]["source"].startswith("ResponsesWsTransport")
    messages = [m for m in state.messages.values() if m.role == "error"]
    assert len(messages) == 2
    assert "session.request_status.last_error" in messages[0].text
    assert "REQUEST_TIMEOUT" in messages[0].text and "等待模型响应超时" in messages[0].text
    assert "read" in messages[1].text and "peer reset" in messages[1].text
    state.apply(packet(2, session={"request_status": status}))
    assert len(state.messages) == 2
    state.apply(packet(3, session={"request_status": {"last_error": None, "transport": None}}))
    assert not state.errors
    state.apply(packet(4, session={"request_status": status}))
    assert len(state.messages) == 4


def test_failure_details_are_kept_in_request_history_after_recovery():
    state = ViewState()
    state.apply(packet(request={"model": "m"}, session={"request_status": {"state": "generating"}}))
    error = {"code": "INVALID_RESPONSE", "message": "missing output"}
    state.apply(packet(2, session={"request_status": {"last_error": error}}))
    state.apply(packet(3, request=None, session={"request_status": {"state": "idle", "last_error": None}}))
    record = next(iter(state.requests.values()))
    assert record["state"] == "failed" and record["error"] == error


def test_completion_requires_all_current_protocol_fields():
    import pytest
    state = ViewState()
    with pytest.raises(ValueError, match="tools.finished_calls 缺少字段"):
        state.apply(packet(tools={"finished_calls": [{"call_id": "c", "state": "finished"}]}))
