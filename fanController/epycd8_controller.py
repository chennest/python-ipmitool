"""ASRock Rack EPYCD8（永擎）风扇控制器。

**与 Dell 系列的区别全在「写」这一侧** —— 读取（温度、转速）由基类统一
从 Prometheus 取，与本类无关：

=================  ========================================  ==============================
机型               设置风扇                                   恢复自动
=================  ========================================  ==============================
Dell 730           ``raw 0x30 0x30 0x02 0x{idx} 0x{pct}``     ``raw 0x30 0x30 0x01 0x01``
                   逐个风扇单独设，且**必须先切手动模式**
EPYCD8             ``raw 0x3a 0x01 b1..b8``                   ``raw 0x3a 0x01 0x00 × 8``
                   **一次全量写 8 字节**，无独立手动模式命令
=================  ========================================  ==============================

**EPYCD8 的坑**（2026-09-17 / 09-28 实测）：

1. **必须写满 8 个字节。** 少写一个字节 BMC 不报错、返回码仍为 0，但转速
   纹丝不动。实测曾误给 7 字节，一度误判「该风扇位不可控」。
2. **``0x00`` 就是「交回 BMC 自动」** —— 所以本机型不需要单独的 auto 命令，
   退出时把 8 字节全填 0x00 即可，比 Dell 省事。
3. **手动值不持久化**，BMC 重启或整机断电后失效，自动回到 BMC 自动策略。
4. CPU 温度达到临界阈值时，BMC 会**强行覆盖**手动值（热保护，不可对抗，
   也不应尝试对抗）。

8 字节位映射（b2 为保留位，恒 ``0x00``）::

    b1  CPU1_FAN1
    b2  --（保留）
    b3  REAR_FAN1
    b4  REAR_FAN2   ← 宿主机用于 Tesla T10 散热
    b5  FRNT_FAN1   ← 宿主机用于 Tesla T10 散热
    b6  FRNT_FAN2   （未接风扇）
    b7  FRNT_FAN3   （未接风扇）
    b8  FRNT_FAN4   （未接风扇）
"""

from .base_controller import IPMIFanController


