"""传感器读取 —— GPU 指标、风扇转速、板载温度。

设计取舍：

- **GPU 温度优先走本地 DCGM 端点**（``127.0.0.1:9400``），而不是解析
  ``nvidia-smi`` 的文本输出。理由：口径与观测侧（Prometheus）完全一致，
  不会出现「控制器用一个值、看板上是另一个值」这种对不上的情况；
  且 Prometheus 文本格式比 nvidia-smi 的人类可读输出稳定得多。
  ``nvidia-smi`` 保留为兜底，DCGM 挂了也不至于瞎眼。
- **风扇转速优先走本地 ipmi_exporter**（``127.0.0.1:9290``），兜底 ``ipmitool``。
- **只用标准库**（``urllib``），不引入 ``requests``/``prometheus_client`` ——
  这台机器上少一个依赖就少一个将来炸的点。
- 所有网络/子进程调用**带超时**。上游项目的教训：一个不带超时的阻塞读
  能把整个控制线程静默挂死。

关于 pve02 这块板子（ASRock Rack EPYCD8，BMC 固件 2.20）的实测要点：

- ``ipmitool sdr type fan`` 输出**五列**：``名称 | 传感器ID | 状态 | 阈值 | 读数``。
  读数在**最后一列**。最初按「第二列是读数」写会把传感器 ID ``62h`` 当成 RPM。
- 风扇传感器有 **14 个位**，其中 10 个是 ``FRNT_FAN2_2`` 这类未接位，全报
  ``No Reading``。它们不参与控制，解析时直接跳过。
- **BMC 里没有任何 GPU 温度传感器**（实测只有 MB / Card Side / CPU / TR1 /
  DDR4_A~H）。这不是漏配，是硬件层面就没接 —— 所以 GPU 联动只能走 DCGM。
"""

from __future__ import annotations

import logging
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 常量

#: ipmi_exporter 的传感器状态码语义（风扇与温度共用同一套编码）
SENSOR_STATE_LABELS: dict[int, str] = {
    0: "nominal",
    1: "warning",
    2: "critical",
}

# ------------------------------------------------------------------ 解析工具

_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?"
    r"[ \t]+(?P<value>[^\s]+)"
)
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
_RPM_RE = re.compile(r"(\d+)\s*RPM")
_TEMP_RE = re.compile(r"(-?\d+)\s*degrees\s*C", re.IGNORECASE)


@dataclass(frozen=True)
class Sample:
    """一条 Prometheus 样本。"""

    name: str
    labels: dict[str, str]
    value: float


def parse_prometheus(text: str) -> list[Sample]:
    """解析 Prometheus 文本格式。忽略注释与无法解析的行。"""
    samples: list[Sample] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        if not match:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue  # NaN / +Inf 之类，本项目用不上
        labels = dict(_LABEL_RE.findall(match.group("labels") or ""))
        samples.append(Sample(match.group("name"), labels, value))
    return samples


def _fetch_text(url: str, timeout: float) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": "gpu-fan-console/0.1"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_sdr_line(line: str) -> tuple[str, str, str] | None:
    """解析一行 ``ipmitool sdr`` 输出，返回 ``(名称, 读数, 状态)``。

    EPYCD8 上的实际格式是**五列**：``名称 | 传感器ID | 状态 | 阈值 | 读数``。
    注意**读数在最后一列**、不是第二列（第二列是传感器 ID）::

        FRNT_FAN1        | 62h | ok  |  7.0 | 3000 RPM
        MB Temp          | 31h | ok  |  3.0 | 34 degrees C
        FRNT_FAN2        | 63h | ns  |  7.0 | No Reading

    2026-09-28 实测踩坑：最初按「第二列是读数」写正则，结果把 ``62h``
    当成了 RPM，静默解析出 None。不同 ipmitool 版本与传感器类型的列数
    并不一致，所以按管道分割后取「首列=名称、第三列=状态、末列=读数」，
    比写死正则稳。

    Returns:
        ``(name, reading, state)``；无法解析时返回 ``None``。
    """
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 4 or not parts[0]:
        return None
    state = parts[2] if len(parts) > 2 else ""
    return parts[0], parts[-1], state


# ------------------------------------------------------------------ 数据模型


