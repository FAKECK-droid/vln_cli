"""界面使用中文；工具参数和结果按原始字段和值动态展示。"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

STATES = {
    "submitted": "正在执行", "waiting": "排队等待", "running": "正在执行",
    "finished": "已完成", "completed": "已完成", "complete": "已完成",
    "failed": "执行失败", "error": "发生错误", "idle": "空闲",
    "generating": "等待模型响应", "response_ready": "响应待接收",
    "waiting_interval": "等待请求间隔", "waiting_transport": "等待模型连接",
    "connected": "已连接", "connecting": "连接中", "disconnected": "连接已断开",
    "reconnecting": "重新连接中", "closed": "已关闭", "ready": "已就绪",
    "unobserved": "结束状态未收到", "interrupted": "已中断", "streaming": "正在生成", "unknown": "未收到状态",
    "server": "服务端上下文", "local": "本地上下文", "closing": "正在关闭",
}
LABELS = {
    "code": "错误代码", "message": "说明", "name": "名称",
    "time_stamp": "记录时间", "status": "状态", "state": "状态",
    "model": "模型", "reasoning": "推理配置", "effort": "推理强度",
    "max_output_tokens": "输出上限", "parallel_tool_calls": "允许并行工具",
    "store": "服务端保存", "prompt_cache_key": "缓存标识", "context_management": "上下文管理",
    "compact_threshold": "压缩阈值", "type": "类型", "format": "格式",
    "input": "输入内容", "output": "输出内容", "role": "来源", "content": "内容",
    "text": "文本", "instruction": "指令", "source": "来源", "origin": "事件来源",
    "data": "业务载荷", "pose": "小车位姿", "images": "图片元数据", "metadata": "元数据",
    "previous_response_id": "前次响应标识", "call_id": "调用标识", "arguments": "调用参数",
    "blocking": "阻塞后续工具", "async": "异步调用", "sequence": "调用序号",
    "id": "标识", "input_topic": "输入话题", "output_topic": "输出话题",
    "input_subscribers": "输入订阅端", "output_publishers": "输出发布端",
    "received_packets": "已接收更新数", "pending_updates": "待显示更新数",
    "coalesced_updates": "已合并采样数", "receiver_running": "接收线程运行中",
    "last_receive_age_ms": "距最近接收（毫秒）",
    "mode": "模式", "input_tokens": "输入用量", "output_tokens": "输出用量",
    "cached_tokens": "缓存命中", "total_tokens": "总用量", "budget_tokens": "上下文预算",
    "compaction_count": "压缩次数", "history_item_count": "上下文条目数",
    "last_error": "最近错误", "transport": "模型连接", "connected": "已连接",
    "retry_required": "需要重试", "can_retry": "允许重试", "in_flight": "请求进行中",
    "send_requested": "已登记发送", "response_ready": "响应已就绪",
    "pending_input_count": "待发输入数", "current_input_count": "本轮输入数",
    "request_elapsed_ms": "请求耗时（毫秒）", "rate_limit_remaining_ms": "限频等待（毫秒）",
    "current_response_id": "当前响应标识", "latest_response_id": "最近响应标识",
    "request_interrupted": "请求已中断", "running_count": "运行工具数",
    "waiting_count": "等待工具数", "completed_count": "已完成工具数",
    "max_concurrent_calls": "并发上限", "max_pending_calls": "等待容量", "call_count": "活动调用数",
    "usage": "模型用量", "input_tokens_details": "输入用量详情",
    "output_tokens_details": "输出用量详情", "reasoning_tokens": "推理用量",
    "response_id": "响应标识", "event": "事件", "description": "说明",
}
IMAGE_TYPES = {"input_image", "output_image", "image", "image_generation_call"}


def state_name(value):
    return STATES.get(value, str(value or "未收到状态"))


def tool_name(value):
    return str(value or "未知工具")


def local_time(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            ZoneInfo("Asia/Shanghai")).strftime("%H:%M:%S")
    except (ValueError, AttributeError):
        return str(value or "—")


def without_images(value):
    if isinstance(value, dict):
        if value.get("type") in IMAGE_TYPES:
            return None
        return {key: shown for key, item in value.items()
                if key not in ("image_url", "b64_json")
                and ((shown := without_images(item)) is not None or item is None)}
    if isinstance(value, list):
        return [shown for item in value if (shown := without_images(item)) is not None or item is None]
    if isinstance(value, str) and value.startswith("data:image/"):
        return None
    return value


def scalar(value, *, translate=True):
    if not translate:
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if value is None:
        return "无"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def describe(value, level=0, *, translate=True):
    """缩进展示任意结构；工具载荷禁用界面标签和状态翻译。"""
    if isinstance(value, dict):
        if not value:
            return "无" if translate else "{}"
        lines = []
        for key, item in value.items():
            label = LABELS.get(key, key) if translate else key
            if isinstance(item, (dict, list)) and item:
                lines.append("  " * level + label + "：")
                lines.append(describe(item, level + 1, translate=translate))
            else:
                shown = (state_name(item) if translate and key in ("state", "status", "mode")
                         and isinstance(item, str) else scalar(item, translate=translate))
                if translate and item in ([], {}):
                    shown = "无"
                lines.append("  " * level + label + "：" + shown)
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "无" if translate else "[]"
        return "\n".join("  " * level + f"第 {i + 1} 项\n" + describe(item, level + 1, translate=translate)
                         for i, item in enumerate(value))
    return "  " * level + scalar(value, translate=translate)


def arguments(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    return describe(without_images(value), translate=False)


def error_text(source, error):
    """标注真实组件，保留完整错误字段及原始异常类型/信息。"""
    if isinstance(error, BaseException):
        detail = f"{type(error).__name__}: {error}"
    else:
        detail = describe(without_images(error), translate=False)
    return "错误来源：" + source + "\n" + detail


def call_duration(call):
    state = call["state"]
    if state == "submitted":
        return "等待工具状态"
    if state == "interrupted":
        return "已中断"
    elapsed = call["wait_ms" if state == "waiting" else "run_ms"]
    return f"{elapsed:g} 毫秒"


def call_text(call, detail=False):
    name = tool_name(call.get("name"))
    status = state_name(call.get("state", "submitted"))
    lines = [name + " · " + status]
    if call.get("description"):
        lines.append(call["description"])
    if call.get("state") == "failed":
        lines += [error_text("ToolManager / " + name, {
            "name": name, "call_id": call["call_id"],
            **{key: call[key] for key in ("code", "message") if key in call}})]
    if detail:
        wait = call.get("wait_ms")
        lines += ["函数：" + call.get("name", "未知"), "调用标识：" + call["call_id"],
                  "记录时间：" + local_time(call.get("first_seen")),
                  "执行耗时：" + ("尚未开始" if call.get("state") == "waiting" else call_duration(call)),
                  "排队耗时：" + (f"{wait:g} 毫秒" if wait is not None else "等待工具状态")]
        if "sequence" in call:
            lines.append("调用序号：" + str(call["sequence"]))
    if "arguments" in call:
        lines += ["", "调用参数", arguments(call["arguments"])]
    if "result" in call:
        lines += ["", "返回结果", describe(without_images(call["result"]), translate=False)]
    elif call.get("state") in ("finished", "failed"):
        lines += ["", "尚未收到返回内容"]
    else:
        lines += ["", "正在执行，等待工具返回…"]
    return "\n".join(lines)


def input_text(inputs):
    lines = []
    for item in inputs or []:
        if not isinstance(item, dict):
            lines.append(str(item))
            continue
        if item.get("type") == "function_call_output":
            payload = json.loads(item["output"][0]["text"])
            lines.append("工具结果 · " + item["call_id"] + "\n" +
                         describe(without_images(payload), translate=False))
            continue
        if item.get("type") == "function_call":
            lines.append("工具调用 · " + tool_name(item.get("name")))
            continue
        role = {"user": "用户", "system": "系统", "assistant": "助手", "developer": "开发者"}.get(item.get("role"), "输入")
        content = item.get("content", [])
        if isinstance(content, str):
            content = [{"text": content}]
        for part in content:
            if part.get("type") in IMAGE_TYPES:
                continue
            text = part.get("text", "")
            if not text:
                continue
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                payload = text
            source = ("机器事件" if isinstance(payload, dict) and
                      payload.get("source") == "robot" and "event" in payload else role)
            lines.append(source + "：" + (
                describe(without_images(payload)) if isinstance(payload, (dict, list)) else str(payload)))
    return "\n\n".join(lines) or "无文本输入"


def request_text(record):
    request = record["request"]
    options = {key: value for key, value in request.items()
               if key not in ("input", "tools", "type")}
    elapsed = (f"{record['elapsed_ms'] / 1000:.2f} 秒"
               if record.get("elapsed_ms") is not None else "未上报")
    if record.get("elapsed_ms") is not None and record.get("elapsed_final") is False:
        elapsed += "（末次采样）"
    return ("请求 #" + str(record["sequence"]) + " · " + state_name(record["state"]) +
            "\n开始时间：" + local_time(record["time_stamp"]) +
            "\n请求耗时：" + elapsed +
            ("\n响应标识：" + record["response_id"] if record.get("response_id") else "") +
            ("\n\n" + error_text("SessionManager（会话请求）", record["error"])
             if record.get("error") else "") +
            "\n\n模型与请求配置\n" + describe(options) +
            "\n\n输入内容\n" + input_text(request.get("input")))
