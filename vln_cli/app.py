"""Textual 界面：聊天、遥测和本地指令，所有 ROS 细节都留在 bridge。"""
import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import shlex
import time

from rich.table import Table
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.suggester import SuggestFromList
from textual.widgets import (
    DataTable, Footer, Input, Markdown, ProgressBar, Static, TabbedContent, TabPane,
)

from .state import Message, ViewState, user_input
from .presentation import (arguments, call_duration, call_text, describe, local_time,
                           request_text, state_name, tool_name, without_images, error_text)

COMMANDS = {
    "/help": "显示指令和快捷键",
    "/model": "查看最近观察到的模型及推理配置",
    "/status": "查看连接、请求和上下文状态",
    "/tools": "查看工具定义和执行状态",
    "/request": "查看当前请求和历史请求",
    "/session": "查看会话和错误详情",
    "/topics": "查看 ROS 话题与匹配端点",
    "/clear": "清空本地聊天，不清空模型上下文",
    "/export": "导出当前视图：/export [文件.json]",
    "/quit": "退出终端",
}


def number(value) -> str:
    return f"{value:,}" if isinstance(value, (int, float)) else "—"


def short(value, limit=28) -> str:
    value = str(value or "—")
    return value if len(value) <= limit else value[:limit - 1] + "…"


class CommandInput(Input):
    BINDINGS = [
        Binding("up", "previous", show=False),
        Binding("down", "next", show=False),
    ]

    def __init__(self):
        super().__init__(
            placeholder="输入指令与 Agent 对话；/help 查看命令",
            suggester=SuggestFromList(COMMANDS, case_sensitive=False),
            id="composer",
        )
        self.history: list[str] = []
        self.position: int | None = None
        self.draft = ""

    def remember(self, value: str) -> None:
        if not self.history or self.history[-1] != value:
            self.history.append(value)
            self.history = self.history[-100:]
        self.position = None

    def action_previous(self) -> None:
        if not self.history:
            return
        if self.position is None:
            self.draft = self.value
            self.position = len(self.history)
        self.position = max(0, self.position - 1)
        self.value = self.history[self.position]
        self.cursor_position = len(self.value)

    def action_next(self) -> None:
        if self.position is None:
            return
        self.position += 1
        if self.position >= len(self.history):
            self.position = None
            self.value = self.draft
        else:
            self.value = self.history[self.position]
        self.cursor_position = len(self.value)


class ChatBubble(Vertical):
    def __init__(self, message: Message):
        self.heading = Static(classes="message-heading")
        self.body = (Static("", classes="message-body", markup=False) if message.role == "tool"
                     else Markdown("", classes="message-body", open_links=False))
        super().__init__(self.heading, self.body, classes=f"bubble {message.role}")
        self.message = message
        self.last_view = None

    async def sync(self, message: Message) -> None:
        view = (message.revision, message.state)
        if view == self.last_view or not self.body.is_attached:
            return
        labels = {"user": ("你", "#65e5da"), "assistant": ("助手", "#ae9dff"),
                  "system": ("控制台", "#f3c36a"), "error": ("错误", "#ff788d"),
                  "tool": ("工具", "#7ed4a7")}
        name, color = labels[message.role]
        heading = Text(name, style=f"bold {color}")
        heading.append("  " + local_time(message.time_stamp), style="#72849c")
        if message.role == "tool":
            heading.append("   " + state_name(message.state),
                           style="#ff788d" if message.state == "failed" else "#7ed4a7")
        if message.state == "streaming":
            heading.append("   ● 生成中", style="#b4a0ff")
        self.heading.update(heading)
        if message.role == "tool":
            self.body.update(Text(message.text))
        else:
            await self.body.update(message.text)
        self.last_view = view


