import json
from datetime import datetime, timedelta

import pytest
from textual.widgets import DataTable, Input, Static, TabbedContent
from rich.text import Text

from vln_cli.app import VLNConsole
from vln_cli.bridge import DemoBridge, ReplayBridge


@pytest.mark.asyncio
async def test_chinese_input_streaming_and_local_model_command():
    bridge = DemoBridge()
    app = VLNConsole(bridge)
    async with app.run_test(size=(148, 46)) as pilot:
        await pilot.pause(.2)
        input_widget = app.query_one("#composer", Input)
        input_widget.value = "检查门口"
        await pilot.press("enter")
        await pilot.pause(.3)
        messages = list(app.state.messages.values())
        assert any(message.role == "user" and message.text == "检查门口" for message in messages)
        assert any(message.role == "assistant" and "演示输入：检查门口" in message.text
                   for message in messages)
        turns = bridge._turn
        await app.execute("/model")
        assert bridge._turn == turns
        assert "gpt-6.1-terra" in list(app.state.messages.values())[-1].text
        await app.execute("/model another")
        assert "只查看" in list(app.state.messages.values())[-1].text


@pytest.mark.asyncio
async def test_panels_history_and_narrow_resize():
    app = VLNConsole(DemoBridge())
    async with app.run_test(size=(148, 46)) as pilot:
        await pilot.pause(.2)
        await app.execute("/tools")
        await pilot.pause(.1)
        assert app.query_one(TabbedContent).active == "tools-tab"
        await app.execute("/request")
        assert app.query_one(TabbedContent).active == "request-tab"
        composer = app.query_one("#composer", Input)
        composer.value = "/topics"
        await pilot.press("enter")
        await pilot.press("up")
        assert composer.value == "/topics"
        await pilot.press("down")
        assert composer.value == ""
        app.sidebar_hidden = None
        await pilot.resize_terminal(78, 28)
        await pilot.pause(.1)
        assert app.has_class("compact")
        await pilot.press("ctrl+b")
        assert not app.has_class("compact")
        await app.execute("/clear")
        assert len(app.state.messages) == 1
        assert app.state.model_info["model"] == "gpt-6.1-terra"


@pytest.mark.asyncio
async def test_export_and_readonly_replay(tmp_path):
    app = VLNConsole(DemoBridge())
    path = tmp_path / "export.json"
    async with app.run_test(size=(120, 38)) as pilot:
        await pilot.pause(.2)
        await app.execute("/export " + str(path))
        exported = json.loads(path.read_text())
        assert exported["model"]["model"] == "gpt-6.1-terra"
        assert exported["messages"]
        assert "raw_history" not in exported
    replay_file = tmp_path / "replay.json"
    replay_file.write_text(json.dumps({"packets": []}))
    app = VLNConsole(ReplayBridge(replay_file))
    async with app.run_test() as pilot:
        await app.execute("不要发送这条消息")
        assert list(app.state.messages.values())[-1].role == "error"


@pytest.mark.asyncio
async def test_late_join_does_not_invent_model():
    path_bridge = ReplayBridge.__new__(ReplayBridge)
    from collections import deque
    path_bridge._packets = deque()
    path_bridge.history_limit = 200
    app = VLNConsole(path_bridge)
    async with app.run_test() as pilot:
        await app.execute("/model")
        assert "尚未收到模型信息" in list(app.state.messages.values())[-1].text


