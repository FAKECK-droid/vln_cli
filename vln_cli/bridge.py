"""ROS 接收独立于终端刷新；只暂存待处理更新，不记录原始包历史。"""
from collections import deque
from datetime import datetime, timezone
import math
import json
import threading
import time
from typing import Callable

from .state import Json, utc_now


def replaceable_telemetry(previous: Json, current: Json) -> bool:
    """只合并同一状态内的连续全量采样；请求边界、调用、结果和差量文本必须保留。"""
    if current.get("batch_id", 0) <= previous.get("batch_id", 0):
        return False
    if previous.get("full") and previous.get("batch_id") == 1:
        return False
    for packet in (previous, current):
        response, tools, session = packet.get("response") or {}, packet.get("tools") or {}, packet.get("session") or {}
        if "request" not in packet or "request_status" not in session or "queue" not in tools:
            return False
        if response.get("id") or response.get("function_calls") or response.get("tool_outputs") or tools.get("finished_calls"):
            return False
        if any(part.get("op") != "replace" for part in response.get("messages", [])):
            return False
    before = previous["session"]["request_status"] or {}
    after = current["session"]["request_status"] or {}
    if after.get("request_elapsed_ms", 0) < before.get("request_elapsed_ms", 0):
        return False
    ignored = {"request_elapsed_ms", "rate_limit_remaining_ms"}
    if {k: v for k, v in before.items() if k not in ignored} != {k: v for k, v in after.items() if k not in ignored}:
        return False
    if previous["request"] != current["request"]:
        return False
    old_tools, new_tools = previous["tools"], current["tools"]
    for key in ("definitions", "status"):
        if old_tools.get(key) != new_tools.get(key):
            return False
    def queue_identity(tools):
        return {group: [{k: v for k, v in call.items() if k not in {"run_ms", "wait_ms"}}
                        for call in calls] for group, calls in (tools.get("queue") or {}).items()}
    if queue_identity(old_tools) != queue_identity(new_tools):
        return False
    old_response, new_response = previous.get("response") or {}, current.get("response") or {}
    if old_response.get("stream") != new_response.get("stream"):
        return False
    def positions(response):
        return [(part.get("response_id"), part.get("output_index"), part.get("content_index"))
                for part in response.get("messages", [])]
    return (positions(old_response) == positions(new_response) and
            previous["session"].get("context_status") == current["session"].get("context_status"))


