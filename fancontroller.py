"""
IPMI 风扇控制器 - 循环执行模式

此脚本持续运行，定期监控温度并调整风扇转速。
适合作为后台服务或 systemd 服务运行。

使用场景：
- 作为后台进程持续运行
- 通过 systemd 服务管理
- 需要实时响应温度变化的场景

优势：
- 实时监控，响应及时
- 保持上下文状态，避免重复初始化
- 适合长期运行的服务器环境
"""

import os
import threading
import logging
from logging.handlers import TimedRotatingFileHandler
import yaml

from fanController.dell730_controller import Dell730FanController
from fanController.epycd8_controller import Epycd8FanController


#: 机型 → 控制器类 的映射。
#: 配置里 ``servers[].type`` 填什么，就分发到哪个控制器 —— 新增机型时在这里
#: 登记一行即可，下面的分发逻辑不用动。
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
        input("按任意键退出程序：")
        return
    except yaml.YAMLError as e:
        print(f"错误：配置文件格式错误，请检查配置后重新打开。\n详细信息：{e}")
        input("按任意键退出程序：")
        return

    # --- 日志配置 ---
    log_backup_count = data.get('log_backup_count', 30)  # 从配置读取日志保留天数，默认为30
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
    formatter = logging.Formatter('%(asctime)s - %(threadName)s - %(message)s')
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)

    # 添加处理器到logger
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    # --- 启动控制器（循环模式）---
    logger.info("循环控制模式启动")
    servers = data['servers']
    windows_ipmi_tool_path = data['windows_ipmi_tool_path']
    interval = data['interval']
    alert_config = data.get('alert', {})  # 获取告警配置
    prometheus_config = data.get('prometheus', {})  # Prometheus 数据源配置
    threads = []

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
            auto=True,  # 循环模式
            alert_config=alert_config,
            prometheus_config=prometheus_config,
        )
        thread = threading.Thread(target=fan_controller.start_fan_control, name=f"Thread-{server['ip']}")
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()

if __name__ == '__main__':
    main()