class Epycd8FanController(IPMIFanController):
    """ASRock Rack EPYCD8 风扇控制器。

    只负责 EPYCD8 特有的「写」；「读」由基类提供（风扇转速走 Prometheus，
    温度在本类重写为取 GPU 温度，原因见 :meth:`get_cpu_temperature`）。
    """

    #: 8 字节 payload 长度 —— 硬性要求，少一个字节整条命令会被静默忽略
    PAYLOAD_LEN = 8

    #: 风扇位名称 → payload 下标（0-based）。基类的 ``enumerate(fan_speeds)``
    #: 传进来的 ``fan_index`` 就是这个下标。
    FAN_SLOT_INDEX = {
        'CPU1_FAN1': 0,
        'REAR_FAN1': 2,
        'REAR_FAN2': 3,
        'FRNT_FAN1': 4,
        'FRNT_FAN2': 5,
        'FRNT_FAN3': 6,
        'FRNT_FAN4': 7,
    }

    #: 下标 → 风扇位名（构造一次，省得每次反查）
    _INDEX_TO_SLOT = {v: k for k, v in FAN_SLOT_INDEX.items()}

    #: payload 中保留位的下标
    RESERVED_INDEX = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 维护 8 字节目标状态。BMC 要求全量写，所以必须记住其它位设过什么，
        # 否则「只想改一个位」会把别的位一起踩成 0x00（= 全部交回自动）。
        # 初值全 0x00 = 全部交回 BMC 自动，是安全的起点。
        self._fan_duties = {slot: 0x00 for slot in self.FAN_SLOT_INDEX}

    # ------------------------------------------------------------ 基础命令

    def _get_base_command(self):
        """根据 IP 配置生成基础 IPMI 命令。

        ``ip: "local"`` 时走本地 in-band（``/dev/ipmi0``），不带 lanplus 参数。
        """
        if self.ip == 'local':
            return ""
        return f"-I lanplus -H {self.ip} -U {self.user} -P {self.password}"

    # ------------------------------------------------------------ 读取（重写）

    def _build_temperature_query(self):
        """覆盖基类：EPYCD8 要的是 **GPU 温度**，不是 CPU 温度。

        原因：BMC 里没有任何 GPU 温度传感器（实测温度项只有 MB / Card Side /
        CPU / TR1 / DDR4_A~H），而机箱风扇 FRNT_FAN1 / REAR_FAN2 是两块
        Tesla T10（原厂被动散热）的唯一散热手段。所以基类里「CPU 温度」这个
        字段名，在本机型上承载的其实是 GPU 温度。

        数据源同样是 Prometheus —— DCGM exporter 已接入
        （job 名自定义，如 ``dcgm-exporter``），指标 ``DCGM_FI_DEV_GPU_TEMP``。

        Returns:
            str | None: PromQL；未配置 ``gpu_instance`` 时返回 ``None``。
        """
        instance = self.prometheus_config.get('gpu_instance')
        if not instance:
            self.logger.error(
                f"服务器 {self.ip}: 未配置 prometheus.gpu_instance，无法定位 "
                f"DCGM 数据（例: 192.0.2.10:9400）"
            )
            return None
        return f'DCGM_FI_DEV_GPU_TEMP{{instance="{instance}"}}'

    # ------------------------------------------------------------ 写入

    def set_fan_speed(self, fan_index, percentage):
        """设置风扇转速。

        Args:
            fan_index (int): **payload 下标**（0-based），与配置里 ``fan_speeds``
                列表的位置一一对应。
            percentage (int): 转速百分比。``1~100`` = 手动占空比；
                **``0`` = 把该位交回 BMC 自动控制**（这和 Dell 不同 ——
                Dell 那边 0 无意义，EPYCD8 这边 0 是个有语义的值）。

        说明：单次调用**不会**立即下发，而是更新目标状态后整包写出。
        因为 BMC 要求 8 字节全量写，逐个位调用来回写 8 次既慢又容易互相踩。
        基类的 ``adjust_fans_once`` 会 ``enumerate`` 遍历所有位，所以
        一整套调完正好写一整包。
        """
        slot = self._INDEX_TO_SLOT.get(fan_index)
        if slot is None:
            self.logger.warning(
                f"服务器 {self.ip}: 未知风扇下标 {fan_index}（合法范围 "
                f"0~{self.PAYLOAD_LEN - 1}），跳过"
            )
            return

        if percentage == 0:
            duty = 0x00
        elif 1 <= percentage <= 100:
            duty = percentage
        else:
            self.logger.warning(
                f"服务器 {self.ip}: 非法占空比 {percentage}%（应为 0 或 1~100），跳过"
            )
            return

        self._fan_duties[slot] = duty
        self._write_payload()

    def _write_payload(self):
        """把当前目标状态按 8 字节全量写下去。

        **永远写满 8 字节** —— 这是这个机型最容易踩的坑，见模块头部说明。
        """
        payload = [0x00] * self.PAYLOAD_LEN
        for slot, duty in self._fan_duties.items():
            payload[self.FAN_SLOT_INDEX[slot]] = duty
        payload[self.RESERVED_INDEX] = 0x00  # 保留位恒 0

        bytes_str = " ".join(f"0x{b:02x}" for b in payload)
        base_cmd = self._get_base_command()
        command = f"{base_cmd} raw 0x3a 0x01 {bytes_str}"
        self.ipmi_command(command.strip())

    # ------------------------------------------------------------ 模式切换

    def set_ipmi_manual_mode(self):
        """EPYCD8 没有独立的「切手动」命令 —— 写入非零占空比本身就是手动模式。

        所以这里**什么都不做**，只记一条日志。刻意不去刷一遍 payload：
        程序刚启动、目标值还没算出来的时候刷一遍，会把当前状态清成全自动，
        造成一次没必要的转速波动。
        """
        self.logger.info(
            f"服务器 {self.ip}: EPYCD8 无需单独切换手动模式（写占空比即进入手动）"
        )

    def set_ipmi_auto_mode(self):
        """把全部风扇位交回 BMC 自动控制（8 字节全填 0x00）。

        这是本机型的**安全回退动作**，也是它比 Dell 省事的地方 ——
        不需要额外的 ``raw 0x30 0x30 0x01 0x01``。
        """
        self._fan_duties = {slot: 0x00 for slot in self.FAN_SLOT_INDEX}
        self._write_payload()
        self.logger.info(f"服务器 {self.ip}: 已把全部风扇位交回 BMC 自动控制")

    # ------------------------------------------------------------ 入口

    def start_fan_control(self):
        """启动循环控制（供 ``fancontroller.py`` 调用）。"""
        self.process_server_loop()

    def run_once(self):
        """执行一次控制（供 ``fancontroller_once.py`` 调用）。"""
        self.process_server_once()
