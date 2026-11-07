"""统一业务字段、图片元数据及请求展示的回归测试。"""
from copy import deepcopy
import json

import pytest

from vln_cli.presentation import call_text, input_text, request_text, without_images
from vln_cli.state import ViewState, result_payload, user_input

STAMP = "2026-11-07T02:30:00.125Z"
POSE = {"frame_id": "map", "x": 1.2, "y": 2.3, "yaw": .5,
        "time_stamp": "2026-11-07T02:30:00.080Z"}
IMAGES = [
    {"time_stamp": "2026-11-07T02:30:00.080Z", "pose": POSE,
     "metadata": {"costmap": "local", "image_width": 592, "image_height": 660,
                  "meters_per_pixel": .05, "map_rect": {"x": 40, "y": 60, "width": 512, "height": 512}}},
    {"time_stamp": "2026-11-07T02:30:00.085Z",
     "metadata": {"frame_id": "camera_color_optical_frame", "pair_id": 12, "queue_index": 0,
                  "custom": {"nullable": None, "flags": [True, False]}}},
]


def native_content(payload, omitted=False):
    return [{"type": "input_text", "text": json.dumps(payload, ensure_ascii=False)},
            *[{"type": "input_image", "omitted": True} if omitted else
              {"type": "input_image", "image_url": f"data:image/png;base64,SECRET_{i}"}
              for i, _ in enumerate(payload.get("images", []))]]


@pytest.mark.parametrize("omitted", [False, True])
def test_tool_card_and_request_keep_data_and_ordered_image_metadata_without_image_bytes(omitted):
    payload = {"source": "robot", "time_stamp": STAMP, "ok": True,
               "pose": POSE, "data": {"environment_id": "env_12"}, "images": IMAGES}
    output = {"type": "function_call_output", "call_id": "surroundings",
              "output": native_content(payload, omitted)}
    packet = {"type": "interaction_update", "time_stamp": STAMP, "batch_id": 1,
              "tools": {"finished_calls": [{
                  "call_id": "surroundings", "name": "get_surroundings", "sequence": 1,
                  "blocking": False, "state": "finished", "wait_ms": 0, "run_ms": 8}]},
              "response": {"tool_outputs": [output]}}
    original = deepcopy(packet)
    state = ViewState()
    state.apply(packet)
    assert result_payload(output) == payload
    assert state.calls["surroundings"]["result"] == payload
    assert state.snapshot()["tools"]["calls"][0]["result"]["images"] == IMAGES
    card = call_text(state.calls["surroundings"], detail=True)
    request = input_text([output])
    for text in (card, request):
        for expected in ("data：", "environment_id：env_12", "images：", "metadata：",
                         "map_rect：", "nullable：null", "执行耗时：8 毫秒" if text == card else "工具结果"):
            assert expected in text
        assert text.index("costmap：local") < text.index("pair_id：12")
        assert "SECRET" not in text and "input_image" not in text
    assert packet == original


def test_image_filter_preserves_metadata_and_only_removes_image_content():
    business = {"source": "robot", "time_stamp": STAMP, "data": {}, "images": IMAGES}
    content = native_content(business)
    assert without_images(business)["images"] == IMAGES
    assert without_images(content) == [content[0]]
    assert len(content) == 3  # 不修改原生输入或图片描述。


def test_machine_event_is_distinguished_from_user_input_and_keeps_nested_payload():
    event = {"source": "robot", "time_stamp": STAMP, "origin": "ReportNavigationCompleted",
             "event": "navigation_completed", "pose": POSE,
             "data": {"task_id": "nav_12", "message": "已到达目标位置"}, "images": IMAGES}
    event_input = {"role": "user", "content": native_content(event, omitted=True)}
    user = user_input("检查门口", pose=POSE)[0]
    user_payload = json.loads(user["content"][0]["text"])
    assert user_payload["data"] == {}
    assert user_payload["instruction"] == "检查门口"
    assert not {"event", "origin", "text", "images"} & user_payload.keys()
    rendered = input_text([user, event_input])
    for expected in ("用户：", "机器事件：", "navigation_completed", "ReportNavigationCompleted",
                     "业务载荷：", "task_id：nav_12", "已到达目标位置", "图片元数据："):
        assert expected in rendered
    assert "SECRET" not in rendered


def test_request_history_shows_machine_event_and_complete_tool_payload():
    event = {"role": "user", "content": native_content({
        "source": "robot", "time_stamp": STAMP, "origin": "ReportNavigationFailure",
        "event": "navigation_failed", "data": {"message": "路径被阻挡", "task_id": "nav_13"}})}
    result = {"type": "function_call_output", "call_id": "nav-feedback",
              "output": native_content({
                  "source": "robot", "time_stamp": STAMP, "ok": False,
                  "code": "NAVIGATION_FAILED", "message": "导航失败",
                  "pose": POSE, "data": {"task_id": "nav_13", "state": "failed"}})}
    record = {"request": {"model": "test", "input": [event, result]},
              "sequence": 1, "state": "finished", "time_stamp": STAMP,
              "elapsed_ms": 10, "elapsed_final": True}
    detail = request_text(record)
    for expected in ("机器事件", "ReportNavigationFailure", "路径被阻挡",
                     "工具结果 · nav-feedback", "code：NAVIGATION_FAILED",
                     "message：导航失败", "data：", "task_id：nav_13", "state：failed"):
        assert expected in detail