class VLNConsole(App):
    TITLE = "VLN · 终端控制台"
    CSS_PATH = "console.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+q", "quit", "退出"),
        Binding("f1", "help", "帮助"),
        Binding("ctrl+l", "clear", "清屏", priority=True),
        Binding("ctrl+b", "sidebar", "侧栏", priority=True),
        Binding("ctrl+k", "focus_input", "输入", priority=True),
    ]

    def __init__(self, bridge, history_limit=None):
        super().__init__()
        self.bridge = bridge
        self.state = ViewState(history_limit or bridge.history_limit)
        self.bubbles: dict[str, ChatBubble] = {}
        self.rendered: dict[str, object] = {}
        self.sidebar_hidden: bool | None = None
        self.last_revision = -1
        self._chat_signature = None
        self._pending_user = None
        self._connection_notice_attempted = False
        self._refresh_lock = asyncio.Lock()
        self.connection_info = bridge.connection()
        self.last_bridge_error = ""
        self.selected_call: str | None = None
        self.selected_request: str | None = None
        self._tables: dict[str, dict] = {}
        self.theme = "textual-dark"

    def compose(self) -> ComposeResult:
        with Horizontal(id="masthead"):
            yield Static(Text.assemble(("◈ ", "bold #66eadf"),
                ("VLN", "bold #e9f0ff"), ("  /  终端控制台", "#899bb8")), id="brand")
            yield Static("", id="clock")
        yield Static("", id="ribbon")
        yield Static("", id="error-banner", markup=False)
        with Horizontal(id="workspace"):
            with Vertical(id="conversation"):
                with VerticalScroll(id="chat"):
                    yield Static(Text.assemble(
                        ("让每一步行动都有迹可循\n", "bold #e7eeff"),
                        ("ROS 2 · 会话观测 · 实时对话\n", "#7e94af"),
                        ("Enter 发送    /model 模型    F1 帮助", "#55cfc8")), id="welcome")
                with Vertical(id="compose-box"):
                    yield CommandInput()
                    yield Static("Enter 发送 · ↑↓ 输入历史 · // 开头发送字面量斜杠", id="input-hint")
            with Vertical(id="sidebar"):
                with TabbedContent(initial="overview-tab", id="inspector"):
                    with TabPane("概览", id="overview-tab"):
                        with VerticalScroll():
                            yield Static("", id="model-card")
                            yield Static("", id="context-label")
                            yield ProgressBar(total=100, show_eta=False, id="context-bar")
                            yield Static("", id="usage-card")
                            yield Static("", id="runtime-card")
                            yield Static("", id="link-card")
                    with TabPane("工具", id="tools-tab"):
                        yield Static("调用历史 · 双击或 Enter 查看详情", classes="section-title")
                        yield DataTable(id="call-table", cursor_type="row", zebra_stripes=True)
                        with VerticalScroll(id="tools-details"):
                            yield Static("", id="call-detail", markup=False)
                            yield Static("", id="tool-definitions", markup=False)
                    with TabPane("请求", id="request-tab"):
                        yield Static("请求历史 · 双击或 Enter 查看详情", classes="section-title")
                        yield DataTable(id="request-table", cursor_type="row", zebra_stripes=True)
                        with VerticalScroll(id="request-scroll"):
                            yield Static("", id="request-detail", markup=False)
                    with TabPane("状态", id="session-tab"):
                        with VerticalScroll():
                            yield Static("", id="session-detail", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#chat").border_title = "对话与工具执行"
        self.query_one("#call-table", DataTable).add_columns("状态", "工具 / 调用", "耗时")
        self.query_one("#request-table", DataTable).add_columns("序号 / 时间", "模型", "状态")
        self.query_one("#error-banner").display = False
        self._layout()
        self.set_interval(0.1, self.refresh_tick)
        self.query_one("#composer", Input).focus()
        await self.refresh_tick()

    def on_resize(self, event: events.Resize) -> None:
        self._layout(event.size.width)

    def _layout(self, width=None) -> None:
        width = self.size.width if width is None else width
        hidden = width < 110 if self.sidebar_hidden is None else self.sidebar_hidden
        self.set_class(hidden, "compact")

    def action_sidebar(self) -> None:
        self.sidebar_hidden = not self.has_class("compact")
        self._layout()

    def action_focus_input(self) -> None:
        self.query_one("#composer", Input).focus()

    async def action_help(self) -> None:
        await self.execute("/help")

    async def action_clear(self) -> None:
        await self.execute("/clear")

    def on_unmount(self) -> None:
        self.bridge.close()

    def _set(self, selector: str, value) -> None:
        if self.rendered.get(selector) == value:
            return
        first = selector not in self.rendered
        self.rendered[selector] = value
        self.query_one(selector, Static).update(
            value, layout=first or selector not in {"#clock", "#ribbon", "#context-label"})

    async def refresh_tick(self) -> None:
        async with self._refresh_lock:
            # 退出时 Screen 的子节点先被拆除，定时器可能尚有一次回调。
            if not self.query("#clock"):
                return
            await self._refresh_view()

    async def _refresh_view(self) -> None:
        try:
            self.bridge.poll(self._receive_packet, self._error)
            self.connection_info = self.bridge.connection()
            self._notify_first_connection()
        except Exception as error:
            self._error(error_text("vln_cli.VLNConsole / 接收与刷新", error))
        self._render_telemetry()
        signature = tuple((key, message.revision, message.state)
                          for key, message in self.state.messages.items())
        if signature == self._chat_signature:
            return
        messages = list(self.state.messages.items())
        chat = self.query_one("#chat", VerticalScroll)
        follow = chat.is_vertical_scroll_end
        for identity in list(self.bubbles):
            if identity not in self.state.messages:
                await self.bubbles.pop(identity).remove()
        additions = []
        for identity, message in messages:
            if identity not in self.bubbles:
                self.bubbles[identity] = ChatBubble(message)
                additions.append(self.bubbles[identity])
        if additions:
            await chat.mount(*additions)
        for identity, message in messages:
            await self.bubbles[identity].sync(message)
        self._chat_signature = signature
        self.query_one("#welcome").display = not bool(self.state.messages)
        if follow:
            chat.scroll_end(animate=False)

    def _send_input(self, text: str, role="user") -> None:
        inputs = user_input(text, pose=self.bridge.current_pose())
        self.bridge.send(inputs)
        if self.connection_info["mode"] == "ROS 2":
            stamp = json.loads(inputs[0]["content"][0]["text"])["time_stamp"]
            self._pending_user = (stamp, time.monotonic())
        self.state.local_message(role, text)

    def _notify_first_connection(self) -> None:
        info = self.connection_info
        if (self._connection_notice_attempted or info["mode"] != "ROS 2" or
                not info["input_subscribers"] or not info["output_publishers"]):
            return
        # 每个 CLI 进程只尝试一次，刷新与重新匹配都不重复发布。
        self._connection_notice_attempted = True
        self._send_input("CLI 已连接成功。", role="system")

    def _receive_packet(self, packet) -> None:
        if not self.state.apply(packet) or self._pending_user is None:
            return
        stamp, _ = self._pending_user
        for item in (packet.get("request") or {}).get("input", []):
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            content = item.get("content", [])
            if isinstance(content, str):
                content = [{"text": content}]
            if any(stamp in part.get("text", "") for part in content):
                self._pending_user = None
                break

    def _error(self, text: str) -> None:
        if text != self.last_bridge_error:
            self.state.local_message("error", text)
            self.last_bridge_error = text

    def _render_telemetry(self) -> None:
        state = self.state
        info = self.connection_info
        mode = info["mode"]
        connected = info["input_subscribers"] > 0 and info["output_publishers"] > 0
        label = "ROS 已连接" if connected else "等待 ROS 连接"
        color = "#63e0b0" if connected else "#f1c76d"
        if mode != "ROS 2":
            label, color = ("离线演示" if mode == "DEMO" else "只读回放"), "#f1c76d"
        model = state.model_info.get("model", "等待模型信息")
        request_state = state.request_status.get("state", "waiting")
        ribbon = Text(" ● " + label, style=f"bold {color}")
        ribbon.append("    ◇ " + str(model), style="#d4d7dc")
        visible_state = ("已发送，等待 Agent 处理" if self._pending_user and request_state == "idle"
                         else state_name(request_state))
        ribbon.append("    / " + visible_state, style="#a997f6")
        ribbon.append("    · " + str(state.completed_count) + " 次响应", style="#70849f")
        self._set("#ribbon", ribbon)
        self._set("#clock", Text(datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
            "%m-%d  %H:%M:%S  CST"), style="#8fa0b9"))
        errors = state.errors
        self.query_one("#error-banner").display = bool(errors)
        if errors:
            summaries = []
            for item in errors:
                value = item["error"]
                detail = (" · ".join(str(value[key]) for key in ("code", "stage", "message")
                                    if value.get(key) is not None)
                          if isinstance(value, dict) else str(value))
                summaries.append("⚠ " + item["source"] + "：" + short(detail, 160))
            self._set("#error-banner", "\n".join(summaries))
        model_card = Text("模型\n", style="bold #66dfd5")
        model_card.append(str(model) + "\n", style="bold #edf2ff")
        effort = state.model_info.get("reasoning", {}).get("effort", "—")
        model_card.append("推理  " + str(effort) + "     输出上限  " +
                          number(state.model_info.get("max_output_tokens")), style="#a0b1c9")
        self._set("#model-card", model_card)
        context = state.context_status
        usage = context.get("usage") or {}
        used = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        budget = context.get("budget_tokens")
        self._set("#context-label", Text(
            "上下文    " + (f"{used:,} / {budget:,}" if budget else "等待遥测"), style="#b1a0ef"))
        bar = self.query_one("#context-bar", ProgressBar)
        bar.update(total=budget or 100, progress=used)
        cached = usage.get("input_tokens_details", {}).get("cached_tokens")
        self._set_rows("#usage-card", [
            ("输入 / 输出", number(usage.get("input_tokens")) + " / " + number(usage.get("output_tokens"))),
            ("缓存命中", number(cached)),
            ("压缩阈值", number(context.get("compact_threshold"))),
            ("已观察压缩", number(context.get("compaction_count"))),
        ])
        status = state.request_status
        tools = state.tool_status
        self._set_rows("#runtime-card", [
            ("请求状态", visible_state),
            ("本地消息等待", f"{time.monotonic() - self._pending_user[1]:.1f} 秒"
             if self._pending_user else "—"),
            ("本轮耗时", f"{status['request_elapsed_ms'] / 1000:.2f}s"
             if "request_elapsed_ms" in status else "—"),
            ("待发 / 在途输入", number(status.get("pending_input_count")) + " / " +
             number(status.get("current_input_count"))),
            ("工具运行 / 等待", number(tools.get("running_count")) + " / " +
             number(tools.get("waiting_count"))),
            ("重试状态", "需要显式处理" if status.get("retry_required") else "—"),
            ("模型连接", state_name(status.get("transport", {}).get("state"))),
        ])
        self._set_rows("#link-card", [
            ("ROS 输入订阅端", str(info["input_subscribers"])),
            ("ROS 输出发布端", str(info["output_publishers"])),
            ("已显示更新", str(state.packet_count)),
            ("待显示 / 已合并", str(info.get("pending_updates", 0)) + " / " +
             str(info.get("coalesced_updates", 0))),
            ("距最近接收", str(info["last_receive_age_ms"]) + " 毫秒"
             if info.get("last_receive_age_ms") is not None else "—"),
            ("最新响应", short(state.latest_response, 26)),
        ])
        self._render_inspector()

    def _set_rows(self, selector, rows) -> None:
        if self.rendered.get(selector) != rows:
            previous = self.rendered.get(selector)
            self.rendered[selector] = rows
            # 固定行数的遥测卡片只重画内容，避免每个耗时采样重排整段聊天。
            self.query_one(selector, Static).update(
                self._table(rows), layout=previous is None or len(previous) != len(rows))

    @staticmethod
    def _table(rows) -> Table:
        table = Table.grid(padding=(0, 1), expand=True)
        table.add_column(style="#7f92ad")
        table.add_column(style="#d4e1f4", justify="right")
        for title, value in rows:
            table.add_row(title, value)
        return table

    def _render_inspector(self) -> None:
        active = self.query_one("#inspector", TabbedContent).active
        if active == "request-tab":
            self._render_requests()
        elif active == "session-tab":
            errors = "\n\n".join(error_text(
                item["source"], {"field": item["path"], "error": item["error"]})
                for item in self.state.errors) or "当前无错误"
            text = ("错误详情\n" + errors + "\n\n会话状态\n" + describe(self.state.request_status) +
                    "\n\n上下文\n" + describe(self.state.context_status) +
                    "\n\nROS 话题\n" + describe(self.connection_info))
            self._set("#session-detail", Text(text))
        elif active == "tools-tab":
            self._render_tools()

    def _sync_table(self, selector, rows) -> None:
        table = self.query_one(selector, DataTable)
        previous = self._tables.setdefault(selector, {})
        keys = {key for key, _ in rows}
        for key in list(previous):
            if key not in keys:
                table.remove_row(key)
                del previous[key]
        for key, cells in rows:
            if key not in previous:
                table.add_row(*cells, key=key)
            elif previous[key] != cells:
                for column, value in zip(table.columns, cells):
                    table.update_cell(key, column, value)
            previous[key] = cells

    def _render_tools(self) -> None:
        colors = {"running": "#65e4d5", "waiting": "#e8be6d",
                  "finished": "#9ab0c8", "failed": "#ff778c"}
        rows = []
        for identity, call in self.state.calls.items():
            status = call.get("state", "submitted")
            rows.append((identity, (
                Text(state_name(status), style=colors.get(status, "#b5a2f6")),
                Text(tool_name(call.get("name"))), Text(call_duration(call)),
            )))
        self._sync_table("#call-table", rows)
        identity = self.selected_call if self.selected_call in self.state.calls else next(reversed(self.state.calls), None)
        detail = call_text(self.state.calls[identity], detail=True) if identity else "暂无调用记录。收到工具调用后会自动显示。"
        self._set("#call-detail", Text(detail))
        definitions = Text("\n已注册工具\n", style="bold #66dfd5")
        if not self.state.definitions:
            definitions.append("已注册 0 个工具" if self.state.definitions_received else
                               "等待 Agent 推送工具定义。", style="#7e94af")
        for tool in self.state.definitions:
            definitions.append("\n" + tool_name(tool["name"]) + "\n", style="bold #c5b8ff")
            definitions.append(tool.get("description", "") + "\n", style="#8a9db6")
        self._set("#tool-definitions", definitions)

    def _render_requests(self) -> None:
        self._sync_table("#request-table", [
            (key, (Text(f"#{record['sequence']} · {local_time(record['time_stamp'])}"),
                   Text(record["request"].get("model", "未知")), Text(state_name(record["state"]))))
            for key, record in self.state.requests.items()])
        key = self.selected_request if self.selected_request in self.state.requests else next(reversed(self.state.requests), None)
        self._set("#request-detail", Text(request_text(self.state.requests[key]) if key else
                  "暂无请求记录。这里会保留 CLI 启动后观察到的当前和历史请求。"))

    @on(DataTable.RowSelected, "#call-table")
    def select_call(self, event: DataTable.RowSelected) -> None:
        self.selected_call = event.row_key.value
        self._render_tools()

    @on(DataTable.RowSelected, "#request-table")
    def select_request(self, event: DataTable.RowSelected) -> None:
        self.selected_request = event.row_key.value
        self._render_requests()

    @on(TabbedContent.TabActivated, "#inspector")
    def activate_tab(self) -> None:
        self._render_inspector()

    @on(Input.Changed, "#composer")
    def changed_input(self, event: Input.Changed) -> None:
        text = event.value
        matches = [name + "  " + description for name, description in COMMANDS.items()
                   if name.startswith(text)] if text.startswith("/") and not text.startswith("//") else []
        self._set("#input-hint", "   ·   ".join(matches[:2]) if matches else
                  "Enter 发送 · ↑↓ 输入历史 · // 开头发送字面量斜杠")

    @on(Input.Submitted, "#composer")
    async def submit(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        widget = self.query_one("#composer", CommandInput)
        widget.remember(value)
        widget.value = ""
        await self.execute(value)
        await self.refresh_tick()

    def _show_tab(self, tab: str) -> None:
        self.sidebar_hidden = False
        self._layout()
        # 避免旧页面中仍聚焦的表格滚动事件将标签切回去。
        self.query_one("#composer", Input).focus()
        self.query_one("#inspector", TabbedContent).active = tab
        self._render_inspector()

    async def execute(self, line: str) -> None:
        if not line.startswith("/") or line.startswith("//"):
            text = line[1:] if line.startswith("//") else line
            try:
                self._send_input(text)
                if self.connection_info["mode"] == "ROS 2" and not self.connection_info["input_subscribers"]:
                    self.state.local_message("system",
                        "当前输入话题没有匹配订阅者，本次发布不会自动重发。连接后请重新发送。")
            except Exception as error:
                self.state.local_message("error", error_text("vln_cli.VLNConsole / 输入发送", error))
            return
        try:
            args = shlex.split(line)
            command = args[0].lower()
            reply = ""
            if command in ("/quit", "/exit"):
                self.exit()
                return
            if command == "/help":
                reply = "### 指令\n\n" + "\n".join(
                    f"- `{name}`：{description}" for name, description in COMMANDS.items())
                reply += "\n\n**快捷键**：Enter 发送；↑↓ 历史；Ctrl+B 侧栏；Ctrl+L 清屏；Ctrl+K 输入；Ctrl+Q 退出。"
            elif command == "/model":
                if len(args) != 1:
                    raise ValueError("/model 只查看信息；现有话题协议不提供切换模型操作。")
                info = self.state.model_info
                reply = ("### 模型配置\n\n" + describe(info) if info else
                         "尚未收到模型信息，等待 Agent 推送状态；也可发送普通消息以观察下一次请求。")
                self._show_tab("overview-tab")
            elif command in ("/status", "/session"):
                self._show_tab("session-tab")
                reply = "会话、连接和错误详情已显示在「状态」面板。"
            elif command == "/request":
                self._show_tab("request-tab")
                reply = f"已保留 {len(self.state.requests)} 次请求。双击记录或按 Enter 查看中文详情。"
            elif command == "/tools":
                self._show_tab("tools-tab")
                reply = f"已收到 {len(self.state.definitions)} 个工具定义、{len(self.state.calls)} 条调用记录。双击调用或按 Enter 查看详情。"
            elif command == "/topics":
                reply = "### ROS 话题\n\n" + describe(self.connection_info)
            elif command == "/clear":
                self.state.clear_chat()
                reply = "本地聊天已清空；模型上下文保持不变。"
            elif command == "/export":
                if len(args) > 2:
                    raise ValueError("用法：/export [文件.json]")
                path = Path(args[1] if len(args) == 2 else
                            datetime.now().strftime("vln-session-%Y%m%d-%H%M%S.json")).expanduser()
                with path.open("x", encoding="utf-8") as file:
                    snapshot = self.state.snapshot()
                    snapshot["cli_connection"] = self.bridge.connection()
                    json.dump(snapshot, file, ensure_ascii=False, indent=2)
                reply = "视图已导出到：" + str(path.resolve())
            else:
                raise ValueError("未知指令：" + command + "。输入 /help 查看可用指令。")
            self.state.local_message("system", reply)
        except (ValueError, OSError) as error:
            self.state.local_message("error", error_text(
                "vln_cli.VLNConsole / 本地指令 " + line.split()[0], error))
