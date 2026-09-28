"""
IPMI 风扇控制器 - 单次执行模式

此脚本执行一次温度检测和风扇调整后退出。
适合被外部调度工具（cron、systemd timer、Windows 任务计划程序等）定时调用。

使用场景：
- 由 cron 或 systemd timer 每隔 N 分钟调用一次
- 由外部监控系统触发执行
- 集成到其他自动化工作流中

优势：
- 更灵活的调度控制
- 便于集成到现有的任务调度系统
- 执行失败不会影响后续调度
"""

import os
import logging
from logging.handlers import TimedRotatingFileHandler
import yaml

from fanController.dell730_controller import Dell730FanController
from fanController.epycd8_controller import Epycd8FanController


#: 机型 → 控制器类 的映射（与 fancontroller.py 保持一致）。
CONTROLLER_TYPES = {
    'dell730': Dell730FanController,
    'epycd8': Epycd8FanController,
}


def main():
    # --- 路径设置 ---
    current_directory = os.path.dirname(os.path.abspath(__file__))
    log_directory = os.path.join(current_directory, 'logs')
    os.makedirs(log_directory, exist_ok=True)
    log_file_path = os.path.join(log_directory, 'fancontroller.log')

    # --- 读取配置 ---
    config_file_path = os.path.join(current_directory, 'fan_settings.yaml')
    try:
        with open(config_file_path, 'r', encoding='utf-8') as file:
            data = yaml.safe_load(file)
    except FileNotFoundError:
        print(f"错误：配置文件 'fan_settings.yaml' 未找到。")
        return
    except yaml.YAMLError as e:
        print(f"错误：配置文件格式错误，请检查配置后重新打开。\n详细信息：{e}")
        return

    # --- 日志配置 ---
    log_backup_count = data.get('log_backup_count', 30)
    logger = logging.getLogger('FanController')
    logger.setLevel(logging.INFO)

    # 文件处理器 (按天轮转)
    file_handler = TimedRotatingFileHandler(
        log_file_path,
        when='midnight',
        interval=1,
        backupCount=log_backup_count,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)

    # 控制台处理器
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)

    # 日志格式
    formatter = logging.Formatter('%(asctime)s - %(message)s')
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)

    # 添加处理器到logger
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    # --- 执行单次控制 ---
    logger.info("单次执行模式启动")
    servers = data['servers']
    windows_ipmi_tool_path = data['windows_ipmi_tool_path']
    interval = data.get('interval', 60)  # 单次模式不使用 interval，但保留参数兼容性
    alert_config = data.get('alert', {})  # 获取告警配置
    prometheus_config = data.get('prometheus', {})  # Prometheus 数据源配置

    for server in servers:
        controller_class = CONTROLLER_TYPES.get(server['type'])
        if controller_class is None:
            logger.warning(
                f"未知的服务器类型 {server['type']!r}（{server.get('ip')}），已跳过。"
                f"当前支持的机型: {', '.join(sorted(CONTROLLER_TYPES))}"
            )
            continue

        fan_controller = controller_class(
            servers=server,
            interval=interval,
            windows_ipmi_tool_path=windows_ipmi_tool_path,
            logger=logger,
            auto=False,  # 单次模式不需要 auto 参数
            alert_config=alert_config,
            prometheus_config=prometheus_config,
        )
        # 执行一次风扇控制
        fan_controller.run_once()

    logger.info("单次执行完成")


if __name__ == '__main__':
    main()
