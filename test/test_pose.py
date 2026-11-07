"""仅本机隔离域的 TF/ROS 测试，不接触实际 Agent、模型或小车。"""
import json
import math
import re
import time

import pytest

from vln_cli.bridge import RosBridge
from vln_cli.state import user_input


@pytest.fixture
def bridge(monkeypatch):
    pytest.importorskip("rclpy")
    pytest.importorskip("tf2_ros")
    monkeypatch.setenv("ROS_DOMAIN_ID", "188")
    monkeypatch.setenv("ROS_LOCALHOST_ONLY", "1")
    result = RosBridge("/pose_test/input", "/pose_test/output", 5)
    try:
        yield result
    finally:
        result.close()


def transform(parent, child, stamp, x=0., y=0., yaw=0.):
    from geometry_msgs.msg import TransformStamped
    result = TransformStamped()
    result.header.frame_id, result.child_frame_id = parent, child
    result.header.stamp = stamp
    result.transform.translation.x, result.transform.translation.y = x, y
    result.transform.rotation.z = math.sin(yaw / 2)
    result.transform.rotation.w = math.cos(yaw / 2)
    return result


def test_missing_and_stale_tf_return_unknown_without_waiting(bridge):
    from rclpy.time import Time
    started = time.monotonic()
    assert bridge.current_pose() is None
    assert time.monotonic() - started < .5
    assert bridge.current_pose() is None
    errors = []
    bridge.poll(lambda packet: None, errors.append)
    assert len(errors) == 1 and "pose: null" in errors[0]
    now = bridge.node.get_clock().now().nanoseconds
    for offset in (-5, 5):
        stamp = Time(nanoseconds=now + offset * 1_000_000_000).to_msg()
        bridge.tf_buffer.clear()
        bridge.tf_buffer.set_transform(transform("map", "base_link", stamp), "test")
        assert bridge.current_pose() is None
    bridge.tf_buffer.clear()
    bridge.tf_buffer.set_transform(transform("map", "base_link", bridge.node.get_clock().now().to_msg()), "test")
    assert bridge.current_pose() is not None
    bridge.tf_buffer.clear()
    assert bridge.current_pose() is None
    bridge.poll(lambda packet: None, errors.append)
    assert len(errors) == 2  # 恢复有效后才再次提示缺失，不随每条输入刷屏。


def test_real_tf_chain_is_composed_and_each_sent_input_uses_latest_pose(bridge):
    from rclpy.node import Node
    from rclpy.time import Time
    from std_msgs.msg import String
    from tf2_ros import StaticTransformBroadcaster, TransformBroadcaster

    peer = Node("pose_tf_test_peer", context=bridge.context)
    bridge.executor.add_node(peer)
    received = []
    subscription = peer.create_subscription(String, "/pose_test/input",
        lambda msg: received.append(json.loads(msg.data)), 10)
    static = StaticTransformBroadcaster(peer)
    dynamic = TransformBroadcaster(peer)
    try:
        static.sendTransform(transform("map", "odom", peer.get_clock().now().to_msg(),
                                       x=10., y=20., yaw=math.pi / 2))
        deadline = time.monotonic() + 5
        while not bridge.publisher.get_subscription_count():
            assert time.monotonic() < deadline
            time.sleep(.01)
        for index, (x, y, yaw, expected) in enumerate([
                (2., 3., -math.pi / 4, (7., 22., math.pi / 4)),
                (4., 5., -math.pi / 2, (5., 24., 0.))]):
            stamp = peer.get_clock().now().to_msg()
            latest = transform("odom", "base_link", stamp, x=x, y=y, yaw=yaw)
            dynamic.sendTransform(latest)
            while True:
                if bridge.tf_buffer.can_transform("map", "base_link", Time()):
                    pose = bridge.current_pose()
                    if pose and pose["x"] == pytest.approx(expected[0]):
                        break
                assert time.monotonic() < deadline
                dynamic.sendTransform(latest)
                time.sleep(.01)
            assert (pose["x"], pose["y"], pose["yaw"]) == pytest.approx(expected)
            assert pose["frame_id"] == "map"
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", pose["time_stamp"])
            bridge.send(user_input(f"第 {index + 1} 条输入", pose=pose))
            while len(received) <= index:
                assert time.monotonic() < deadline
                time.sleep(.01)
            business = json.loads(received[index][0]["content"][0]["text"])
            assert business["pose"] == pose
            assert business["data"] == {} and business["source"] == "user"
            assert "origin" not in business and "event" not in business
        assert len(received) == 2
    finally:
        bridge.executor.remove_node(peer)
        peer.destroy_node()


def test_static_and_simulated_tf_do_not_fabricate_utc_sample_time(bridge):
    from rclpy.parameter import Parameter
    from rclpy.time import Time
    bridge.tf_buffer.set_transform_static(transform("map", "base_link", Time().to_msg(), x=1.), "test")
    assert bridge.current_pose() == {"frame_id": "map", "x": 1., "y": 0., "yaw": 0.}
    bridge.tf_buffer.clear()
    bridge.node.set_parameters([Parameter("use_sim_time", value=True)])
    bridge.node.get_clock().set_ros_time_override(Time(seconds=42))
    bridge.tf_buffer.set_transform(transform("map", "base_link", Time(seconds=42).to_msg(), x=2.), "test")
    pose = bridge.current_pose()
    assert pose == {"frame_id": "map", "x": 2., "y": 0., "yaw": 0.}