@dataclass
class GPUMetric:
    """单张 GPU 的一次快照。"""

    uuid: str
    index: int
    pci_bus_id: str = ""
    model_name: str = ""
    temperature: float | None = None
    power_watts: float | None = None
    utilization: float | None = None
    #: SM/核心频率（MHz）—— 概览卡片展示用的「GPU 频率」
    clock_mhz: float | None = None
    memory_used_mib: float | None = None
    memory_total_mib: float | None = None
    source: str = ""

    @property
    def short_uuid(self) -> str:
        """``GPU-e49ed30f-...`` → ``e49ed30f``，用于日志和前端展示。"""
        return self.uuid.removeprefix("GPU-")[:8]

    @property
    def memory_percent(self) -> float | None:
        if not self.memory_total_mib:
            return None
        used = self.memory_used_mib or 0.0
        return round(used / self.memory_total_mib * 100, 1)


@dataclass
class FanReading:
    """单个风扇位的一次读数。"""

    slot: str
    rpm: float | None = None
    state: str = ""
    source: str = ""


@dataclass
class BoardTemperature:
    """BMC 板载温度读数。"""

    name: str
    celsius: float | None = None
    state: str = ""


# ------------------------------------------------------------------ GPU 读取


class GPUMetricsReader:
    """GPU 指标读取器：DCGM 端点优先，nvidia-smi 兜底。"""

    def __init__(
        self,
        dcgm_endpoint: str | None = "http://127.0.0.1:9400/metrics",
        nvidia_smi: str = "nvidia-smi",
        timeout: float = 5.0,
    ) -> None:
        self.dcgm_endpoint = dcgm_endpoint
        self.nvidia_smi = nvidia_smi
        self.timeout = timeout
        #: 上一次成功读取用的数据源，便于排障
        self.last_source: str = ""

    # ---------------------------------------------------------- 入口

    def read(self) -> list[GPUMetric]:
        """读取全部 GPU 快照。两个数据源都失败时返回空列表（不抛异常）。"""
        if self.dcgm_endpoint:
            try:
                metrics = self._read_dcgm()
                if metrics:
                    self.last_source = "dcgm"
                    return metrics
                logger.warning("DCGM 端点返回空指标，回退 nvidia-smi")
            except (urllib.error.URLError, OSError, ValueError) as exc:
                logger.warning("DCGM 端点读取失败，回退 nvidia-smi: %s", exc)

        try:
            metrics = self._read_nvidia_smi()
            self.last_source = "nvidia-smi"
            return metrics
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            logger.error("nvidia-smi 也读不到: %s", exc)
            return []

    # ---------------------------------------------------------- DCGM

    #: DCGM 字段名 → :class:`GPUMetric` 属性名
    _DCGM_FIELDS: dict[str, str] = {
        "DCGM_FI_DEV_GPU_TEMP": "temperature",
        "DCGM_FI_DEV_POWER_USAGE": "power_watts",
        "DCGM_FI_DEV_GPU_UTIL": "utilization",
        # SM/核心频率（MHz）。dcgm-exporter 默认指标集里就有，无需加 --metrics
        "DCGM_FI_DEV_SM_CLOCK": "clock_mhz",
        "DCGM_FI_DEV_FB_USED": "memory_used_mib",
        # FB_TOTAL 在部分版本里不直接提供，用 USED + FREE 兜底
        "DCGM_FI_DEV_FB_FREE": "_memory_free_mib",
    }

    def _read_dcgm(self) -> list[GPUMetric]:
        text = _fetch_text(self.dcgm_endpoint or "", self.timeout)
        samples = parse_prometheus(text)

        buckets: dict[str, dict[str, Any]] = {}
        for sample in samples:
            attr = self._DCGM_FIELDS.get(sample.name)
            if attr is None or sample.name.startswith("DCGM_FI_PROF_"):
                continue
            uuid = sample.labels.get("UUID")
            if not uuid:
                continue
            bucket = buckets.setdefault(
                uuid,
                {
                    "uuid": uuid,
                    "index": _safe_int(sample.labels.get("gpu"), -1),
                    "pci_bus_id": sample.labels.get("pci_bus_id", ""),
                    "model_name": sample.labels.get("modelName", ""),
                },
            )
            bucket[attr] = sample.value

        metrics: list[GPUMetric] = []
        for bucket in buckets.values():
            free = bucket.pop("_memory_free_mib", None)
            used = bucket.get("memory_used_mib")
            if used is not None and free is not None:
                bucket["memory_total_mib"] = used + free
            metrics.append(GPUMetric(source="dcgm", **bucket))

        metrics.sort(key=lambda m: m.index)
        return metrics

    # ---------------------------------------------------------- nvidia-smi

    _SMI_FIELDS = (
        "index,uuid,pci.bus_id,name,"
        "temperature.gpu,power.draw,utilization.gpu,memory.used,memory.total,"
        "clocks.sm"
    )

    def _read_nvidia_smi(self) -> list[GPUMetric]:
        cmd = [
            self.nvidia_smi,
            f"--query-gpu={self._SMI_FIELDS}",
            "--format=csv,noheader,nounits",
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self.timeout, check=False
        )
        if proc.returncode != 0:
            raise ValueError(f"nvidia-smi 返回 {proc.returncode}: {proc.stderr.strip()}")

        metrics: list[GPUMetric] = []
        for line in proc.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 9:
                continue
            metrics.append(
                GPUMetric(
                    uuid=parts[1],
                    index=_safe_int(parts[0], -1),
                    pci_bus_id=parts[2],
                    model_name=parts[3],
                    temperature=_safe_float(parts[4]),
                    power_watts=_safe_float(parts[5]),
                    utilization=_safe_float(parts[6]),
                    # clocks.sm 是后加的字段 —— 老驱动列数不足时容忍缺失
                    clock_mhz=_safe_float(parts[9]) if len(parts) > 9 else None,
                    memory_used_mib=_safe_float(parts[7]),
                    memory_total_mib=_safe_float(parts[8]),
                    source="nvidia-smi",
                )
            )
        return metrics