class RosBridge:
    def __init__(self, input_topic: str, output_topic: str, history_limit: int,
                 ros_args: list[str] | None = None):
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.node import Node
        from std_msgs.msg import String
        from tf2_ros import Buffer, TransformListener

        self._String = String
        self.context = Context()
        self.context.init(args=ros_args)
        self.node = Node("vln_cli", context=self.context)
        self.input_topic = self.node.declare_parameter("input_topic", input_topic).value
        self.output_topic = self.node.declare_parameter("output_topic", output_topic).value
        self.history_limit = self.node.declare_parameter("history_limit", history_limit).value
        self.pose_frame = self.node.declare_parameter("pose_frame", "map").value
        self.pose_base_frame = self.node.declare_parameter("pose_base_frame", "base_link").value
        self.pose_max_age_sec = self.node.declare_parameter("pose_max_age_sec", 2.0).value
        if (not self.pose_frame or not self.pose_base_frame or
                not math.isfinite(self.pose_max_age_sec) or self.pose_max_age_sec <= 0):
            self.node.destroy_node()
            self.context.shutdown()
            raise ValueError("pose_frame/pose_base_frame 不能为空，pose_max_age_sec 必须大于零")
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self.node, spin_thread=False)
        self._pose_warned = False
        self.publisher = self.node.create_publisher(String, self.input_topic, 10)
        self.subscription = self.node.create_subscription(
            String, self.output_topic, self._receive, 100)
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self._incoming = deque()
        self._errors = deque()
        self._received = 0
        self._closed = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_received = None
        self._coalesced = 0
        self._thread = threading.Thread(target=self._spin, name="vln-cli-ros", daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        try:
            while not self._stop.is_set():
                self.executor.spin_once(timeout_sec=.05)
        except Exception as error:
            if not self._stop.is_set():
                with self._lock:
                    self._errors.append("ROS 接收线程已停止：" + str(error))

    def _receive(self, message) -> None:
        try:
            packet = json.loads(message.data)
            if not isinstance(packet, dict):
                raise ValueError("输出话题必须为 JSON 对象")
            with self._lock:
                if self._incoming and replaceable_telemetry(self._incoming[-1], packet):
                    self._incoming[-1] = packet
                    self._coalesced += 1
                else:
                    self._incoming.append(packet)
                self._received += 1
                self._last_received = time.monotonic()
        except (ValueError, TypeError) as error:
            with self._lock:
                self._errors.append(f"输出话题 JSON 无法解析：{error}")

    def poll(self, receive: Callable[[Json], None], error: Callable[[str], None]) -> None:
        # UI 仅取走已接收更新，不负责驱动 ROS executor。
        with self._lock:
            packets = list(self._incoming)
            self._incoming.clear()
            errors = list(self._errors)
            self._errors.clear()
        for packet in packets:
            try:
                receive(packet)
            except (ValueError, KeyError, TypeError) as exception:
                error(f"输出协议无法解析：{exception}")
        for message in errors:
            error(message)

    def current_pose(self) -> Json | None:
        """从已接收的 TF 缓存读取最新二维位姿，不等待 TF、不额外 spin。"""
        from rclpy.time import Time
        from tf2_ros import TransformException

        try:
            stamped = self.tf_buffer.lookup_transform(self.pose_frame, self.pose_base_frame, Time())
            transform = stamped.transform
            position, rotation = transform.translation, transform.rotation
            values = (position.x, position.y, rotation.x, rotation.y, rotation.z, rotation.w)
            norm = sum(v * v for v in values[2:])
            if not all(math.isfinite(v) for v in values) or norm < 1e-12:
                raise ValueError("TF 位姿包含无效数值")
            stamp_ns = stamped.header.stamp.sec * 1_000_000_000 + stamped.header.stamp.nanosec
            if stamp_ns:
                age_sec = (self.node.get_clock().now().nanoseconds - stamp_ns) / 1_000_000_000
                if abs(age_sec) > self.pose_max_age_sec:
                    raise ValueError("TF 已过期或时钟偏差超出 pose_max_age_sec")
            pose = {"frame_id": self.pose_frame, "x": position.x, "y": position.y,
                    "yaw": math.atan2(2 * (rotation.w * rotation.z + rotation.x * rotation.y),
                                      norm - 2 * (rotation.y ** 2 + rotation.z ** 2))}
            # 仿真时钟不能当作 UTC；静态 TF 的零时间也不伪造采样时刻。
            if stamp_ns and not self.node.get_parameter("use_sim_time").value:
                pose["time_stamp"] = datetime.fromtimestamp(
                    stamped.header.stamp.sec, timezone.utc).replace(
                        microsecond=stamped.header.stamp.nanosec // 1000).isoformat(
                            timespec="milliseconds").replace("+00:00", "Z")
            self._pose_warned = False
            return pose
        except (TransformException, ValueError) as error:
            if not self._pose_warned:
                with self._lock:
                    self._errors.append(f"无法获取 {self.pose_frame} → {self.pose_base_frame} 位姿，"
                                        f"输入将附加 pose: null：{error}")
                self._pose_warned = True
            return None

    def send(self, inputs: list[Json]) -> None:
        message = self._String()
        message.data = json.dumps(inputs, ensure_ascii=False)
        self.publisher.publish(message)

    def connection(self) -> Json:
        with self._lock:
            diagnostics = {"received_packets": self._received, "pending_updates": len(self._incoming),
                           "coalesced_updates": self._coalesced, "receiver_running": self._thread.is_alive(),
                           "last_receive_age_ms": round((time.monotonic() - self._last_received) * 1000)
                           if self._last_received is not None else None}
        return {
            **diagnostics, "mode": "ROS 2", "input_topic": self.input_topic, "output_topic": self.output_topic,
            "input_subscribers": self.publisher.get_subscription_count(),
            "output_publishers": self.node.count_publishers(self.subscription.topic_name),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self.executor.wake()
        self._thread.join()
        self.executor.remove_node(self.node)
        self.executor.shutdown()
        self.tf_listener.unregister()
        self.node.destroy_node()
        self.context.shutdown()


class DemoBridge:
    """离线演示同一份话题协议；不创建 ROS 节点或模型连接。"""
    history_limit = 200

    def __init__(self):
        self._packets = deque()
        self._batch = 0
        self._turn = 0
        self._packets.append(self.packet(
            full=True,
            request={"model": "gpt-6.1-terra", "reasoning": {"effort": "low"},
                     "max_output_tokens": 8192, "store": True, "parallel_tool_calls": True,
                     "input": [], "prompt_cache_key": "scout-mini-vln"},
            session={
                "request_status": {"state": "idle", "pending_input_count": 0,
                    "request_elapsed_ms": 1820, "transport": {"connected": True, "state": "connected"}},
                "context_status": {"budget_tokens": 128000, "compact_threshold": 102400,
                    "compaction_count": 1, "usage": {"input_tokens": 12640, "output_tokens": 418,
                        "input_tokens_details": {"cached_tokens": 8192}}}},
            tools={
                "definitions": [
                    {"name": "capture_image", "description": "获取相机当前画面"},
                    {"name": "navigate_to_pose_get_feedback", "description": "查询导航反馈"}],
                "status": {"state": "ready", "running_count": 1, "waiting_count": 0,
                    "max_concurrent_calls": 4, "completed_count": 12},
                "queue": {"waiting": [], "running": [
                    {"name": "capture_image", "call_id": "demo-camera", "run_ms": 420}]}}
        ))
        self._packets.append(self.packet(
            request=None,
            response={"id": "demo-welcome", "status": "completed", "messages": [
                {"response_id": "demo-welcome", "output_index": 0, "content_index": 0,
                 "type": "output_text", "op": "append", "text":
                    "### 控制台已就绪\n这是 **离线演示**，没有连接机器人或模型。\n\n"
                    "左侧输入任务，右侧检查会话、工具与原始请求。"
                    "输入 `/model` 查看模型配置，按 **F1** 查看全部指令。"}]}))

    def packet(self, **values) -> Json:
        self._batch += 1
        return {"type": "interaction_update", "batch_id": self._batch,
                "time_stamp": utc_now(), **values}

    def poll(self, receive, error) -> None:
        if self._packets:
            receive(self._packets.popleft())

    def current_pose(self) -> None:
        return None

    def send(self, inputs: list[Json]) -> None:
        self._turn += 1
        identity = f"demo-{self._turn}"
        instruction = json.loads(inputs[0]["content"][0]["text"])["instruction"]
        self._packets.append(self.packet(
            request={"model": "gpt-6.1-terra", "reasoning": {"effort": "low"},
                     "max_output_tokens": 8192, "input": inputs},
            response={"messages": [{"response_id": identity, "output_index": 0,
                "content_index": 0, "type": "output_text", "op": "append", "text": "已收到演示输入："}]}))
        self._packets.append(self.packet(request=None, response={
            "id": identity, "status": "completed", "messages": [
                {"response_id": identity, "output_index": 0, "content_index": 0,
                 "type": "output_text", "op": "append",
                 "text": instruction + "\n\n当前为演示模式；这条消息没有发送到 ROS 或模型。"}]}))

    def connection(self) -> Json:
        return {"mode": "DEMO", "input_topic": "/vln/user_input",
                "output_topic": "/vln/user_output", "input_subscribers": 0, "output_publishers": 0}

    def close(self) -> None:
        pass


class ReplayBridge:
    def __init__(self, path):
        with open(path, encoding="utf-8") as file:
            source = json.load(file)
        self._packets = deque(source["packets"] if isinstance(source, dict) else source)
        self.history_limit = 200

    def poll(self, receive, error) -> None:
        # 限制每帧回放量，保证大文件打开时仍能响应键盘。
        for _ in range(min(8, len(self._packets))):
            receive(self._packets.popleft())

    def connection(self) -> Json:
        return {"mode": "REPLAY", "input_topic": "只读回放",
                "output_topic": "已记录的 interaction_update", "input_subscribers": 0,
                "output_publishers": 0}

    def current_pose(self) -> None:
        return None

    def send(self, inputs: list[Json]) -> None:
        raise RuntimeError("回放模式只读；请退出回放后连接 ROS 话题。")

    def close(self) -> None:
        pass
