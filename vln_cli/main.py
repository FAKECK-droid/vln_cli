import argparse
import sys

from .app import VLNConsole
from .bridge import DemoBridge, ReplayBridge, RosBridge
from .presentation import error_text


def main(args=None):
    parser = argparse.ArgumentParser(description="VLN Agent · ROS 2 终端控制台")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="离线演示，不连接 ROS 或模型")
    mode.add_argument("--replay", metavar="JSON", help="只读回放 interaction_update 记录")
    parser.add_argument("--input-topic", default="/vln/user_input", help="向 Agent 发布用户输入的话题")
    parser.add_argument("--output-topic", default="/vln/user_output", help="从 Agent 接收汇总输出的话题")
    parser.add_argument("--history-limit", type=int, default=200, help="本地聊天及工具记录数量上限")
    options, ros_args = parser.parse_known_args(args)
    if options.history_limit < 1:
        parser.error("--history-limit 必须大于零")
    if (options.demo or options.replay) and ros_args:
        parser.error("离线模式不接收 ROS 参数")
    bridge = None
    source = ("DemoBridge" if options.demo else "ReplayBridge" if options.replay else "RosBridge")
    source = "vln_cli." + source + " / 初始化"
    try:
        if options.demo:
            bridge = DemoBridge()
        elif options.replay:
            bridge = ReplayBridge(options.replay)
        else:
            bridge = RosBridge(options.input_topic, options.output_topic,
                               options.history_limit, ros_args)
        source = "vln_cli.VLNConsole / 运行"
        VLNConsole(bridge, history_limit=(
            options.history_limit if options.demo or options.replay else bridge.history_limit)).run()
    except (ImportError, OSError, ValueError, RuntimeError) as error:
        print(error_text(source, error), file=sys.stderr)
        return 1
    finally:
        if bridge is not None:
            bridge.close()
    return 0
