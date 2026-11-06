# vln_cli

独立 ROS 2 终端控制台，通过 `RosTopicInterface` 的两个 `std_msgs/msg/String` 话题与 Agent 交互，并监听 `/tf` 和 `/tf_static` 获取小车位姿。
不链接、导入或修改 `vln_agent`，不读取 API 密钥，也不直接访问模型。

- 深色青紫配色、流式 Markdown 聊天、中文输入、输入历史、指令补全和窄终端布局。
- 中文模型、上下文、连接和状态详情；保留模型名、原始函数名等技术标识。
- 工具历史保留 Agent 的原始函数名、参数/结果字段名和字段值，嵌套字段与新增字段动态展示；工具说明直接使用 tools 的 description。
- “调用参数”“返回结果”等界面标题使用中文，双击记录或按 Enter 查看详情。
- 对话中每个 call_id 对应一张工具卡片，从正在执行更新为完成/失败，显示文字结果并省略图片。
- 请求历史按响应标识关联完成事件，支持旧响应完成与下一请求开始同批到达；缺少终态证据时标为“结束状态未收到”，不永久显示等待。
- 空闲后仍可双击或按 Enter 查看模型配置、输入和耗时；工具详情分别展示排队耗时 wait_ms 和执行耗时 run_ms，0 毫秒是有效值。
- 工具最终耗时直接来自 Agent 的 tools.finished_calls.run_ms/wait_ms；快速调用无需活动队列采样。完成通知必须包含完整终态字段，缺失字段按协议错误报告。
- 错误标明组件：SessionManager 会话请求、ResponsesWsTransport 模型 WebSocket、ToolManager 与具体工具函数，以及 CLI 的 ROS 接收、TF、协议解析、发送和本地指令。
- 会话与传输错误同时显示，完整字段及原始异常类型/信息保留在聊天和 /session 详情中；重复遥测不重复刷屏。工具失败卡显示 ToolManager、函数名、call_id 和原始返回内容。
- 不记录原始收发包，不提供历史 JSON 页面；界面状态使用中文，工具参数和结果保留原始字段。
- ROS 由独立接收线程处理，TUI 以 10 Hz 刷新；连续重复遥测只保留最新采样，调用/结果/请求边界不合并；正文始终是当前位置的全文。
- 发送后立即显示“已发送，等待 Agent 处理”；观察到对应请求后才切换到真实模型状态。
- 每次 CLI 启动首次匹配 Agent 的输入/输出端点后，向 Agent 发送一次“CLI 已连接成功。”提示输入；刷新与断线重连不重复发送，演示/回放模式不发送。
- 每次发送用户输入（含连接提示）都附加发送时查询的 `map → base_link` 二维 `pose`；TF 缺失、断链或过期时发送 `pose: null`，不阻塞发送。
- 在途/失败请求和原始状态 JSON，支持离线演示与已有话题记录回放。
- 聊天、工具和请求历史分别保留最近 200 条，可通过 history_limit 调整；活动工具另外保留。
- 历史只保存在 CLI 进程内存中，退出后清空；/export 可手动导出，没有自动落盘或重启恢复。
- 周期遥测只更新当前状态；工具和请求按调用/请求合并，不按心跳新增。

## 安装与运行

ROS 2 Humble 使用系统 Python 3.10。Textual 通过 Python 依赖安装，不需要改 Agent 的环境。

```bash
cd ~/scout_mini_ws  # 改为你的工作空间
source /opt/ros/humble/setup.bash
python3 -m venv --system-site-packages .venv-vln-cli
touch .venv-vln-cli/COLCON_IGNORE
source .venv-vln-cli/bin/activate
python -m pip install -r src/scout_mini_vln/vln_cli/requirements.txt
python -m colcon build --base-paths src/scout_mini_vln/vln_cli --packages-select vln_cli
source install/setup.bash
ros2 run vln_cli vln_cli
```

使用 `python -m colcon`，让安装入口绑定到装有 Textual 的同一 Python 环境。
终端建议至少 110×35，支持真彩色并使用可显示中文的等宽字体；较窄时侧栏自动收起。

```bash
ros2 run vln_cli vln_cli --input-topic /vln/user_input --output-topic /vln/user_output
ros2 run vln_cli vln_cli --ros-args --params-file \
  "$(ros2 pkg prefix vln_cli)/share/vln_cli/config/cli.yaml"
ros2 run vln_cli vln_cli --demo
ros2 run vln_cli vln_cli --replay /path/to/result.json
```

演示和回放也可在安装 Python 包后运行 `python -m vln_cli --demo`，不需要 ROS 环境。
回放文件固定为含 `packets` 数组的 JSON 对象；回放不会发布输入。

## 与现有 Agent 对接

CLI 默认发布 `/vln/user_input`，订阅 `/vln/user_output`；
自定义话题时与已有 RosTopicInterface 的 `receive_topic/send_topic` 保持对应。
跨机器须使用兼容的 ROS_DOMAIN_ID、RMW 和可达的 DDS 网络；CLI 不提供网络代理。

