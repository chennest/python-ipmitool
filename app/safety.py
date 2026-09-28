"""安全护栏 —— 本应用最不该省的一块。

手动占空比是一条「单行道」：程序异常退出而没回退的话，风扇会**永远停在**
最后一次写入的转速上。BMC 的热保护最终会介入，但中间那段高温窗口足以
损伤硬件。所以这里的职责只有一件事 —— **保证任何可捕获的退出路径都能把
风扇交回 BMC 自动控制**。

覆盖的退出路径：

===============  ==========================================================
路径             手段
===============  ==========================================================
正常结束          ``close()`` / ``with`` 退出
未捕获异常        ``try/finally`` + ``atexit``
SIGTERM/SIGINT   信号处理器（systemd stop、Ctrl-C）
SIGKILL / 断电   **捕获不到**，只能靠独立看门狗检查心跳文件兜底
===============  ==========================================================

最后一行是设计上的硬伤，不可能在进程内解决 —— 所以心跳文件 + 外部看门狗
是**必需项**，不是可选优化。配套的看门狗见 ``app/deploy/fan-watchdog.sh``。
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import threading
import time
from pathlib import Path
from types import FrameType
from typing import Iterable, Sequence

from .ipmi import GPU_COOLING_SLOTS, IPMIClient

logger = logging.getLogger(__name__)

#: 默认心跳超时（秒）。看门狗脚本用同一个值，改这里要顺手改脚本。
DEFAULT_HEARTBEAT_TIMEOUT: float = 120.0


class SafetyGuard:
    """风扇接管状态跟踪 + 回退保证。

    用法::

        guard = SafetyGuard(ipmi, heartbeat_path=Path("/run/fan-console/heartbeat"))
        guard.install_signal_handlers()
        with guard:
            guard.engage()
            ipmi.apply({"FRNT_FAN1": 60})
            ...
        # 退出 with 时自动回退
    """

    def __init__(
        self,
        ipmi: IPMIClient,
        heartbeat_path: Path | None = None,
        restore_timeout: float = 15.0,
    ) -> None:
        self._ipmi = ipmi
        self._heartbeat_path = heartbeat_path
        self._restore_timeout = restore_timeout

        self._lock = threading.RLock()
        self._engaged = False
        self._closed = False

    # ------------------------------------------------------------ 上下文管理

    def __enter__(self) -> "SafetyGuard":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        reason = "normal-exit" if exc_type is None else f"exception-{exc_type.__name__}"
        self.close(reason=reason)
        return False  # 不吞异常，让它继续往上冒

    # ------------------------------------------------------------ 接管与回退

    @property
    def engaged(self) -> bool:
        """是否已经接管过风扇（即写过手动占空比）。"""
        return self._engaged

    @property
    def closed(self) -> bool:
        return self._closed

    def engage(self) -> None:
        """标记「开始接管风扇」，并注册 ``atexit`` 兜底。

        必须在**第一次写入手动占空比之前**调用，否则进程崩溃时护栏不会触发。
        """
        with self._lock:
            if self._engaged:  # 幂等，别重复注册 atexit
                return
            self._engaged = True
        atexit.register(self.close, "atexit")
        logger.info("安全护栏已激活：进程退出时将把风扇交回 BMC 自动控制")

    def close(self, reason: str = "normal-exit") -> None:
        """回退到 BMC 自动控制。**幂等**，可以随便重复调用。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            was_engaged = self._engaged

        if not was_engaged:
            logger.debug("未接管过风扇，无需回退（原因: %s）", reason)
            return

        logger.warning("触发安全回退（原因: %s）", reason)
        self._ipmi.restore_auto(timeout=self._restore_timeout)
        # 正常退出要清心跳 —— 否则看门狗 2 分钟后会对着过期心跳做一次
        # 多余的回落，还留下「服务卡死」的误导日志（2026-09-28 审计发现）
        self.clear_heartbeat()

    # ------------------------------------------------------------ 信号处理

    def install_signal_handlers(
        self, signals: Sequence[signal.Signals] = (signal.SIGTERM, signal.SIGINT)
    ) -> None:
        """接管 SIGTERM / SIGINT，先回退再退出。

        注意 ``SIGKILL`` 无法捕获 —— 那正是需要外部看门狗的原因。
        """
        for sig in signals:
            signal.signal(sig, self._make_handler(sig))
        logger.debug("已接管信号: %s", ", ".join(s.name for s in signals))

    def _make_handler(self, sig: signal.Signals):
        def handler(signum: int, frame: FrameType | None) -> None:
            name = signal.Signals(signum).name
            logger.warning("收到 %s，执行安全回退", name)
            self.close(reason=f"signal-{name}")
            # 复位为默认处理再把信号重发给自己，让进程以标准语义终止
            # （保留正确的退出码，systemd 那边才判断得准）
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        return handler

    # ------------------------------------------------------------ 心跳

    def beat(self, note: str = "") -> None:
        """更新心跳文件。

        独立看门狗据此判断本进程是否还活着。写的是「原子替换」的文件，
        避免看门狗读到写了一半的内容。
        """
        if self._heartbeat_path is None:
            return
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._heartbeat_path.with_name(self._heartbeat_path.name + ".tmp")
            tmp.write_text(f"{time.time():.3f}\n{note}\n", encoding="utf-8")
            tmp.replace(self._heartbeat_path)
        except OSError:
            # 心跳写不出去不该拖垮控制回路，但要留痕
            logger.exception("写心跳文件失败: %s", self._heartbeat_path)

    def clear_heartbeat(self) -> None:
        """进程正常退出时清掉心跳，看门狗见不到文件就不会误触发。"""
        if self._heartbeat_path is None:
            return
        try:
            self._heartbeat_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("清理心跳文件失败: %s", self._heartbeat_path)

    # ------------------------------------------------------------ 紧急动作

    def force_full_speed(self, slots: Iterable[str] = GPU_COOLING_SLOTS) -> bool:
        """紧急全速。温度失控或传感器读不到时的最后手段。

        这是**加**散热方向的操作，不存在「调错会烧硬件」的风险，
        所以允许在检测到异常时自动触发。
        """
        try:
            logger.error("🚨 触发紧急全速: %s", ", ".join(slots))
            result = self._ipmi.apply({slot: 100 for slot in slots})
            return bool(result.ok)
        except Exception:
            logger.exception("紧急全速执行失败")
            return False
