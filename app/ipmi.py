"""IPMI 风扇控制底层封装。

目标平台：ASRock Rack EPYCD8（BMC 固件 2.20），命令族 ``raw 0x3a 0x01``。

⚠️ 三条硬约束 —— 动这个文件之前请先读完：

1. **必须一次写满 8 个字节。**
   少写字节时 BMC 不报错、返回码仍为 0，但目标风扇转速纹丝不动。
   （2026-09-17 实测：误给 7 字节，一度误判「该风扇位不可控」，补齐后立即生效。）

2. **单字节取值语义**
   - ``0x00`` = 交回 BMC 自动控制（硬件 Smart Fan 温度-占空比表生效）
   - ``0x01``~``0x64`` = 手动占空比百分比。字节的**十进制值**即百分比：
     ``0x14``=20%、``0x32``=50%、``0x64``=100%
   - BMC 可能拒绝低于约 20% 的占空比

3. **手动值不持久化**
   BMC 重启或整机断电后自动回到 BMC 自动策略；CPU 温度达到临界阈值时
   BMC 会强行覆盖手动值。这是热保护，**不可对抗，也不应尝试对抗**。

8 字节位映射（b2 为保留位，恒 ``0x00``）::

    b1  CPU1_FAN1
    b2  --（保留）
    b3  REAR_FAN1
    b4  REAR_FAN2   ← 宿主机用于 Tesla T10 散热
    b5  FRNT_FAN1   ← 宿主机用于 Tesla T10 散热
    b6  FRNT_FAN2   （未接风扇）
    b7  FRNT_FAN3   （未接风扇）
    b8  FRNT_FAN4   （未接风扇）

相对上游 python-ipmitool 的两处关键改进：

- 所有子进程调用**强制带超时**（上游用 ``p.stdout.read()`` 无超时，ipmitool
  一旦卡住会静默挂死整个线程，进程活着但不干活 —— 最难排查的失效形态）
- 在「必须全量写」的前提下，本地维护目标状态，从而能精确控制单个风扇位，
  不会误踩其它位
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Iterable, Mapping

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- 常量

#: 风扇位名称 → 8 字节 payload 中的下标（0-based）
FAN_SLOT_INDEX: dict[str, int] = {
    "CPU1_FAN1": 0,
    "REAR_FAN1": 2,
    "REAR_FAN2": 3,
    "FRNT_FAN1": 4,
    "FRNT_FAN2": 5,
    "FRNT_FAN3": 6,
    "FRNT_FAN4": 7,
}

#: payload 中保留位（b2）的下标
RESERVED_INDEX: int = 1

#: payload 长度。少一个字节 BMC 会静默忽略整条命令，所以这是个硬性校验点。
PAYLOAD_LEN: int = 8

#: 手动占空比取值范围（0 被保留用于表示「自动」）
MIN_DUTY: int = 1
MAX_DUTY: int = 100

#: 语义常量：交回 BMC 自动控制
AUTO: None = None

#: 控制命令的 opcode 前缀
CMD_PREFIX: tuple[str, ...] = ("raw", "0x3a", "0x01")

#: 需要管控的风扇位（供上层循环使用）
GPU_COOLING_SLOTS: tuple[str, ...] = ("FRNT_FAN1", "REAR_FAN2")


# --------------------------------------------------------------------- 异常


class IPMIError(RuntimeError):
    """IPMI 操作失败基类。"""


class IPMITimeoutError(IPMIError):
    """ipmitool 调用超时。"""


class IPMINotFoundError(IPMIError):
    """找不到 ipmitool 可执行文件。"""


# --------------------------------------------------------------------- 结果


@dataclass(frozen=True)
class CmdResult:
    """一次 ipmitool 调用的结果。"""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def summary(self) -> str:
        return (
            f"rc={self.returncode} {self.duration:.2f}s "
            f"stdout={self.stdout.strip()!r} stderr={self.stderr.strip()!r}"
        )


# --------------------------------------------------------------------- 编解码


def _fmt_byte(value: int) -> str:
    """把 0~255 的字节值格式化成 ipmitool 需要的十六进制字面量。"""
    return f"0x{value & 0xFF:02x}"


def encode_duty(duty: int | None) -> int:
    """占空比 → 单字节值。

    ``None`` 表示交回 BMC 自动（编码为 ``0x00``）；``1~100`` 表示手动百分比，
    字节的十进制值就是百分比本身（``50`` → ``0x32``）。
    """
    if duty is None:
        return 0x00
    if isinstance(duty, bool) or not isinstance(duty, int):
        raise TypeError(f"占空比必须是 int 或 None，收到 {type(duty).__name__}")
    if not MIN_DUTY <= duty <= MAX_DUTY:
        raise ValueError(f"占空比需在 {MIN_DUTY}~{MAX_DUTY} 之间，收到 {duty}")
    return duty


# --------------------------------------------------------------------- 客户端


class IPMIClient:
    """ipmitool 封装。

    默认走**本地 in-band**（``/dev/ipmi0``，需要 root），这是推荐用法：
    链路最短、无网络依赖。也支持 ``lanplus`` 远程模式指向 BMC 独立地址
    （``192.0.2.11``），用于宿主系统起不来时的带外兜底。
    """

    def __init__(
        self,
        binary: str = "ipmitool",
        remote: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        dry_run: bool = False,
    ) -> None:
        self.binary = binary
        self.remote = dict(remote) if remote else None
        self.timeout = timeout
        self.dry_run = dry_run
        # 目标状态：风扇位 → 占空比（None = BMC 自动）。
        # BMC 要求 8 字节全量写，所以必须靠这份状态拼出完整 payload，
        # 否则「只想改一个位」就会把其它位一起踩成 0x00。
        self._target: dict[str, int | None] = {slot: AUTO for slot in FAN_SLOT_INDEX}

    # ------------------------------------------------------------ 内部工具

    def _base_args(self) -> list[str]:
        if not self.remote:
            return []
        return [
            "-I",
            "lanplus",
            "-H",
            str(self.remote.get("host", "")),
            "-U",
            str(self.remote.get("user", "admin")),
            "-P",
            str(self.remote.get("password", "")),
        ]

    @staticmethod
    def _redact(cmd: Iterable[str]) -> list[str]:
        """日志脱敏：别把 BMC 密码打进日志文件。"""
        out: list[str] = []
        mask_next = False
        for token in cmd:
            if mask_next:
                out.append("***")
                mask_next = False
                continue
            out.append(token)
            if token == "-P":
                mask_next = True
        return out

    # ------------------------------------------------------------ 执行

    def run(self, args: Iterable[str], timeout: float | None = None) -> CmdResult:
        """执行一次 ipmitool 调用。超时抛 :class:`IPMITimeoutError`。"""
        cmd = [self.binary, *self._base_args(), *args]
        effective_timeout = self.timeout if timeout is None else timeout
        started = time.monotonic()

        logger.debug("执行: %s", " ".join(self._redact(cmd)))

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            logger.error(
                "ipmitool 超时（%.1fs 未返回）: %s",
                effective_timeout,
                " ".join(self._redact(cmd)),
            )
            raise IPMITimeoutError(
                f"ipmitool 调用超过 {effective_timeout}s 未返回"
            ) from exc
        except FileNotFoundError as exc:
            raise IPMINotFoundError(
                f"找不到 ipmitool 可执行文件: {self.binary!r}，请先安装 ipmitool"
            ) from exc

        result = CmdResult(
            args=tuple(cmd),
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration=time.monotonic() - started,
        )
        if not result.ok:
            logger.warning("ipmitool 返回非零: %s", result.summary())
        return result

    # ------------------------------------------------------------ 目标状态

    @property
    def target_state(self) -> dict[str, int | None]:
        """当前目标状态的副本（风扇位 → 占空比 / None）。"""
        return dict(self._target)

    def describe_target(self) -> str:
        parts = [
            f"{slot}={'auto' if duty is None else f'{duty}%'}"
            for slot, duty in self._target.items()
        ]
        return " ".join(parts)

    def build_payload(self, updates: Mapping[str, int | None] | None = None) -> list[int]:
        """合并目标状态并编码成 8 字节 payload。

        Args:
            updates: 风扇位 → 占空比。``None`` 表示该位交回 BMC 自动。
                未出现的位保持上一次设定的值。

        Raises:
            ValueError: 出现未知风扇位，或占空比超出取值范围。
        """
        if updates:
            unknown = set(updates) - set(FAN_SLOT_INDEX)
            if unknown:
                raise ValueError(
                    f"未知风扇位 {sorted(unknown)}，合法取值: {sorted(FAN_SLOT_INDEX)}"
                )
            self._target.update(updates)

        payload = [0x00] * PAYLOAD_LEN
        for slot, duty in self._target.items():
            payload[FAN_SLOT_INDEX[slot]] = encode_duty(duty)
        # 保留位恒 0x00（_target 里没有它，这里显式兜一层）
        payload[RESERVED_INDEX] = 0x00

        if len(payload) != PAYLOAD_LEN:  # pragma: no cover - 防御性断言
            raise AssertionError(f"payload 长度异常: {len(payload)}")
        return payload

    # ------------------------------------------------------------ 下发

    def apply(
        self,
        updates: Mapping[str, int | None] | None = None,
        timeout: float | None = None,
    ) -> CmdResult:
        """更新目标状态并下发（8 字节全量写）。

        Args:
            updates: 风扇位 → 占空比，``None`` 表示交回 BMC 自动。
                     未出现的位保持上一次设定的值（首次为自动）。
            timeout: 覆盖默认超时，给退出路径上的回退调用留更宽裕的时间。

        Returns:
            一次 ipmitool 调用的结果。
        """
        payload = self.build_payload(updates)
        args = [*CMD_PREFIX, *(_fmt_byte(b) for b in payload)]

        if self.dry_run:
            logger.info("[dry-run] 不下发，仅演示: ipmitool %s", " ".join(args))
            return CmdResult(
                args=tuple(args), returncode=0, stdout="", stderr="", duration=0.0
            )

        result = self.run(args, timeout=timeout)
        if result.ok:
            logger.info("风扇占空比已下发 | %s", self.describe_target())
        return result

    def restore_auto(self, timeout: float | None = None) -> CmdResult | None:
        """把**全部**风扇位交回 BMC 自动控制。

        这是进程退出路径上的安全回退动作，必须在任何异常/信号处理里都能跑通，
        因此这里**吞掉所有异常**（只记日志）—— 它要是把退出流程本身搞崩，
        风扇就真回不去了。
        """
        logger.warning("开始回退：全部风扇位交回 BMC 自动控制")
        try:
            self._target = {slot: AUTO for slot in FAN_SLOT_INDEX}
            result = self.apply(timeout=timeout)
            if result.ok:
                logger.info("已回退 BMC 自动控制")
            else:
                logger.error("回退命令返回非零: %s", result.summary())
            return result
        except Exception:
            logger.exception(
                "❌ 回退 BMC 自动控制失败 —— 请立即手动确认风扇状态！"
            )
            return None
