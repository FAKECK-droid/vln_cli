"""合并全量/差量快照，只保留有界聊天、工具和请求历史。"""
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import json
from copy import deepcopy
import time

from .presentation import call_text

Json = dict[str, Any]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def user_input(instruction: str, pose: Json | None = None) -> list[Json]:
    """每次发布的 data 是原生 Responses input[] 数组的 JSON 文本。"""
    payload = {"source": "user", "time_stamp": utc_now(), "instruction": instruction, "pose": pose}
    return [{"role": "user", "content": [
        {"type": "input_text", "text": json.dumps(payload, ensure_ascii=False)}
    ]}]


@dataclass
class Message:
    key: str
    role: str
    time_stamp: str
    state: str = "complete"
    revision: int = 0
    parts: dict[tuple[int, int], str] = field(default_factory=dict)
    call_id: str | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(text for _, text in sorted(self.parts.items()))


def result_payload(output: Json) -> Any:
    value = output.get("output")
    if isinstance(value, list):
        value = "\n".join(item.get("text", "") for item in value
                          if item.get("type") == "input_text")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class ViewState:
    """仅当前遥测与有上限的终端聊天/工具记录；缺省键保持、显式 null 清除。"""

    def __init__(self, history_limit: int = 200):
        if history_limit < 1:
            raise ValueError("history_limit 必须大于零")
        self.history_limit = history_limit
        self.messages: OrderedDict[str, Message] = OrderedDict()
        self.calls: OrderedDict[str, Json] = OrderedDict()
        self.requests: OrderedDict[str, Json] = OrderedDict()
        self.current_request_key: str | None = None
        self._request_serial = 0
        self._completed_responses: OrderedDict[tuple[int, str], None] = OrderedDict()
        self._cleared_calls: set[str] = set()
        self._cleared_responses: OrderedDict[tuple[int, str], None] = OrderedDict()
        self.request: Json | None = None
        self.model_info: Json = {}
        self.request_status: Json = {}
        self.context_status: Json = {}
        self.tool_status: Json = {}
        self.tool_queue: Json = {"waiting": [], "running": []}
        self.definitions: list[Json] = []
        self.definitions_received = False
        self.stream: Json | None = None
        self.packet_count = 0
        self.completed_count = 0
        self.last_received: float | None = None
        self.last_stamp = ""
        self.last_batch: int | None = None
        self.last_packet_key: tuple[str, int] | None = None
        self.latest_response = ""
        self.epoch = 0
        self.revision = 0
        self._local_serial = 0

    def local_message(self, role: str, text: str) -> Message:
        self._local_serial += 1
        message = Message(f"local-{self._local_serial}", role, utc_now(),
                          parts={(0, 0): text})
        self.messages[message.key] = message
        self._prune()
        self.revision += 1
        return message

    def clear_chat(self) -> None:
        self._cleared_calls.update(self.calls)
        for key, message in self.messages.items():
            if message.role == "assistant":
                self._cleared_responses[(self.epoch, key.split("-", 2)[-1])] = None
        while len(self._cleared_responses) > self.history_limit * 2:
            self._cleared_responses.popitem(last=False)
        self.messages.clear()
        self.revision += 1

    def _prune(self) -> None:
        while len(self.messages) > self.history_limit:
            self.messages.popitem(last=False)
        # 当前排队/运行中的工具仍有用途，不随终端历史窗口一起删除。
        active = {call["call_id"] for group in self.tool_queue.values() for call in group}
        for key in list(self.calls):
            if len(self.calls) <= self.history_limit + len(active):
                break
            if key not in active:
                del self.calls[key]

    def _restart(self) -> None:
        # 只有批次回退且时间更新才视为 Agent 重启；full 本身仅表示全量快照。
        self.epoch += 1
        self.request = None
        self.current_request_key = None
        self.model_info = {}
        self.request_status = {}
        self.context_status = {}
        self.tool_status = {}
        self.tool_queue = {"waiting": [], "running": []}
        self.definitions = []
        self.definitions_received = False
        self.stream = None
        self.latest_response = ""
        for identity, call in self.calls.items():
            if call.get("state") not in ("finished", "failed", "interrupted"):
                call["state"] = "interrupted"
                self._sync_tool_message(identity)
        for record in self.requests.values():
            if record["state"] not in ("finished", "failed", "interrupted"):
                record["state"] = "interrupted"
        for message in self.messages.values():
            if message.state == "streaming":
                message.state = "interrupted"

    def _observe_request(self, previous: Json | None, response: Json, stamp: str) -> None:
        status = self.request_status
        elapsed = status.get("request_elapsed_ms")
        active_id = status.get("current_response_id") or ""
        finished_id = response.get("id") if response.get("status") == "completed" else ""
        confirmed = {identity for epoch, identity in self._completed_responses if epoch == self.epoch}
        confirmed.update(filter(None, [finished_id, self.context_status.get("latest_response_id"),
                                       (self.request or {}).get("previous_response_id")]))
        terminal = {"finished", "failed", "interrupted", "unobserved"}
        record = self.requests.get(self.current_request_key)

        # 完成事件按响应 ID 回填所有对应历史，不能只更新“当前记录”。
        for item in self.requests.values():
            if item.get("epoch", self.epoch) != self.epoch:
                continue
            if item.get("response_id") in confirmed and item["state"] not in {"failed", "interrupted"}:
                item["state"] = "finished"

        if self.request is not None:
            changed = previous is None or previous != self.request
            restarted = (record is not None and elapsed is not None and
                         record.get("elapsed_ms") is not None and elapsed < record["elapsed_ms"])
            if record is None or changed or restarted:
                if record is not None and record["state"] not in terminal:
                    if not record.get("response_id"):
                        chain_head = self.request.get("previous_response_id")
                        if chain_head and chain_head != record["request"].get("previous_response_id"):
                            record["response_id"] = chain_head
                    record["state"] = "finished" if record.get("response_id") in confirmed else "unobserved"
                self._request_serial += 1
                self.current_request_key = f"request-{self.epoch}-{self._request_serial}"
                record = {"key": self.current_request_key, "sequence": self._request_serial,
                          "epoch": self.epoch, "time_stamp": stamp, "request": deepcopy(self.request),
                          "state": "generating", "elapsed_ms": elapsed, "elapsed_final": False,
                          "response_id": ""}
                self.requests[self.current_request_key] = record

        if record is not None:
            if record["state"] not in terminal:
                record["elapsed_ms"] = elapsed
                record["state"] = status.get("state", record["state"])
                if active_id and active_id != record["request"].get("previous_response_id"):
                    record["response_id"] = active_id
                if status.get("last_error"):
                    record["state"] = "failed"
                elif status.get("request_interrupted"):
                    record["state"] = "interrupted"
                elif (record.get("response_id") in confirmed or status.get("response_ready") or
                      (self.request is None and status.get("state") == "idle")):
                    record["state"] = "finished"
                    if finished_id:
                        record["response_id"] = finished_id
            # 当前状态的耗时可能已属于下一轮；只有同一响应的终态采样才能当最终耗时。
            same_response = not active_id or active_id == record.get("response_id")
            if self.request is None and same_response and record["state"] in {"finished", "failed", "interrupted"}:
                record["elapsed_ms"] = elapsed
                record["elapsed_final"] = elapsed is not None
        while len(self.requests) > self.history_limit:
            self.requests.popitem(last=False)

    def _message_part(self, part: Json, stamp: str, full: bool) -> None:
        identity = part.get("response_id", "")
        if not identity or (self.epoch, identity) in self._cleared_responses:
            return
        key = f"response-{self.epoch}-{identity}"
        message = self.messages.get(key)
        if message is None:
            message = Message(key, "assistant", stamp, "streaming")
            self.messages[key] = message
        position = (part.get("output_index", 0), part.get("content_index", 0))
        text = part.get("text", "")
        # 快照中的文本是当前全文；旧协议的 append/replace 差量仍可接收。
        if full or part.get("op", "replace") == "replace":
            updated = text
        elif (self.epoch, identity) in self._completed_responses:
            return
        else:
            updated = message.parts.get(position, "") + text
        if message.parts.get(position) != updated:
            message.parts[position] = updated
            message.revision += 1

    def apply(self, packet: Json) -> bool:
        if packet.get("type") != "interaction_update":
            raise ValueError("收到的消息不是 interaction_update")
        stamp, batch = packet["time_stamp"], packet["batch_id"]
        identity = (stamp, batch)
        if self.last_packet_key is not None and identity <= self.last_packet_key:
            return False
        if self.last_batch is not None and batch < self.last_batch:
            self._restart()
        elif not self.packet_count:
            self.epoch = 1
        self.last_packet_key = identity
        self.last_stamp, self.last_batch = stamp, batch
        self.last_received = time.monotonic()
        self.packet_count += 1
        previous_request = self.request
        if "request" in packet:
            self.request = packet["request"]
            if self.request is not None:
                self.model_info = {key: value for key, value in self.request.items()
                                   if key not in ("input", "tools", "type", "previous_response_id")}
        session = packet.get("session") or {}
        if "request_status" in session:
            self.request_status = session["request_status"] or {}
        if "context_status" in session:
            self.context_status = session["context_status"] or {}
        tools = packet.get("tools") or {}
        if "definitions" in tools:
            self.definitions = tools["definitions"] or []
            self.definitions_received = True
        if "status" in tools:
            self.tool_status = tools["status"] or {}
        response = packet.get("response") or {}
        full = packet.get("full", False)
        if "stream" in response:
            self.stream = response["stream"]
        for part in response.get("messages", []):
            self._message_part(part, stamp, full)
        if full and self.stream and not response.get("messages"):
            for part in self.stream.get("content", []):
                self._message_part({**part, "response_id": self.stream.get("response_id")}, stamp, True)
        touched = set()
        for call in response.get("function_calls", []):
            key = call["call_id"]
            self._update_call(key, call, stamp)
            touched.add(key)
        if "queue" in tools:
            self.tool_queue = tools["queue"] or {"waiting": [], "running": []}
            for state in ("waiting", "running"):
                for call in self.tool_queue.get(state, []):
                    self._update_call(call["call_id"], {**call, "state": state}, stamp)
                    touched.add(call["call_id"])
        for call in tools.get("finished_calls", []):
            values = dict(call)
            if "run_ms" in call:
                values["duration_ms"] = call["run_ms"]
            self._update_call(call["call_id"], values, stamp)
            touched.add(call["call_id"])
        for output in response.get("tool_outputs", []):
            payload = result_payload(output)
            state = "failed" if isinstance(payload, dict) and payload.get("ok") is False else "finished"
            self._update_call(output["call_id"], {"state": state, "result": payload}, stamp)
            touched.add(output["call_id"])
        if "definitions" in tools:
            for key, call in self.calls.items():
                self._update_call(key, {}, stamp)
                if self.calls[key].get("description") != call.get("description"):
                    touched.add(key)
        # 按调用首次出现的顺序插入，每个 call_id 只占一个对话条目。
        for key in self.calls:
            if key in touched:
                self._sync_tool_message(key)
        completed_ids = []
        if response.get("status") == "completed" and response.get("id"):
            completed_ids.append(response["id"])
        if self.stream and self.stream.get("state") == "completed" and self.stream.get("response_id"):
            completed_ids.append(self.stream["response_id"])
        for response_id in completed_ids:
            self.latest_response = response_id
            key = f"response-{self.epoch}-{response_id}"
            if key in self.messages:
                self.messages[key].state = "complete"
            completed_key = (self.epoch, response_id)
            if completed_key not in self._completed_responses:
                self._completed_responses[completed_key] = None
                self.completed_count += 1
            while len(self._completed_responses) > self.history_limit * 2:
                self._completed_responses.popitem(last=False)
        self._observe_request(previous_request, response, stamp)
        self._prune()
        self._cleared_calls.intersection_update(self.calls)
        self.revision += 1
        return True

    def _update_call(self, identity: str, values: Json, stamp: str) -> None:
        previous = self.calls.get(identity, {})
        values = dict(values)
        name = values.get("name", previous.get("name"))
        definition = next((tool for tool in self.definitions if tool.get("name") == name), None)
        if definition is not None:
            values["description"] = definition.get("description", "")
        # 高频全量快照可能仍含提交项；终态不能退回正在执行。
        if previous.get("state") in ("finished", "failed", "interrupted"):
            if values.get("state") not in ("finished", "failed", "interrupted"):
                values.pop("state", None)
            if previous.get("state") == "failed" and values.get("state") == "finished":
                values.pop("state")
        self.calls[identity] = {"first_seen": stamp, "state": "submitted", **previous,
                                **values, "call_id": identity}

    def _sync_tool_message(self, identity: str) -> None:
        call = self.calls[identity]
        key = "tool-" + identity
        message = self.messages.get(key)
        if message is None:
            if identity in self._cleared_calls:
                return
            message = Message(key, "tool", call["first_seen"], call_id=identity)
            self.messages[key] = message
        text = call_text(call)
        if message.text != text or message.state != call["state"]:
            message.parts = {(0, 0): text}
            message.state = call["state"]
            message.revision += 1

    @property
    def error(self) -> Any:
        return self.request_status.get("last_error") or self.request_status.get(
            "transport", {}).get("last_error")

    def snapshot(self) -> Json:
        return {
            "time_stamp": utc_now(), "model": self.model_info,
            "request": self.request, "requests": list(self.requests.values()),
            "session": {
                "request_status": self.request_status, "context_status": self.context_status},
            "tools": {"definitions": self.definitions, "status": self.tool_status,
                      "queue": self.tool_queue, "calls": list(self.calls.values())},
            "messages": [
                {"role": item.role, "time_stamp": item.time_stamp,
                 "state": item.state, "text": item.text, "call_id": item.call_id}
                for item in self.messages.values()],
        }
