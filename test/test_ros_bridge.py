import json
import time

import pytest

from vln_cli.bridge import RosBridge
from vln_cli.state import user_input


def test_real_ros_topics_without_agent_or_network(monkeypatch):
    pytest.importorskip("rclpy")
    from rclpy.node import Node
    from std_msgs.msg import String

    monkeypatch.setenv("ROS_DOMAIN_ID", "181")
    monkeypatch.setenv("ROS_LOCALHOST_ONLY", "1")
    bridge = RosBridge("/cli_test/input", "/cli_test/output", 5)
    peer = Node("vln_cli_test_peer", context=bridge.context)
    bridge.executor.add_node(peer)
    received = []
    outgoing = []
    errors = []
    subscription = peer.create_subscription(
        String, "/cli_test/input", lambda message: received.append(json.loads(message.data)), 10)
    publisher = peer.create_publisher(String, "/cli_test/output", 10)
    try:
        deadline = time.monotonic() + 4
        while not bridge.publisher.get_subscription_count() or not publisher.get_subscription_count():
            bridge.poll(outgoing.append, errors.append)
            assert time.monotonic() < deadline
        inputs = user_input("只测试 ROS 序列化，不调用模型")
        bridge.send(inputs)
        packet = {"type": "interaction_update", "batch_id": 1,
                  "time_stamp": "2026-10-28T06:00:00.000Z", "full": True}
        publisher.publish(String(data=json.dumps(packet)))
        deadline = time.monotonic() + 4
        while not received or not outgoing:
            bridge.poll(outgoing.append, errors.append)
            assert time.monotonic() < deadline
        assert received == [inputs] and outgoing == [packet] and not errors
        assert bridge.connection()["input_subscribers"] == 1
    finally:
        bridge.executor.remove_node(peer)
        peer.destroy_node()
        bridge.close()
        bridge.close()



def test_cli_reconnects_to_existing_peer_and_accepts_periodic_full_state(monkeypatch):
    pytest.importorskip("rclpy")
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node
    from std_msgs.msg import String
    from vln_cli.state import ViewState, utc_now

    monkeypatch.setenv("ROS_DOMAIN_ID", "182")
    monkeypatch.setenv("ROS_LOCALHOST_ONLY", "1")
    peer_context = Context()
    peer_context.init(args=[])
    peer = Node("reconnect_test_peer", context=peer_context)
    executor = SingleThreadedExecutor(context=peer_context)
    executor.add_node(peer)
    received = []
    subscription = peer.create_subscription(String, "/cli_reconnect/input",
        lambda message: received.append(json.loads(message.data)), 10)
    publisher = peer.create_publisher(String, "/cli_reconnect/output", 10)
    try:
        for generation in range(2):
            bridge = RosBridge("/cli_reconnect/input", "/cli_reconnect/output", 5)
            state, errors = ViewState(), []
            try:
                deadline = time.monotonic() + 8
                batch = 10
                while state.packet_count < 3:
                    executor.spin_once(timeout_sec=0)
                    bridge.poll(state.apply, errors.append)
                    if publisher.get_subscription_count():
                        publisher.publish(String(data=json.dumps({
                            "type": "interaction_update", "time_stamp": utc_now(), "batch_id": batch,
                            "full": False, "request": {"model": "local-test", "input": []},
                            "session": {"request_status": {"state": "generating", "request_elapsed_ms": batch}},
                            "tools": {"definitions": [{"name": "capture_image"}], "queue": {
                                "waiting": [], "running": [{"call_id": "one-call", "name": "capture_image"}]}}})))
                        batch += 1
                    assert time.monotonic() < deadline
                    time.sleep(.01)
                assert bridge.connection()["input_subscribers"] == 1
                assert bridge.connection()["output_publishers"] == 1
                assert len(state.requests) == len(state.calls) == 1 and state.definitions_received
                assert len([m for m in state.messages.values() if m.role == "tool"]) == 1
                assert not errors
                bridge.send(user_input(f"synthetic input {generation}"))
                while len(received) < generation + 1:
                    executor.spin_once(timeout_sec=.01)
                    assert time.monotonic() < deadline
            finally:
                bridge.close()
    finally:
        executor.remove_node(peer)
        peer.destroy_node()
        executor.shutdown()
        peer_context.shutdown()



def test_background_receive_continues_while_ui_is_paused_and_keeps_tool_result(monkeypatch):
    pytest.importorskip("rclpy")
    from rclpy.node import Node
    from std_msgs.msg import String
    from test_bridge import snapshot
    from vln_cli.state import ViewState

    monkeypatch.setenv("ROS_DOMAIN_ID", "184")
    monkeypatch.setenv("ROS_LOCALHOST_ONLY", "1")
    bridge = RosBridge("/background/input", "/background/output", 20)
    peer = Node("background_test_peer", context=bridge.context)
    bridge.executor.add_node(peer)
    publisher = peer.create_publisher(String, "/background/output", 10)
    try:
        deadline = time.monotonic() + 6
        while not publisher.get_subscription_count():
            assert time.monotonic() < deadline
            time.sleep(.01)
        # 完全不调用 poll，模拟界面绘制或终端输出繁忙。
        for batch in range(1, 51):
            packet = snapshot(batch)
            packet["time_stamp"] = f"2026-10-29T09:00:00.{batch:03d}Z"
            if batch == 20:
                packet["response"]["tool_outputs"] = [{"call_id": "c1", "output": [
                    {"type": "input_text", "text": '{"ok":true}'}]}]
            publisher.publish(String(data=json.dumps(packet)))
            time.sleep(.004)
        while bridge.connection()["received_packets"] < 50:
            assert time.monotonic() < deadline
            time.sleep(.01)
        connection = bridge.connection()
        assert connection["receiver_running"]
        assert connection["coalesced_updates"] >= 40
        assert connection["pending_updates"] <= 4
        state, errors = ViewState(), []
        bridge.poll(state.apply, errors.append)
        assert state.last_batch == 50 and state.calls["c1"]["state"] == "finished"
        assert not errors
    finally:
        bridge.executor.remove_node(peer)
        peer.destroy_node()
        bridge.close()
        assert not bridge._thread.is_alive()
