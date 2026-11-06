from vln_cli.presentation import arguments, call_duration, call_text, describe, input_text, without_images


def test_nested_tool_results_preserve_fields_and_hide_images_without_mutating_source():
    result = {"ok": True, "feedback": {"distance_remaining": .52}, "custom": [
        {"type": "input_image", "image_url": "data:image/jpeg;base64,SECRET"}, {"message": "门口"}]}
    text = call_text({"call_id": "c", "name": "custom_tool", "state": "finished", "result": result})
    assert "distance_remaining：0.52" in text and "门口" in text
    assert "SECRET" not in text and "input_image" not in text
    assert result["custom"][0]["image_url"].endswith("SECRET")
    assert "调用参数" in call_text({"call_id": "c", "name": "capture_image", "arguments": "{}"})
    assert arguments('{"position":{"x":1}}') == "position：\n  x：1"


def test_final_duration_uses_required_native_run_ms():
    assert call_duration({"state": "finished", "run_ms": 0}) == "0 毫秒"
    assert call_duration({"state": "finished", "run_ms": 20}) == "20 毫秒"
    assert call_duration({"state": "waiting", "wait_ms": 50, "run_ms": 0}) == "50 毫秒"



def test_request_inputs_are_readable_and_do_not_render_image_content():
    text = input_text([{"role": "user", "content": [
        {"type": "input_text", "text": '{"instruction":"拍照"}'},
        {"type": "input_image", "image_url": "data:image/jpeg;base64,SECRET"}]}])
    assert "指令：拍照" in text and "SECRET" not in text



def test_tool_names_remain_native_and_description_comes_from_definition():
    from vln_cli.state import ViewState
    state = ViewState()
    state.apply({"type": "interaction_update", "time_stamp": "2026-10-29T08:00:00.000Z", "batch_id": 1,
        "tools": {"definitions": [{"name": "custom_sensor", "description": "来自 tools 的实时描述"}]},
        "response": {"function_calls": [{"call_id": "c1", "name": "custom_sensor", "arguments": "{}"}]}})
    text = next(iter(state.messages.values())).text
    assert "custom_sensor" in text and "来自 tools 的实时描述" in text
    state.apply({"type": "interaction_update", "time_stamp": "2026-10-29T08:00:01.000Z", "batch_id": 2,
        "tools": {"definitions": [{"name": "custom_sensor", "description": "后续更新的描述"}]}})
    assert "后续更新的描述" in next(iter(state.messages.values())).text



def test_arbitrary_tool_fields_and_values_remain_native_at_every_depth():
    value = {"frame_id": "camera", "ok": True, "state": "running", "mode": "server",
             "plugin_new_field": {"position": {"width": 1.23456789012345},
                                  "status": "failed", "optional": None, "items": [False, None]},
             "empty_list": [], "empty_object": {}}
    text = arguments(__import__('json').dumps(value))
    for expected in ["frame_id：camera", "ok：true", "state：running", "mode：server",
                     "plugin_new_field：", "position：", "width：1.23456789012345",
                     "status：failed", "optional：null", "false", "null", "empty_list：[]", "empty_object：{}"]:
        assert expected in text
    result = call_text({"call_id": "c", "name": "arbitrary", "state": "finished", "result": value})
    assert "已完成" in result and "state：running" in result
    assert "是否成功" not in result and "图像宽度" not in result


def test_final_zero_duration_and_separate_wait_time_and_error_source():
    call = {"call_id": "c", "name": "capture_image", "state": "failed",
            "run_ms": 0, "wait_ms": 35, "code": "TOOL_QUEUE_FULL",
            "message": "队列已满"}
    assert call_duration(call) == "0 毫秒"
    detail = call_text(call, detail=True)
    for expected in ("错误来源：ToolManager / capture_image", "TOOL_QUEUE_FULL", "队列已满",
                     "执行耗时：0 毫秒", "排队耗时：35 毫秒", "call_id：c"):
        assert expected in detail
    import pytest
    with pytest.raises(KeyError, match="run_ms"):
        call_duration({"state": "finished"})