@pytest.mark.asyncio
async def test_chinese_history_panels_double_click_without_json_recording():
    app = VLNConsole(DemoBridge())
    async with app.run_test(size=(170, 52)) as pilot:
        await pilot.pause(.15)
        baseline = datetime.fromisoformat(app.state.last_stamp.replace("Z", "+00:00"))
        # 两个真实请求边界；空闲时历史仍可选择。
        for batch, model in [(30, "first-model"), (32, "second-model")]:
            stamp = (baseline + timedelta(seconds=batch)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            app.state.apply({"type": "interaction_update", "time_stamp": stamp,
                "batch_id": batch, "request": {"model": model, "input": [{"role": "user", "content": [
                    {"type": "input_text", "text": '{"instruction":"检查门口"}'}]}]},
                "session": {"request_status": {"state": "generating", "request_elapsed_ms": 1}}})
            app.state.apply({"type": "interaction_update", "time_stamp": (baseline + timedelta(seconds=batch, milliseconds=500)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "batch_id": batch + 1, "request": None,
                "session": {"request_status": {"state": "idle", "request_elapsed_ms": 500}},
                "response": {"id": f"r{batch}", "status": "completed"}})
        await app.execute("/request")
        await pilot.pause(.1)
        table = app.query_one("#request-table", DataTable)
        assert table.row_count == 3  # 演示欢迎请求 + 两次观察到的请求。
        # 首次点击移动，第二次点击选择：原生 DataTable 的双击语义。
        await pilot.click("#request-table", offset=(8, 2), times=2)
        await pilot.pause(.1)
        assert app.selected_request is not None
        content = app.query_one("#request-detail", Static).content
        assert isinstance(content, Text) and "模型与请求配置" in content.plain
        table.move_cursor(row=table.row_count - 1)
        table.focus()
        await pilot.press("enter")
        await pilot.pause(.1)
        assert "second-model" in app.query_one("#request-detail", Static).content.plain
        await app.execute("/tools")
        await pilot.pause(.1)
        await pilot.click("#call-table", offset=(4, 1), times=2)
        await pilot.pause(.1)
        assert app.selected_call == "demo-camera"
        detail = app.query_one("#call-detail", Static).content
        assert isinstance(detail, Text) and "capture_image" in detail.plain and "获取相机当前画面" in detail.plain and "调用标识" in detail.plain
        await app.execute("/session")
        assert isinstance(app.query_one("#session-detail", Static).content, Text)
        assert not app.query("#json-tab")
        assert not hasattr(app.state, "raw_history")
        # 查看历史和状态不向 Agent 发送消息。
        assert app.bridge._turn == 0


@pytest.mark.asyncio
async def test_tool_chat_card_updates_in_place_with_text_result_only():
    app = VLNConsole(DemoBridge())
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause(.15)
        baseline = datetime.fromisoformat(app.state.last_stamp.replace("Z", "+00:00"))
        stamp = (baseline + timedelta(seconds=10)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        app.state.apply({"type": "interaction_update", "time_stamp": stamp,
            "batch_id": 10, "response": {"function_calls": [
                {"call_id": "actual-camera", "name": "capture_image", "arguments": "{}"}]}})
        await app.refresh_tick()
        key = "tool-actual-camera"
        bubble = app.bubbles[key]
        assert isinstance(bubble.body, Static)
        assert "正在执行" in bubble.body.content.plain
        for batch in range(11, 14):
            stamp = (baseline + timedelta(seconds=batch)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            app.state.apply({"type": "interaction_update", "time_stamp": stamp,
                "batch_id": batch, "tools": {"finished_calls": [
                    {"call_id": "actual-camera", "name": "capture_image", "state": "finished",
                     "sequence": 0, "blocking": False, "wait_ms": 2, "run_ms": 12}]},
                "response": {"tool_outputs": [{"call_id": "actual-camera", "output": [
                    {"type": "input_text", "text": '{"source":"robot","time_stamp":"2026-11-07T02:30:00.125Z","data":{"width":1280,"height":720},"ok":true,"images":[{"metadata":{"test_index":0}}]}'},
                    {"type": "input_image", "image_url": "data:image/jpeg;base64,SECRET"}]}]}})
            await app.refresh_tick()
        assert app.bubbles[key] is bubble
        assert "已完成" in bubble.body.content.plain
        assert "width：1280" in bubble.body.content.plain
        assert "SECRET" not in bubble.body.content.plain
        assert len([m for m in app.state.messages.values() if m.call_id == "actual-camera"]) == 1
        await app.execute("/model")
        await app.execute("/topics")
        assert all("```json" not in m.text for m in app.state.messages.values())



@pytest.mark.asyncio
async def test_sent_message_is_distinguished_from_agent_started_response():
    from collections import deque
    class WaitingBridge:
        history_limit = 20
        def __init__(self):
            self.inputs = None
            self.packets = deque()
        def connection(self):
            return {"mode": "ROS 2", "input_subscribers": 1, "output_publishers": 1,
                    "input_topic": "/test/input", "output_topic": "/test/output"}
        def poll(self, receive, error):
            while self.packets:
                receive(self.packets.popleft())
        def current_pose(self):
            return None
        def send(self, inputs):
            self.inputs = inputs
        def close(self):
            pass
    bridge = WaitingBridge()
    app = VLNConsole(bridge)
    async with app.run_test(size=(150, 46)) as pilot:
        from vln_cli.state import utc_now
        bridge.packets.append({"type": "interaction_update", "time_stamp": utc_now(), "batch_id": 1,
                               "request": None, "session": {"request_status": {"state": "idle"}}})
        await app.refresh_tick()
        await app.execute("下一条消息")
        await app.refresh_tick()
        assert "已发送，等待 Agent 处理" in app.query_one("#ribbon", Static).content.plain
        bridge.packets.append({"type": "interaction_update", "time_stamp": utc_now(), "batch_id": 2,
            "request": {"model": "m", "input": bridge.inputs},
            "session": {"request_status": {"state": "generating", "in_flight": True}}})
        await app.refresh_tick()
        assert app._pending_user is None
        assert "等待模型响应" in app.query_one("#ribbon", Static).content.plain



@pytest.mark.asyncio
async def test_first_complete_ros_match_sends_one_connection_notice_even_after_reconnect():
    class ConnectionBridge:
        history_limit = 20
        def __init__(self):
            self.input_count = self.output_count = 0
            self.sent = []
            self.pose = {"frame_id": "map", "x": 1.2, "y": 2.3, "yaw": .5}
        def connection(self):
            return {"mode": "ROS 2", "input_subscribers": self.input_count,
                    "output_publishers": self.output_count,
                    "input_topic": "/test/input", "output_topic": "/test/output"}
        def poll(self, receive, error):
            pass
        def current_pose(self):
            return self.pose
        def send(self, inputs):
            self.sent.append(inputs)
        def close(self):
            pass
    bridge = ConnectionBridge()
    app = VLNConsole(bridge)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(.1)
        assert not bridge.sent
        bridge.input_count = 1
        await app.refresh_tick()
        assert not bridge.sent
        bridge.output_count = 1
        await app.refresh_tick()
        assert len(bridge.sent) == 1
        payload = json.loads(bridge.sent[0][0]["content"][0]["text"])
        assert payload["instruction"] == "CLI 已连接成功。" and payload["source"] == "user"
        assert payload["pose"] == bridge.pose
        assert payload["data"] == {} and "origin" not in payload and "event" not in payload
        assert any(m.role == "system" and m.text == "CLI 已连接成功。" for m in app.state.messages.values())
        for _ in range(3):
            await app.refresh_tick()
        bridge.input_count = bridge.output_count = 0
        await app.refresh_tick()
        bridge.input_count = bridge.output_count = 1
        await app.refresh_tick()
        assert len(bridge.sent) == 1
        bridge.pose = {"frame_id": "map", "x": 4.0, "y": -2.0, "yaw": -.5}
        await app.execute("查询当前状态")
        payload = json.loads(bridge.sent[-1][0]["content"][0]["text"])
        assert payload["pose"] == bridge.pose
        assert payload["data"] == {} and "origin" not in payload and "event" not in payload
        assert len(bridge.sent) == 2
        bridge.pose = None
        await app.execute("没有 TF 也正常发送")
        assert json.loads(bridge.sent[-1][0]["content"][0]["text"])["pose"] is None


@pytest.mark.asyncio
async def test_both_remote_error_sources_are_visible_and_full_details_survive_banner_shortening():
    app = VLNConsole(DemoBridge())
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause(.1)
        app.state.request_status = {
            "last_error": {"code": "REQUEST_TIMEOUT", "message": "会话超时"},
            "transport": {"last_error": {"stage": "read", "message": "WebSocket disconnected"}}}
        await app.refresh_tick()
        banner = app.query_one("#error-banner", Static).content
        assert "SessionManager" in str(banner) and "ResponsesWsTransport" in str(banner)
        assert "REQUEST_TIMEOUT" in str(banner) and "WebSocket disconnected" in str(banner)
        await app.execute("/session")
        detail = app.query_one("#session-detail", Static).content.plain
        assert "session.request_status.transport.last_error" in detail
        assert "SessionManager" in detail and "ResponsesWsTransport" in detail


@pytest.mark.asyncio
async def test_current_business_schema_is_visible_in_tool_and_request_panels(tmp_path):
    from test_payloads import STAMP, IMAGES, POSE, native_content
    app = VLNConsole(DemoBridge())
    async with app.run_test(size=(170, 52)) as pilot:
        await pilot.pause(.2)
        baseline = datetime.fromisoformat(app.state.last_stamp.replace("Z", "+00:00"))
        stamp = (baseline + timedelta(seconds=10)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        payload = {"source": "robot", "time_stamp": STAMP, "ok": True,
                   "data": {"environment_id": "env_12"}, "pose": POSE, "images": IMAGES}
        event = {"source": "robot", "time_stamp": STAMP, "origin": "ReportNavigationCompleted",
                 "event": "navigation_completed", "data": {"message": "已到达目标位置"}}
        output = {"type": "function_call_output", "call_id": "surroundings",
                  "output": native_content(payload, omitted=True)}
        app.state.apply({"type": "interaction_update", "time_stamp": stamp, "batch_id": 20,
                         "request": {"model": "test", "input": [
                             {"role": "user", "content": native_content(event)}, output]},
                         "tools": {"finished_calls": [{
                             "call_id": "surroundings", "name": "get_surroundings", "sequence": 1,
                             "blocking": False, "state": "finished", "wait_ms": 0, "run_ms": 8}]},
                         "response": {"tool_outputs": [output]}})
        await app.refresh_tick()
        await app.execute("/tools")
        app.selected_call = "surroundings"
        app._render_inspector()
        detail = app.query_one("#call-detail", Static).content.plain
        assert "environment_id：env_12" in detail and "metadata：" in detail
        assert detail.index("costmap：local") < detail.index("pair_id：12")
        await app.execute("/request")
        detail = app.query_one("#request-detail", Static).content.plain
        assert "机器事件：" in detail and "ReportNavigationCompleted" in detail
        assert "工具结果 · surroundings" in detail and "environment_id：env_12" in detail
        assert "SECRET" not in detail
        path = tmp_path / "current-schema.json"
        await app.execute("/export " + str(path))
        exported = json.loads(path.read_text())
        saved = next(call for call in exported["tools"]["calls"] if call["call_id"] == "surroundings")
        assert saved["result"] == payload