# ------------------------------------------------------------------ 风扇读取


class FanMetricsReader:
    """风扇转速读取器：ipmi_exporter 优先，ipmitool 兜底。"""

    def __init__(
        self,
        exporter_endpoint: str | None = "http://127.0.0.1:9290/metrics",
        ipmi_binary: str = "ipmitool",
        timeout: float = 5.0,
    ) -> None:
        self.exporter_endpoint = exporter_endpoint
        self.ipmi_binary = ipmi_binary
        self.timeout = timeout
        self.last_source: str = ""

    def read(self) -> dict[str, FanReading]:
        """读取全部风扇位转速，返回 ``{风扇位名: FanReading}``。"""
        if self.exporter_endpoint:
            try:
                readings = self._read_exporter()
                if readings:
                    self.last_source = "ipmi_exporter"
                    return readings
            except (urllib.error.URLError, OSError, ValueError) as exc:
                logger.warning("ipmi_exporter 读取失败，回退 ipmitool: %s", exc)

        try:
            readings = self._read_ipmitool()
            self.last_source = "ipmitool"
            return readings
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            logger.error("ipmitool 也读不到风扇转速: %s", exc)
            return {}

    def _read_exporter(self) -> dict[str, FanReading]:
        text = _fetch_text(self.exporter_endpoint or "", self.timeout)
        rpms: dict[str, float] = {}
        states: dict[str, int] = {}

        for sample in parse_prometheus(text):
            name = sample.labels.get("name")
            if not name:
                continue
            if sample.name == "ipmi_fan_speed_rpm":
                rpms[name] = sample.value
            elif sample.name == "ipmi_fan_speed_state":
                states[name] = int(sample.value)

        readings: dict[str, FanReading] = {}
        for name, rpm in rpms.items():
            code = states.get(name, 0)
            readings[name] = FanReading(
                slot=name,
                rpm=rpm,
                state=SENSOR_STATE_LABELS.get(code, f"code={code}"),
                source="ipmi_exporter",
            )
        return readings

    def _read_ipmitool(self) -> dict[str, FanReading]:
        proc = subprocess.run(
            [self.ipmi_binary, "sdr", "type", "fan"],
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise ValueError(f"ipmitool 返回 {proc.returncode}")

        readings: dict[str, FanReading] = {}
        for line in proc.stdout.splitlines():
            parsed = parse_sdr_line(line.strip())
            if parsed is None:
                continue
            slot, reading, state = parsed
            rpm_match = _RPM_RE.search(reading)
            if rpm_match is None:
                # EPYCD8 会报出一堆**未接**的传感器位（``FRNT_FAN2_2`` 之类，
                # 实测共 14 个位、其中 10 个是 No Reading）。它们不参与控制，
                # 跳过 —— 也让两条数据源的行为保持一致（ipmi_exporter 只暴露
                # 有读数的 4 个位）。
                continue
            readings[slot] = FanReading(
                slot=slot,
                rpm=float(rpm_match.group(1)),
                state=state,
                source="ipmitool",
            )
        return readings


# ------------------------------------------------------------------ 板载温度


class BoardTemperatureReader:
    """BMC 板载温度读取（MB / CPU / Card Side / DDR4_*）。

    ⚠️ **这些全都不是 GPU 温度。** EPYCD8 的 BMC 里没有任何 GPU 温度传感器 ——
    这不是漏配，是硬件层面就没接。所以 GPU 联动必须走 DCGM，BMC 那条
    11 级自动温度-占空比曲线对 GPU 完全无效。

    这里读板载温度纯粹是给界面多一个参照，尤其 ``Card Side Temp``
    （相对最能反映机箱内扩展卡区域的热环境）。
    """

    def __init__(
        self,
        exporter_endpoint: str | None = "http://127.0.0.1:9290/metrics",
        ipmi_binary: str = "ipmitool",
        timeout: float = 5.0,
    ) -> None:
        self.exporter_endpoint = exporter_endpoint
        self.ipmi_binary = ipmi_binary
        self.timeout = timeout
        self.last_source: str = ""

    def read(self) -> dict[str, BoardTemperature]:
        if self.exporter_endpoint:
            try:
                readings = self._read_exporter()
                if readings:
                    self.last_source = "ipmi_exporter"
                    return readings
            except (urllib.error.URLError, OSError, ValueError) as exc:
                logger.warning("板载温度读取失败（exporter），回退 ipmitool: %s", exc)

        try:
            readings = self._read_ipmitool()
            self.last_source = "ipmitool"
            return readings
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            logger.error("板载温度读取失败（ipmitool）: %s", exc)
            return {}

    def _read_exporter(self) -> dict[str, BoardTemperature]:
        text = _fetch_text(self.exporter_endpoint or "", self.timeout)
        values: dict[str, float] = {}
        states: dict[str, int] = {}

        for sample in parse_prometheus(text):
            name = sample.labels.get("name")
            if not name:
                continue
            if sample.name == "ipmi_temperature_celsius":
                values[name] = sample.value
            elif sample.name == "ipmi_temperature_state":
                states[name] = int(sample.value)

        return {
            name: BoardTemperature(
                name=name,
                celsius=celsius,
                state=SENSOR_STATE_LABELS.get(states.get(name, 0), "unknown"),
            )
            for name, celsius in values.items()
        }

    def _read_ipmitool(self) -> dict[str, BoardTemperature]:
        proc = subprocess.run(
            [self.ipmi_binary, "sdr", "type", "Temperature"],
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise ValueError(f"ipmitool 返回 {proc.returncode}")

        readings: dict[str, BoardTemperature] = {}
        for line in proc.stdout.splitlines():
            parsed = parse_sdr_line(line.strip())
            if parsed is None:
                continue
            name, reading, state = parsed
            temp_match = _TEMP_RE.search(reading)
            if temp_match is None:
                continue  # "No Reading"（未接的 DDR4 槽位等）
            readings[name] = BoardTemperature(
                name=name, celsius=float(temp_match.group(1)), state=state
            )
        return readings


# ------------------------------------------------------------------ CPU 核温度


@dataclass
class CPUCoreTemperature:
    """CPU 核心温度（node_exporter 的 hwmon collector）。"""

    label: str
    celsius: float
    chip: str = ""


class CPUCoreTemperatureReader:
    """CPU 核温度读取（node_exporter 的 hwmon collector）。

    ⚠️ **k10temp 在 Prometheus/hwmon 里的 chip 名是 PCI 路径形式**
    （``pci0000:00_0000:00:18_3``），不是可读的 ``k10temp`` —— 因为 AMD 的
    k10temp 挂在 PCI 设备 ``00:18.3`` 下。硬编码 chip 名换台机器就废了，
    所以这里靠 ``node_hwmon_sensor_label`` 做**语义关联**：

    - ``node_hwmon_temp_celsius{chip, sensor}`` → 数值
    - ``node_hwmon_sensor_label{chip, sensor, label}`` → 可读名（Tctl / Tccd1…）

    两个指标都在 node_exporter 的 ``/metrics`` 里，直接从本机端点解析，
    不绕 Prometheus（那边是 30s 快照）。
    """

    def __init__(
        self,
        exporter_endpoint: str | None = "http://127.0.0.1:9100/metrics",
        ipmi_binary: str = "ipmitool",
        timeout: float = 5.0,
    ) -> None:
        self.exporter_endpoint = exporter_endpoint
        self.ipmi_binary = ipmi_binary
        self.timeout = timeout
        self.last_source: str = ""

    def read(self) -> list[CPUCoreTemperature]:
        if self.exporter_endpoint:
            try:
                readings = self._read_exporter()
                if readings:
                    self.last_source = "node_exporter"
                    return readings
            except (urllib.error.URLError, OSError, ValueError) as exc:
                logger.warning("CPU 核温度读取失败（node_exporter）: %s", exc)

        try:
            readings = self._read_sensors_command()
            self.last_source = "sensors"
            return readings
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("CPU 核温度读取失败（sensors）: %s", exc)
            return []

    def _read_exporter(self) -> list[CPUCoreTemperature]:
        text = _fetch_text(self.exporter_endpoint or "", self.timeout)
        samples = parse_prometheus(text)

        values: dict[tuple[str, str], float] = {}
        labels: dict[tuple[str, str], str] = {}

        for sample in samples:
            chip = sample.labels.get("chip", "")
            sensor = sample.labels.get("sensor", "")
            if not chip or not sensor:
                continue
            if sample.name.startswith("node_hwmon_temp_celsius"):
                values[(chip, sensor)] = sample.value
            elif sample.name.startswith("node_hwmon_sensor_label"):
                labels[(chip, sensor)] = sample.labels.get("label", "")

        readings: list[CPUCoreTemperature] = []
        for (chip, sensor), celsius in values.items():
            label = labels.get((chip, sensor), "")
            # 只保留 CPU 核心相关的（Tctl / Tccd*），别把 NVMe、网卡、
            # 主板 SuperIO 的温度也混进来 —— 那些由板载温度那一路负责
            if not (label.startswith("Tctl") or label.startswith("Tccd")):
                continue
            readings.append(
                CPUCoreTemperature(label=label, celsius=celsius, chip=chip)
            )

        # Tctl 排最前（它才是控速真正关心的），其余按名称排
        readings.sort(key=lambda r: (r.label != "Tctl", r.label))
        return readings

    def _read_sensors_command(self) -> list[CPUCoreTemperature]:
        """兜底：解析 ``sensors`` 命令输出。

        ⚠️ 只当 node_exporter 不可用时用 —— ``sensors`` 的输出是给人看的
        排版，还混着大量无效项（未接的传感器脚会报 ``ALARM``），不适合
        程序解析。这里只挑 k10temp 那一段的 Tctl/Tccd。
        """
        proc = subprocess.run(
            ["sensors"],
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise ValueError(f"sensors 返回 {proc.returncode}")

        readings: list[CPUCoreTemperature] = []
        in_k10temp = False
        for line in proc.stdout.splitlines():
            if line.startswith("k10temp-"):
                in_k10temp = True
                continue
            if not in_k10temp:
                continue
            if line and not line.startswith((" ", "\t")):
                break  # 离开 k10temp 段落
            stripped = line.strip()
            if not stripped:
                continue
            head, _, rest = stripped.partition(":")
            label = head.strip()
            if not (label.startswith("Tctl") or label.startswith("Tccd")):
                continue
            match = re.search(r"([+-]?\d+(?:\.\d+)?)", rest)
            if match:
                readings.append(
                    CPUCoreTemperature(label=label, celsius=float(match.group(1)))
                )

        readings.sort(key=lambda r: (r.label != "Tctl", r.label))
        return readings


# ------------------------------------------------------------------ 小工具


def _safe_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text or text.lower() in {"n/a", "na", "[n/a]", "nan", "not supported"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _safe_int(value: str | None, default: int = 0) -> int:
    parsed = _safe_float(value)
    return default if parsed is None else int(parsed)
