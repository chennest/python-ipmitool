"""应用配置模型与加载。

配置用 YAML（``app/config.yaml``），模型用 pydantic 校验 —— 配置写错时
启动即报错，而不是跑起来才发现某个字段是字符串的 ``"50"``。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml

from .runtime import DEFAULT_CONFIG_PATH, DEFAULT_HEARTBEAT_PATH
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


class ServerConfig(BaseModel):
    """HTTP 服务配置。"""

    host: str = "0.0.0.0"
    port: int = Field(default=8765, ge=1, le=65535)


class ControlConfig(BaseModel):
    """控制回路配置。"""

    enabled: bool = True
    mode: Literal["auto", "manual"] = "auto"
    #: 控制周期（秒）
    interval: float = Field(default=15.0, gt=0)
    #: 占空比变化小于此值时不重复下发，省得天天刷 BMC
    min_write_delta: int = Field(default=3, ge=0, le=100)
    #: 连续读取失败多少次后判定为「失明」
    max_consecutive_failures: int = Field(default=3, ge=1)


class SourcesConfig(BaseModel):
    """数据源配置。"""

    # --- 实时读数：直连本机 exporter 的 /metrics（最低延迟）---
    dcgm_endpoint: str | None = "http://127.0.0.1:9400/metrics"
    ipmi_exporter_endpoint: str | None = "http://127.0.0.1:9290/metrics"
    #: node_exporter（需开 --collector.hwmon）—— 取 CPU 核心温度 Tctl/Tccd
    node_exporter_endpoint: str | None = "http://127.0.0.1:9100/metrics"
    ipmitool_binary: str = "ipmitool"
    ipmi_timeout: float = Field(default=10.0, gt=0)
    http_timeout: float = Field(default=5.0, gt=0)
    #: 远程 BMC（``lanplus``）。留空则走本地 in-band（推荐）
    ipmi_remote: dict[str, str] | None = None

    # --- 历史趋势（可选）：只给 /api/history 用 ---
    # 实时读数**不走**这里，因为 Prometheus 是 30s 快照（见 CLAUDE.md）。
    # 但历史曲线恰恰是 Prometheus 的主场，所以单独配一套。
    prometheus_url: str | None = None
    #: GPU 温度在 Prometheus 里的 instance 标签，如 ``192.168.6.7:9400``
    prometheus_gpu_instance: str | None = None
    #: CPU 温度（node_exporter 的 hwmon / k10temp）的 instance 标签，如 ``192.168.6.7:9100``
    prometheus_node_instance: str | None = None
    #: 风扇转速的 instance 标签，如 ``192.168.6.7:9290``
    prometheus_fan_instance: str | None = None


class CurveConfig(BaseModel):
    """温度-占空比曲线配置。"""

    points: list[dict[str, float]] = Field(
        default_factory=lambda: [
            {"temp": 45, "duty": 40},
            {"temp": 55, "duty": 50},
            {"temp": 65, "duty": 65},
            {"temp": 75, "duty": 80},
            {"temp": 85, "duty": 100},
        ]
    )
    #: 滞回带（°C）。降温方向必须跌出这个带宽才降档。
    hysteresis: float = Field(default=3.0, ge=0)
    #: 占空比下限。BMC 本身可能拒绝低于约 20% 的值，这里再兜一层。
    min_duty: int = Field(default=30, ge=1, le=100)
    max_duty: int = Field(default=100, ge=1, le=100)


class FanBinding(BaseModel):
    """风扇位声明（**可选**）。

    ⚠️ 这里**不包含分配关系**（哪张 GPU 用哪些风扇），也不需要把机器上的
    风扇位全列出来 —— ``fan_slots`` 是**探测出来的**（配置声明的位 ∪
    ipmi_exporter 实测到有读数的位），用户在界面上从探测清单里勾选即可。

    配置里写位只有两个用途：① 提前占位（风扇还没转起来/没读数时也想让它
    出现在清单里）② 限制定制范围。**留空（默认）= 完全交给自动探测**。

    为什么不把分配写进配置文件：那会造成**两个数据源**。用户在界面上改的
    存进了数据库，回头改 config.yaml 却不生效（被数据库覆盖），非常容易把人
    绕晕。单一来源，少一类 bug。（2026-09-28 超哥两次纠正过这个点。）
    """

    slot: str
    #: 该风扇位的占空比上限（覆盖曲线的 max_duty）；不填则用曲线自己的
    max_duty: int | None = Field(default=None, ge=1, le=100)


class SafetyConfig(BaseModel):
    """安全护栏配置。"""

    #: 心跳文件路径。独立看门狗据此判断主进程是否还活着。
    #: 默认值随运行形态变化（源码/Linux 服务 = /run/...；冻结 exe = exe 旁），见 runtime.py
    heartbeat_path: str = Field(default_factory=lambda: DEFAULT_HEARTBEAT_PATH)
    #: 退出回退动作的超时（给得宽裕一点，回退是性命攸关的事）
    restore_timeout: float = Field(default=15.0, gt=0)
    #: 超过此温度立即拉满，不再等曲线
    emergency_temp: float = 85.0
    #: 温度回落到此值以下才解除紧急状态（同样需要滞回，否则会在临界点反复横跳）
    emergency_resume_temp: float = 75.0


class AppConfig(BaseModel):
    """应用总配置。"""

    server: ServerConfig = Field(default_factory=ServerConfig)
    control: ControlConfig = Field(default_factory=ControlConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    curve: CurveConfig = Field(default_factory=CurveConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    fans: list[FanBinding] = Field(default_factory=list)

    @field_validator("fans")
    @classmethod
    def _check_fan_slots(cls, value: list[FanBinding]) -> list[FanBinding]:
        from .ipmi import FAN_SLOT_INDEX

        seen: set[str] = set()
        for binding in value:
            if binding.slot not in FAN_SLOT_INDEX:
                raise ValueError(
                    f"未知风扇位 {binding.slot!r}，合法取值: {sorted(FAN_SLOT_INDEX)}"
                )
            if binding.slot in seen:
                raise ValueError(f"风扇位 {binding.slot} 重复配置")
            seen.add(binding.slot)
        return value


def load_config(path: str | Path | None = None) -> AppConfig:
    """从 YAML 加载配置。文件不存在时用默认值并给出提示。"""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH

    if not config_path.exists():
        logger.warning("配置文件 %s 不存在，使用内置默认值", config_path)
        return AppConfig()

    raw: Any = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config = AppConfig.model_validate(raw)
    logger.info("已加载配置: %s", config_path)
    return config
