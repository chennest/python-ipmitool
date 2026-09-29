import platform
import subprocess
import time
import logging
from datetime import datetime

from utils.prometheus_client import PrometheusClient, PrometheusError


class IPMIFanController:
    def __init__(self, servers, interval, windows_ipmi_tool_path, logger, auto=True,
                 alert_config=None, prometheus_config=None):
        """
        初始化 IPMI 风扇控制器。

        Args:
            servers (dict): 包含服务器信息的字典。
            interval (int): 检查 CPU 温度的时间间隔（秒）。
            windows_ipmi_tool_path (str): Windows 平台上 IPMI 工具的路径。
            logger (logging.Logger): 配置好的日志记录器实例。
            auto (bool): 是否自动模式，True为自动模式，False为手动模式。
            alert_config (dict, optional): 告警配置字典。
            prometheus_config (dict, optional): Prometheus 查询配置，形如
                ``{'base_url': 'http://192.0.2.20:30091', 'instance': '192.0.2.10:9290'}``。
                风扇转速统一从这里取（见 :meth:`get_fan_rotational_speed`）。
        """
        self.platform_system = platform.system()
        if self.platform_system == 'Windows':
            self.ipmi_tool_path = windows_ipmi_tool_path
        else:
            self.ipmi_tool_path = 'ipmitool'
        self.interval = interval
        self.servers = servers
        self.logger = logger
        self.ip = self.servers['ip']
        self.user = self.servers['user']
        self.password = self.servers['password']
        self.auto = auto

        # 统计信息
        self.start_time = None
        self.adjustment_count = 0
        self.last_cpu_temp = None
        self.last_check_time = None

        # 告警配置
        self.alert_config = alert_config or {}
        self.alert_enabled = self.alert_config.get('enabled', False)
        self.fan_speed_threshold = self.alert_config.get('fan_speed_threshold', 10000)
        self.max_failed_attempts = self.alert_config.get('max_failed_attempts', 3)
        self.failed_attempts = 0
        self.last_alert_time = None
        self.email_notifier = None

        # 初始化邮件通知器（如果启用）
        if self.alert_enabled:
            try:
                from utils.email_notifier import EmailNotifier
                email_config = self.alert_config.get('email', {})
                self.email_notifier = EmailNotifier(email_config, logger)
                self.logger.info(f"服务器 {self.ip}: 邮件告警功能已启用 (阈值: {self.fan_speed_threshold} RPM, 失败次数: {self.max_failed_attempts})")
            except Exception as e:
                self.logger.error(f"初始化邮件通知器失败: {str(e)}")
                self.alert_enabled = False

        # --- Prometheus 数据源（风扇转速统一从这里取，见 get_fan_rotational_speed）---
        # 全局配置 + servers 项内的 per-server 覆盖：多机场景下每台机器的
        # instance 标签不同，必须逐台指定，否则会把别的机器的转速当成自己的。
        self.prometheus_config = dict(prometheus_config or {})
        server_override = self.servers.get('prometheus') or {}
        if server_override:
            self.prometheus_config.update(server_override)

        self.prometheus_client = None
        base_url = self.prometheus_config.get('base_url')
        if base_url:
            self.prometheus_client = PrometheusClient(
                base_url=base_url,
                timeout=self.prometheus_config.get('timeout', 10),
            )
            self.logger.info(f"服务器 {self.ip}: 风扇转速数据源 = Prometheus ({base_url})")
        else:
            self.logger.warning(
                f"服务器 {self.ip}: 未配置 prometheus.base_url，风扇转速将无法获取"
            )

    def send_command(self, cmd_in):
        """
        发送命令到系统 Shell。

        Args:
            cmd_in (str): 要执行的命令。

        Returns:
            str: 命令的输出。
        """
        p = subprocess.Popen(cmd_in, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             universal_newlines=True, stderr=subprocess.STDOUT, close_fds=True)
        return p.stdout.read()

    def ipmi_command(self, cmd_in):
        """
        执行 IPMI 命令。

        Args:
            cmd_in (str): 要执行的 IPMI 命令。

        Returns:
            str: IPMI 命令的输出。
        """
        command = f'{self.ipmi_tool_path} {cmd_in}'
        return self.send_command(command)

    def set_fan_speed(self, fan_index, percentage):
        """
        设置指定风扇的转速。

        Args:
            fan_index (int): 要设置速度的风扇索引。
            percentage (int): 要设置的风扇转速百分比。

        Raises:
            NotImplementedError: 子类必须实现此方法。
        """
        raise NotImplementedError("Method set_fan_speed must be implemented by subclasses")

    def _pick_instance(self, *keys):
        """从 prometheus 配置里按顺序取第一个非空的 instance 标签。

        ⚠️ **不同数据源的 instance 是不同的**，因为它们是各自的 exporter：

        ============  ==================  ==========================
        数据          指标                instance（实测示例）
        ============  ==================  ==========================
        风扇转速       ipmi_fan_speed_rpm  ``192.0.2.10:9290``
        CPU 温度       node_hwmon_temp_*   ``192.0.2.10:9100``
        GPU 温度       DCGM_FI_DEV_*       ``192.0.2.10:9400``
        ============  ==================  ==========================

        混用一个 ``instance`` 会直接查不到数据 —— 这个坑 2026-09-28 实现时踩到。
        配置里推荐分别写 ``fan_instance`` / ``temp_instance`` / ``gpu_instance``；
        为兼容单数据源场景，仍接受笼统的 ``instance`` 作为兜底。
        """
        for key in keys:
            value = self.prometheus_config.get(key)
            if value:
                return value
        return None

    #: 温度查询里用作语义过滤的传感器标签。
    #: AMD ``k10temp`` 与 Intel ``coretemp`` 的 CPU 核心温度标签都是 ``Tctl``。
    #: 子类可覆盖本常量，或直接覆盖 :meth:`_build_temperature_query`。
    TEMPERATURE_SENSOR_LABEL = "Tctl"

    def _build_temperature_query(self):
        """组装温度查询语句（PromQL）。子类可覆盖以换温度源。

        默认查 **CPU 核心温度**，且刻意用**语义标签** ``Tctl`` 过滤，而不是
        按 hwmon 的 chip 名 —— k10temp 在 Prometheus 里的 chip 名是 PCI 路径
        形式（``pci0000:00_0000:00:18_3``），硬编码它换台机器就失效了。
        用 chip 名反查的那个坑 2026-09-28 实测踩过一次。

        Returns:
            str | None: PromQL 语句；返回 ``None`` 表示无法构造（缺配置）。
        """
        instance = self._pick_instance('temp_instance', 'instance')
        selectors = [f'label="{self.TEMPERATURE_SENSOR_LABEL}"']
        if instance:
            selectors.append(f'instance="{instance}"')
        label_filter = "{" + ",".join(selectors) + "}"
        return (
            "node_hwmon_temp_celsius * on(chip, sensor) group_left(label) "
            f"node_hwmon_sensor_label{label_filter}"
        )

    def get_cpu_temperature(self):
        """获取温度（°C 列表）—— 统一走 Prometheus，与机型无关。

        2026-09-28 改造：与风扇转速同样的思路，「读」这一层收归基类。
        数据源是 **node_exporter 的 hwmon collector**（``node_hwmon_temp_celsius``）
        —— 它读的就是内核 hwmon（``/sys/class/hwmon/``），与直接读 sysfs
        是同一份数据，这里只是换成了 Prometheus 指标这一层封装。

        ⚠️ **延迟提醒**：拿到的是 Prometheus 上一次 scrape 的快照。pve 各
        target 的 ``scrape_interval`` 实测为 **30s**，也就是温度最多滞后 30 秒。
        对控速决策而言这是明显滞后 —— 建议把这些 target 的抓取间隔调小，
        node_exporter 采集很轻，调到 10s 毫无压力。

        Returns:
            list: 温度列表（°C）。取不到时返回**空列表**，不抛异常。
        """
        if self.prometheus_client is None:
            self.logger.error(
                f"服务器 {self.ip}: 未配置 Prometheus 数据源，无法获取温度"
            )
            return []

        promql = self._build_temperature_query()
        if not promql:
            return []

        try:
            samples = self.prometheus_client.query(promql)
        except PrometheusError as e:
            self.logger.error(f"服务器 {self.ip}: 从 Prometheus 获取温度失败: {e}")
            return []

        temperatures = [s.value for s in samples]
        if not temperatures:
            self.logger.warning(
                f"服务器 {self.ip}: Prometheus 中没有温度数据（查询: {promql}）—— "
                f"确认目标机的 node_exporter 已开启 --collector.hwmon 并接入 Prometheus"
            )
        return temperatures

    def set_ipmi_manual_mode(self):
        """
        设置 IPMI 为手动模式。

        Returns:
            str: IPMI 命令的输出。
        """
        command = f'-I lanplus -H {self.ip} -U {self.user} -P {self.password} raw 0x30 0x30 0x01 0x00'
        return self.ipmi_command(command)

    def get_fan_rotational_speed(self):
        """获取风扇转速 —— 统一走 Prometheus（数据源是 ipmi_exporter）。

        2026-09-28 改造说明：原先「读转速」下放到各机型子类，各自执行
        ``ipmitool sdr type fan`` 再解析文本。三个问题：

        1. **输出格式各机型不一致** —— Dell 与 ASRock Rack 的 sdr 列结构完全
           两套，每加一个机型就要重写一遍解析（还容易写错，见 sensors.py 里
           那个把传感器 ID 当成 RPM 的踩坑记录）
        2. **每轮都要 spawn 一个 ipmitool 进程**，还要管 BMC 连接
        3. **读数口径可能与看板对不上** —— 控制器一个值、Grafana 另一个值

        改成统一查 Prometheus 后，「读」这一层与机型无关了，子类只需负责
        「怎么写」（见 :meth:`set_fan_speed`）。

        ⚠️ **代价**：拿到的是上一次 scrape 的快照（延迟由 Prometheus 的
        ``scrape_interval`` 决定），且 Prometheus 不可用时完全读不到数据。

        Returns:
            list: 风扇转速（RPM）列表。取不到时返回**空列表**，不抛异常 ——
            调用方必须自行处理空结果。
        """
        if self.prometheus_client is None:
            self.logger.error(
                f"服务器 {self.ip}: 未配置 Prometheus 数据源，无法获取风扇转速"
            )
            return []

        try:
            speeds = self.prometheus_client.get_fan_speeds(
                instance=self._pick_instance('fan_instance', 'instance')
            )
        except PrometheusError as e:
            self.logger.error(f"服务器 {self.ip}: 从 Prometheus 获取风扇转速失败: {e}")
            return []

        if not speeds:
            self.logger.warning(
                f"服务器 {self.ip}: Prometheus 中没有风扇转速数据 —— "
                f"请确认该机器的 ipmi_exporter 已接入，且 prometheus.instance "
                f"标签配置正确"
            )
        return speeds

    def adjust_fans_once(self, prev_temp_ranges=None, prev_fan_speeds=None):
        """
        单次检测温度并调整风扇转速（不循环）。

        Args:
            prev_temp_ranges (tuple, optional): 之前的温度范围。
            prev_fan_speeds (list, optional): 之前的风扇转速。

        Returns:
            dict: 包含当前状态信息的字典 {
                'temp_ranges': (min_temp, max_temp) or None,
                'fan_speeds': [速度列表] or None,
                'cpu_temp': 当前最高CPU温度,
                'max_fan_speed': 当前最高风扇转速
            }
        """
        check_start_time = time.time()
        cpu_temps = self.get_cpu_temperature()
        current_fan_speeds = self.get_fan_rotational_speed()

        result = {
            'temp_ranges': None,
            'fan_speeds': None,
            'cpu_temp': None,
            'max_fan_speed': None
        }

        if cpu_temps:
            max_temp_value = max(cpu_temps)
            min_temp_value = min(cpu_temps)
            avg_temp_value = sum(cpu_temps) // len(cpu_temps)
            # ⚠️ 空值保护：改用 Prometheus 取数后，「查不到数据」是正常情况
            # （exporter 未接入 / Prometheus 挂了 / instance 标签配错）。
            # 上游在这里直接 max([]) 会 ValueError 把整个控制线程打挂，
            # 进程还活着但已经不再控风扇 —— 典型的静默失效。
            if current_fan_speeds:
                max_fan_speed = max(current_fan_speeds)
                min_fan_speed = min(current_fan_speeds)
            else:
                max_fan_speed = 0
                min_fan_speed = 0
                self.logger.warning(
                    f"服务器 {self.ip}: 本轮没有风扇转速数据，跳过转速相关判断"
                )
            result['cpu_temp'] = max_temp_value
            result['max_fan_speed'] = max_fan_speed

            # 计算温度变化
            temp_change_str = ""
            if self.last_cpu_temp is not None:
                temp_delta = max_temp_value - self.last_cpu_temp
                if temp_delta > 0:
                    temp_change_str = f" | 变化: +{temp_delta}°C ↑"
                    if temp_delta >= 10:
                        temp_change_str += " [警告: 温度快速上升!]"
                elif temp_delta < 0:
                    temp_change_str = f" | 变化: {temp_delta}°C ↓"
                else:
                    temp_change_str = f" | 变化: 持平 →"

            # 计算实际检测间隔
            interval_str = ""
            if self.last_check_time is not None:
                actual_interval = time.time() - self.last_check_time
                interval_str = f" | 检测间隔: {actual_interval:.1f}秒"

            # 详细日志：显示所有 CPU 温度和风扇转速
            cpu_temps_str = ', '.join([f"{temp}°C" for temp in cpu_temps])
            fan_speeds_str = ', '.join([f"{speed} RPM" for speed in current_fan_speeds])

            self.logger.info("=" * 80)
            self.logger.info(f"服务器 {self.ip} 状态检测:")
            self.logger.info(f"  CPU 温度: [{cpu_temps_str}]")
            self.logger.info(f"    └─ 最低: {min_temp_value}°C | 平均: {avg_temp_value}°C | 最高: {max_temp_value}°C{temp_change_str}")
            self.logger.info(f"  风扇转速: [{fan_speeds_str}]")
            self.logger.info(f"    └─ 最低: {min_fan_speed} RPM | 最高: {max_fan_speed} RPM")

            # 温度过高警告
            temp_threshold_warning = 75  # 可配置的警告阈值
            temp_threshold_critical = 85  # 可配置的严重阈值
            if max_temp_value >= temp_threshold_critical:
                self.logger.warning(f"  ⚠️  严重警告: CPU 温度过高 ({max_temp_value}°C >= {temp_threshold_critical}°C)!")
            elif max_temp_value >= temp_threshold_warning:
                self.logger.warning(f"  ⚠️  警告: CPU 温度较高 ({max_temp_value}°C >= {temp_threshold_warning}°C)")

            # 风扇转速异常检测（告警前的检查）
            if max_fan_speed >= self.fan_speed_threshold:
                self.logger.warning(f"  ⚠️  风扇转速异常: {max_fan_speed} RPM (阈值: {self.fan_speed_threshold} RPM)")

                # 如果启用告警，检查是否需要发送告警邮件
                if self.alert_enabled:
                    self.failed_attempts += 1
                    self.logger.warning(f"  风扇调节失败计数: {self.failed_attempts}/{self.max_failed_attempts}")

                    # 连续失败次数达到阈值，发送告警邮件
                    if self.failed_attempts >= self.max_failed_attempts:
                        # 避免频繁发送邮件，至少间隔1小时
                        current_time = time.time()
                        should_send = True
                        if self.last_alert_time is not None:
                            time_since_last_alert = current_time - self.last_alert_time
                            if time_since_last_alert < 3600:  # 1小时
                                should_send = False
                                self.logger.info(f"  距离上次告警仅 {time_since_last_alert/60:.1f} 分钟，跳过邮件发送")

                        if should_send and self.email_notifier:
                            self.logger.warning(f"  ⚠️⚠️⚠️  连续 {self.failed_attempts} 次调节失败，发送告警邮件！")
                            subject = f"🚨 IPMI 风扇控制器告警 - 服务器 {self.ip}"
                            success = self.email_notifier.send_alert(
                                subject=subject,
                                server_ip=self.ip,
                                cpu_temps=cpu_temps,
                                fan_speeds=current_fan_speeds,
                                failed_attempts=self.failed_attempts,
                                threshold=self.fan_speed_threshold
                            )
                            if success:
                                self.last_alert_time = current_time
                                # 发送成功后重置计数器，避免重复告警
                                self.failed_attempts = 0
            else:
                # 风扇转速正常，重置失败计数
                if self.failed_attempts > 0:
                    self.logger.info(f"  风扇转速已恢复正常 ({max_fan_speed} RPM < {self.fan_speed_threshold} RPM)，重置失败计数")
                    self.failed_attempts = 0

            for temp_range in self.servers['temperature_ranges']:
                min_temp = temp_range['min_temp']
                max_temp = temp_range['max_temp']
                fan_speeds = temp_range['fan_speeds']

                if min_temp <= max_temp_value <= max_temp:
                    if (prev_temp_ranges == (min_temp, max_temp)) and (prev_fan_speeds == fan_speeds) and max_fan_speed < 15000:
                        self.logger.info(f"  动作: 温度在范围 [{min_temp}-{max_temp}°C] 内，风扇转速保持不变")
                        result['temp_ranges'] = (min_temp, max_temp)
                        result['fan_speeds'] = fan_speeds
                    else:
                        fan_speeds_percent_str = ', '.join([f"{speed}%" for speed in fan_speeds])
                        self.logger.info(f"  动作: 温度 {max_temp_value}°C 在范围 [{min_temp}-{max_temp}°C]，设置风扇转速为 [{fan_speeds_percent_str}]")

                        for fan_index, speed in enumerate(fan_speeds):
                            self.set_fan_speed(fan_index, speed)
                            time.sleep(1)

                        self.adjustment_count += 1
                        self.logger.info(f"  风扇调整完成 (总调整次数: {self.adjustment_count})")

                        result['temp_ranges'] = (min_temp, max_temp)
                        result['fan_speeds'] = fan_speeds
                    break

            # 更新状态
            self.last_cpu_temp = max_temp_value
            self.last_check_time = time.time()

            # 性能统计
            check_duration = time.time() - check_start_time
            self.logger.info(f"  性能: 检测耗时 {check_duration:.2f}秒{interval_str}")

            # 运行时统计（仅循环模式）
            if self.start_time is not None:
                uptime = time.time() - self.start_time
                uptime_hours = uptime / 3600
                if uptime_hours >= 1:
                    self.logger.info(f"  统计: 运行时长 {uptime_hours:.1f}小时 | 调整次数 {self.adjustment_count}")

        else:
            self.logger.error(f"服务器 {self.ip}: 没有 CPU 温度数据可用！")

        return result

    def process_server_loop(self):
        """
        循环模式：持续监测 CPU 温度并相应调整风扇转速。
        """
        # 设置 IPMI 为手动模式
        self.set_ipmi_manual_mode()

        # 初始化统计信息
        self.start_time = time.time()
        self.logger.info(f"服务器 {self.ip}: 循环控制模式已启动，检测间隔 {self.interval} 秒")

        prev_temp_ranges = None
        prev_fan_speeds = None

        while True:
            result = self.adjust_fans_once(prev_temp_ranges, prev_fan_speeds)
            prev_temp_ranges = result['temp_ranges']
            prev_fan_speeds = result['fan_speeds']

            time.sleep(self.interval)

    def process_server_once(self):
        """
        单次执行模式：执行一次温度检测和风扇调整后退出。
        适合被外部调度工具（cron、systemd timer 等）调用。
        """
        # 设置 IPMI 为手动模式
        self.set_ipmi_manual_mode()

        # 执行一次调整
        self.adjust_fans_once()