CLI 与 vln_agent 必须同时更新到当前协议。以下开关决定 CLI 能看到哪些信息：

- `transmit.request`：模型、推理配置和当前请求。
- `transmit.response.messages/stream`：完整/流式聊天。
- `transmit.response.function_calls/tool_outputs`：函数调用与结果。
- `transmit.tools.definitions/status/queue/finished_calls`：工具详情；工具完成状态和最终计时必须启用 finished_calls。
- `transmit.session.request_status/context_status`：会话、错误、用量和压缩状态。
- `include_images: false`：终端建议不传图片。CLI 不渲染或上传图片。

每轮发送全部已启用状态和工具定义，正文按 response_id/output_index/content_index 直接覆盖为当前全文。
协议没有 full/op 字段；根据时间戳和回退的 batch_id 识别 Agent 重启。
未启用的字段省略，null 清除当前状态，空事件数组不清空已收集的历史。
finished_calls 必须包含 call_id/name/sequence/blocking/state/run_ms/wait_ms。

话题使用 volatile QoS，CLI 加入后接收下一次实时推送；CLI 不补发模型请求以获取历史。
尚未观察过请求时，/model 显示等待模型信息；当前状态并不包含 CLI 离线期间的完整请求历史。
本包不为补充遥测自动发送模型请求。空字段显示为“未收到”，不会猜测模型。
ROS 匹配端点与模型 WebSocket 连接分别显示；ROS 已连接不代表模型服务已连接。

发布的 JSON 文本是原生 input 数组：

```json
[{"role":"user","content":[{"type":"input_text","text":"{\"source\":\"user\",\"time_stamp\":\"2026-10-28T06:00:00.000Z\",\"instruction\":\"检查门口\"}"}]}]
```

位姿放在上述 `input_text.text` 内的业务 JSON 中，与 `instruction` 同级，遵循 `vln_agent/docs/AGENT_PLAN.md` 第 2.2 节：

```json
{"source":"user","time_stamp":"2026-10-29T06:00:00.123Z","instruction":"检查门口","pose":{"frame_id":"map","x":1.2,"y":2.3,"yaw":0.5,"time_stamp":"2026-10-29T06:00:00.100Z"}}
```

`x/y` 单位为米，`yaw` 单位为弧度。TF2 自动组合 `map → odom → base_link` 等变换链；每次发送重新查询缓存，不等待变换，也不周期发送位姿消息。
默认 ROS 参数为 `pose_frame: map`、`pose_base_frame: base_link`、`pose_max_age_sec: 2.0`。
非零 TF 时间戳与节点 ROS 时钟的偏差超过该阈值时，位姿视为不可用；跨机器须同步 ROS 时钟。
可用 `--ros-args -p pose_max_age_sec:=5.0` 调整；配置文件也已包含这三项参数。
真实时钟下附加 TF 的 UTC 毫秒采样时间；`use_sim_time:=true` 或静态 TF 零时间时省略位姿采样时间，不把仿真时刻当作 UTC。
位姿不可用会提示一次，恢复有效后允许再次提示；演示/回放没有真实位姿。

接收端按 response_id 和文本位置合并消息、按 call_id 合并工具记录。重复快照不新增对话卡片或重复统计完成数。
batch_id 允许跳号，因为管理器空轮询也会递增计数；重复/过期批次不会再次拼接。
工具 run_ms 可能只是活动队列的最后采样；完成时未上报最终时长则显示“未上报”或采样下界，不伪装为总耗时。

## 指令与快捷键

| 指令 | 行为 |
|---|---|
| /help | 指令与快捷键 |
| /model | 最近观察到的模型及推理、输出、缓存相关配置，只查看 |
| /status、/session | 会话、连接、上下文和错误详情 |
| /tools | 工具定义、当前队列与最近结果 |
| /request | 当前和历史请求的中文详情 |
| /topics | 话题名称与匹配端点数 |
| /clear | 仅清空本地聊天，不清空模型上下文 |
| /export [文件.json] | 手动导出当前视图、工具/请求历史及收发计数，不包含原始收发包；不覆盖已有文件 |
| /quit、/exit | 退出 CLI，不终止 Agent |

Enter 发送，↑↓ 输入历史，Ctrl+B 切换侧栏，Ctrl+K 聚焦输入，Ctrl+L 清屏，F1 帮助，Ctrl+Q 退出。
`//text` 会发送字面量 `/text`。
当前话题协议没有切换模型、重试、停止任务或重置会话的控制通道，本包不伪造这些操作。
普通用户输入仍可能让 Agent 调用其已装载工具，其行为由 Agent 的系统提示词和工具配置决定。

## 测试

```bash
source /opt/ros/humble/setup.bash
source .venv-vln-cli/bin/activate
python -m pip install "pytest>=8.2,<9" pytest-asyncio
python -m pytest src/scout_mini_vln/vln_cli/test
```

测试覆盖全量/差量合并、重启和清屏、历史窗口、工具生命周期、图片省略、中文展示、双击/Enter 选择和界面缩放；
ROS 测试只在本机发布合成 JSON，不调用模型或机器人。
