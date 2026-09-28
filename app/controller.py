"""控制回路。

每个周期做四件事：读 GPU 温度 → 查曲线算占空比 → 与上次下发值比较 →
必要时写 IPMI。看似简单，但「出错时怎么办」才是这个模块的重点：

- **单次 tick 抛异常不能让回路死掉**。上游项目就是 ``while True`` 里没有
  异常兜底，线程一崩进程还活着、但已经不再控风扇了 —— 这种静默失效
  比直接崩溃危险得多。
- **读不到温度时不要瞎猜**。既不盲目保持（可能正卡在低转速），也不盲目
  拉满（可能把凉快的机器吹成噪音源）。正确做法是**什么都不做 + 报警**：
  保持现状，把决定权交给人和 BMC。
- **降温方向要滞回**，否则温度在阈值附近抖动会让风扇转速反复横跳。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .config import AppConfig
from .curve import CurveState, FanCurve, build_curve_from_config
from .ipmi import FAN_SLOT_INDEX, GPU_COOLING_SLOTS, IPMIClient
from .safety import SafetyGuard
from .sensors import (
    BoardTemperature,
    BoardTemperatureReader,
    CPUCoreTemperature,
    CPUCoreTemperatureReader,
    FanMetricsReader,
    FanReading,
    GPUMetric,
    GPUMetricsReader,
)
from .store import Store

logger = logging.getLogger(__name__)


@dataclass
class SourceAssignment:
    """一个**散热源**分配到哪些风扇位（存在 SQLite，不来自配置文件）。

    **主体是「源」，不是「风扇位」** —— 这条是 2026-09-28 超哥纠正的，我最初写反了：

    - ❌ 旧模型（我原来写的）：风扇位 → 挑一路温度。要表达「FRNT_FAN1 给 GPU0 吹」，
      得滚到风扇那一行找下拉，是**反直觉**的。
    - ✅ 新模型：GPU → 挑它的风扇接口。用户脑子里的顺序是「**这张卡用哪个风扇吹**」，
      配置和界面就该按这个顺序组织。

    两者在「一个风扇位只被一个源占用」的前提下语义等价，但**表达顺序和界面形态
    完全不同** —— 模型必须贴合用户的心智，不能只求数学等价。

    调控**完全由这份分配驱动**：被分配的位按对应源的温度调；没被分配的位
    程序一根线都不碰（交回 BMC）。
    """

    #: 源的唯一键：``gpu:<uuid>`` 或 ``cpu``
    key: str
    #: 源类型：``gpu`` = 某张具体的卡；``cpu`` = CPU 核温度 Tctl
    kind: str = "gpu"
    #: ``kind == "gpu"`` 时的 GPU UUID（**绝不用 index** —— 这机器换过 PCI 槽位）
    gpu_uuid: str | None = None
    #: 分配给这个源的风扇位（可多个）
    slots: list[str] = field(default_factory=list)


@dataclass
class SlotStatus:
    """单个风扇位的运行状态。"""

    slot: str
    duty: int | None = None
    temperature: float | None = None
    curve_index: int | None = None
    updated_ts: float | None = None
    #: 本轮温度的来源说明（人话），如「GPU e49ed30f」/「所有卡最热」/「CPU Tctl」
    bound_detail: str = ""
    #: 绑定的 GPU UUID（供前端展示绑定关系）
    bound_uuids: list[str] = field(default_factory=list)
    #: 温度源类型（gpu / cpu）
    source: str = "gpu"
    #: 占用这个风扇位的源的 key（空 = 没被分配，程序不接管）
    owner_key: str = ""
    #: 该位占空比上限（来自配置文件）
    max_duty: int | None = None


@dataclass
class ControllerSnapshot:
    """供 API 对外暴露的运行快照。"""

    running: bool = False
    mode: str = "auto"
    interval: float = 15.0
    last_tick_ts: float | None = None
    last_tick_duration: float | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    emergency: bool = False
    slots: dict[str, SlotStatus] = field(default_factory=dict)
    gpus: list[GPUMetric] = field(default_factory=list)
    fans: dict[str, FanReading] = field(default_factory=dict)
    gpu_source: str = ""
    fan_source: str = ""
    #: BMC 板载温度（ipmi_exporter）：MB / CPU / Card Side / DDR4_*
    board_temps: dict[str, BoardTemperature] = field(default_factory=dict)
    #: CPU 核心温度（node_exporter 的 hwmon）：Tctl / Tccd*
    cpu_temps: list[CPUCoreTemperature] = field(default_factory=list)
    board_source: str = ""
    cpu_source: str = ""


class FanController:
    """GPU 温度 → 风扇占空比的闭环控制器。"""

    def __init__(
        self,
        config: AppConfig,
        ipmi: IPMIClient,
        guard: SafetyGuard,
        gpu_reader: GPUMetricsReader,
        fan_reader: FanMetricsReader,
        curve: FanCurve,
        board_reader: BoardTemperatureReader | None = None,
        cpu_reader: CPUCoreTemperatureReader | None = None,
        store: Store | None = None,
    ) -> None:
        self.config = config
        self._ipmi = ipmi
        self._guard = guard
        self._gpu_reader = gpu_reader
        self._fan_reader = fan_reader
        self._curve = curve
        self._board_reader = board_reader
        self._cpu_reader = cpu_reader
        self._store = store

        self._mode = config.control.mode
        self._emergency = False

        # --- 管控范围 ---
        # 「控制哪几张 GPU」。``None`` = 全部管控（保守默认：新插的卡自动纳入，
        # 不然新卡没人吹会热死）；非空 set = 只管这几个。设置页可改。
        self._managed_gpus: set[str] | None = None

        # --- 运行时设置 ---
        # 下面这几项都能在界面上改、并持久化到 SQLite。配置文件只提供**初始值**，
        # 之后一律以数据库为准（避免「改配置文件不生效」的双数据源困惑）。
        self._enabled = config.control.enabled
        self._interval = config.control.interval
        self._emergency_temp = config.safety.emergency_temp
        self._emergency_resume = config.safety.emergency_resume_temp
        self._curve_states: dict[str, CurveState] = {
            b.slot: CurveState() for b in config.fans
        }
        #: 各风扇位的占空比上限（来自配置文件，可以按位覆盖曲线的 max_duty）
        self._max_duty: dict[str, int | None] = {
            b.slot: b.max_duty for b in config.fans
        }

        # 分配关系是运行时状态，**权威来源是 SQLite**（见 app/store.py）。
        # 配置文件只回答一个问题：**这台机器上我要管哪几个风扇位**。
        #
        # ⚠️ 这里初始是**空的**：程序不能替用户猜「哪个风扇给哪张卡散热」——
        # 猜错就是「凉的卡吹、热的卡不吹」，是要烧硬件的。所以库里没有分配时，
        # 所有位都是**无主**状态 → 一个都不调，等用户在界面上完成初始化
        # （见 BindingWizard.vue）。
        self._assignments: dict[str, SourceAssignment] = {}
        #: ``slot → 源的 key``。控制回路按「位」下发，靠这张索引找它属于谁。
        self._slot_owner: dict[str, str] = {}
        self._bindings_configured = False
        self._snapshot = ControllerSnapshot(
            mode=self._mode,
            interval=config.control.interval,
            slots={b.slot: SlotStatus(slot=b.slot) for b in config.fans},
        )

    # ------------------------------------------------------------ 属性

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def interval(self) -> float:
        """控制周期（秒）。运行时可改 —— 循环每轮重新读取，无需重启。"""
        return self._interval

    @property
    def enabled(self) -> bool:
        return self._enabled

    def snapshot(self) -> ControllerSnapshot:
        """当前运行快照的副本。"""
        snap = self._snapshot
        return ControllerSnapshot(
            running=snap.running,
            mode=self._mode,
            interval=snap.interval,
            last_tick_ts=snap.last_tick_ts,
            last_tick_duration=snap.last_tick_duration,
            consecutive_failures=snap.consecutive_failures,
            last_error=snap.last_error,
            emergency=self._emergency,
            slots={
                slot: SlotStatus(**vars(status)) for slot, status in snap.slots.items()
            },
            gpus=list(snap.gpus),
            fans=dict(snap.fans),
            gpu_source=self._gpu_reader.last_source,
            fan_source=self._fan_reader.last_source,
            board_temps=dict(snap.board_temps),
            cpu_temps=list(snap.cpu_temps),
            board_source=snap.board_source,
            cpu_source=snap.cpu_source,
        )

    # ------------------------------------------------------------ 模式切换

    def set_mode(self, mode: str) -> None:
        """切换 ``auto`` / ``manual``。

        切回 ``auto`` 时重置曲线状态，避免拿着旧的档位索引做滞回判断。
        """
        if mode not in ("auto", "manual"):
            raise ValueError(f"不支持的模式: {mode!r}（可选 auto / manual）")
        if mode == self._mode:
            return
        logger.info("控制模式 %s → %s", self._mode, mode)
        self._mode = mode
        self._snapshot.mode = mode
        for state in self._curve_states.values():
            state.reset()

    def update_assignments(self, assignments: list[dict[str, Any]]) -> None:
        """更新「散热源 → 风扇位」的分配（内存态，持久化由调用方写 Store）。

        这是**全量覆盖**语义：传进来的就是完整的一份分配。

        校验规则：

        1. ``kind`` 必须是 ``gpu`` / ``cpu`` 之一；
        2. ``kind == "gpu"`` 必须带 ``gpu_uuid``（**UUID，绝不用 index**）；
        3. ``slots`` 里的每个风扇位必须是这台机器上**真实存在的位**
           （配置文件声明的 + 探测到在转的，见 :meth:`_ensure_slot`）；
        4. **一个风扇位只能被一个源占用** —— 冲突直接拒绝（整个更新不生效），
           否则「热的卡到底谁给它吹」就成了说不清的事；
        5. 同一个 ``key`` 出现两次 → 拒绝（key 是唯一键）。

        校验全部在**赋值之前** —— 中途抛错不会留下改了一半的状态。
        """
        if not isinstance(assignments, list):
            raise ValueError("assignments 必须是数组")

        seen_keys: set[str] = set()
        seen_slots: dict[str, str] = {}

        # ---- 第 1 遍：纯校验，不动任何状态 ----
        for item in assignments:
            key = item.get("key")
            kind = item.get("kind", "gpu")
            if not isinstance(key, str) or not key:
                raise ValueError("每个分配项都必须有非空 key")
            if key in seen_keys:
                raise ValueError(f"源 {key!r} 出现了两次")
            seen_keys.add(key)

            if kind not in ("gpu", "cpu"):
                raise ValueError(f"不支持的源类型: {kind!r}（可选 gpu / cpu）")
            if kind == "gpu":
                uuid = item.get("gpu_uuid")
                if not isinstance(uuid, str) or not uuid:
                    raise ValueError(f"kind=gpu 的源（{key!r}）必须带 gpu_uuid")
                if key != f"gpu:{uuid}":
                    raise ValueError(f"kind=gpu 的源 key 必须是 'gpu:<uuid>'（{key!r}）")
                # 打通「设置 ↔ 分配」：未纳入管控的卡不能**持有分配**，
                # 否则用户配了也不生效、界面上却看不出来，两头糊涂。
                # （slots 为空的占位条目允许 —— 启动自愈后的形态就是这样）
                if item.get("slots") and not self._is_managed(uuid):
                    raise ValueError(
                        f"GPU {uuid[4:12]} 未纳入管控（请在「设置 → 管控 GPU」里勾选），"
                        f"不能给它分配风扇"
                    )

            slots = item.get("slots", [])
            if not isinstance(slots, list):
                raise ValueError(f"源 {key!r} 的 slots 必须是数组")
            for raw_slot in slots:
                slot = str(raw_slot)
                # 校验基准 = 配置声明 ∪ 全量位表（懒建进运行时状态）。
                # ⚠️ 不能只看 self._snapshot.slots：启动时探测还没跑过（describe
                # 尚未被调用），slots 可能是空的 —— 16:15 部署后曾因此把用户
                # 15:36 存的分配整份作废，风扇落回 BMC 失明档，GPU 飙到 86°C。
                if slot not in self._snapshot.slots and slot not in FAN_SLOT_INDEX:
                    raise ValueError(f"未纳入管控的风扇位: {slot!r}")
                self._ensure_slot(slot)
                if slot in seen_slots:
                    raise ValueError(
                        f"风扇位 {slot!r} 被两个源同时占用"
                        f"（{seen_slots[slot]!r} 和 {key!r}）—— 一个位只能给一个源"
                    )
                seen_slots[slot] = key

        # ---- 第 2 遍：校验全过了才真正赋值 ----
        self._assignments = {
            item["key"]: SourceAssignment(
                key=item["key"],
                kind=item.get("kind", "gpu"),
                gpu_uuid=item.get("gpu_uuid"),
                slots=[str(s) for s in item.get("slots", [])],
            )
            for item in assignments
        }
        self._reindex()
        self._bindings_configured = True

        # 分配变化 → 档位索引全部作废（每个位的温度源可能换了）
        for state in self._curve_states.values():
            state.reset()

        logger.info(
            "分配更新: %s",
            "; ".join(
                f"{a.key} → [{', '.join(a.slots) or '未分配'}]"
                for a in self._assignments.values()
            )
            or "(空)",
        )

    def sanitize_stored_assignments(
        self, assignments: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """启动恢复前的自愈：把与当前管控范围冲突的存量分配就地修正。

        「设置 ↔ 分配」在运行时是联动清除的（见 :meth:`apply_settings`），
        但如果上次持久化失败，库里可能残留「未管控的卡还带着分配」的脏数据。
        启动恢复时**不能**因此把整份分配作废（16:56 事故的教训）——
        这里只把冲突项的 ``slots`` 清空，其余原样保留。
        """
        for item in assignments:
            if (
                item.get("kind") == "gpu"
                and item.get("gpu_uuid")
                and not self._is_managed(item["gpu_uuid"])
                and item.get("slots")
            ):
                logger.warning(
                    "启动自愈：GPU %s 未纳入管控，忽略其存量分配 %s",
                    item["gpu_uuid"][4:12],
                    item["slots"],
                )
                item["slots"] = []
        return assignments

    def _reindex(self) -> None:
        """重建 ``slot → 源 key`` 的索引（控制回路靠它按位找温度）。"""
        self._slot_owner = {
            slot: key
            for key, a in self._assignments.items()
            for slot in a.slots
        }
        # 把归属同步进每个位的运行状态（给前端展示「这个位被谁占着」）
        for slot, status in self._snapshot.slots.items():
            owner = self._slot_owner.get(slot, "")
            status.owner_key = owner
            assignment = self._assignments.get(owner) if owner else None
            status.source = assignment.kind if assignment else "gpu"
            status.bound_uuids = (
                [assignment.gpu_uuid] if assignment and assignment.gpu_uuid else []
            )

    def apply_settings(self, settings: dict[str, Any]) -> None:
        """应用运行时设置（**部分更新**语义 —— 只改传进来的键）。

        支持的键：

        ================================  ==========================================
        ``control.enabled``               控制总开关。关闭 = 完全不调档，风扇保持现状
        ``control.interval``              控制周期（秒），1~3600
        ``curve``                         整条曲线 ``{points, hysteresis, min_duty, max_duty}``
        ``safety.emergency_temp``         紧急散热触发温度
        ``safety.emergency_resume_temp``  紧急散热解除温度
        ================================  ==========================================

        Raises:
            ValueError: 值非法（API 层会转成 400）。
        """
        if "control.enabled" in settings:
            self._enabled = bool(settings["control.enabled"])
            logger.info("控制总开关 → %s", "开启" if self._enabled else "关闭")

        if "control.interval" in settings:
            value = float(settings["control.interval"])
            if not 1 <= value <= 3600:
                raise ValueError("控制周期需在 1~3600 秒之间")
            self._interval = value
            logger.info("控制周期 → %.1f 秒", value)

        if "curve" in settings:
            raw = settings["curve"] or {}
            points = raw.get("points") or []
            if not points:
                raise ValueError("曲线至少要有一个折点")
            self._curve = build_curve_from_config(
                points,
                hysteresis=float(raw.get("hysteresis", 3.0)),
                min_duty=int(raw.get("min_duty", 20)),
                max_duty=int(raw.get("max_duty", 100)),
            )
            for state in self._curve_states.values():
                state.reset()
            logger.info("控制曲线已更新（%d 个折点）", len(points))

        if "safety.emergency_temp" in settings:
            self._emergency_temp = float(settings["safety.emergency_temp"])
        if "safety.emergency_resume_temp" in settings:
            self._emergency_resume = float(settings["safety.emergency_resume_temp"])

        if "control.managed_gpus" in settings:
            value = settings["control.managed_gpus"]
            if not isinstance(value, list) or not all(
                isinstance(u, str) for u in value
            ):
                raise ValueError("control.managed_gpus 必须是 UUID 字符串数组")
            # 空数组 = 全部管控（保守默认）；非空 = 只管列出的这几张
            self._managed_gpus = set(value) if value else None
            logger.info(
                "管控 GPU 范围 → %s",
                f"指定 {len(value)} 张" if value else "全部",
            )

            # 打通「设置 ↔ 分配」：被移出管控的卡，其分配**立即停用并清除**。
            # 不留「配了但不生效」的暗状态 —— 重新勾选后需要重新分配。
            for a in self._assignments.values():
                if (
                    a.kind == "gpu"
                    and a.gpu_uuid
                    and not self._is_managed(a.gpu_uuid)
                    and a.slots
                ):
                    logger.info(
                        "GPU %s 已移出管控，其分配 %s 已停用清除",
                        a.gpu_uuid[4:12],
                        a.slots,
                    )
                    a.slots = []
            self._reindex()

    def export_settings(self) -> dict[str, Any]:
        """导出当前运行时设置（供持久化到 SQLite / 给前端展示）。"""
        return {
            "control.enabled": self._enabled,
            "control.interval": self._interval,
            "control.managed_gpus": sorted(self._managed_gpus or []),
            "curve": self._curve.describe(),
            "safety.emergency_temp": self._emergency_temp,
            "safety.emergency_resume_temp": self._emergency_resume,
        }

    def _is_managed(self, gpu_uuid: str) -> bool:
        """该 GPU 是否被纳入管控（``None`` = 全部管控）。"""
        return self._managed_gpus is None or gpu_uuid in self._managed_gpus

    def set_manual_duty(self, slot: str, duty: int | None) -> None:
        """手动设定某个风扇位的占空比（仅在 ``manual`` 模式下允许）。

        ``duty=None`` 表示把该位交回 BMC 自动。
        """
        if self._mode != "manual":
            raise RuntimeError("当前不是手动模式，请先切换到 manual")
        if slot not in self._snapshot.slots:
            raise ValueError(f"未纳入管控的风扇位: {slot!r}")

        if not self._guard.engaged:
            self._guard.engage()

        self._ipmi.apply({slot: duty})
        status = self._snapshot.slots[slot]
        status.duty = duty
        status.updated_ts = time.time()

        if self._store is not None:
            self._store.log(
                "ipmi_write",
                "manual",
                f"{slot}={'auto' if duty is None else f'{duty}%'}",
            )

    # ------------------------------------------------------------ 主循环

    async def run(self) -> None:
        """控制回路主循环（asyncio 后台任务）。"""
        logger.info(
            "控制回路启动 | 周期 %.1fs | 模式 %s | 管控风扇位 %s",
            self.interval,
            self._mode,
            ", ".join(self._snapshot.slots),
        )
        self._snapshot.running = True
        try:
            while True:
                try:
                    # IPMI 与 HTTP 都是阻塞调用，丢到线程池里跑，别堵住事件循环
                    await asyncio.to_thread(self.tick)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # 单次失败绝不能终止回路 —— 这是上游最致命的坑
                    logger.exception("控制周期出现未捕获异常（已吞掉，继续运行）")
                    self._snapshot.last_error = f"tick 异常: {exc}"
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            logger.info("控制回路收到取消信号，退出")
            raise
        finally:
            self._snapshot.running = False

    # ------------------------------------------------------------ 单次执行

    def tick(self) -> None:
        """执行一个控制周期。可单独调用，方便测试与排障。"""
        started = time.monotonic()

        gpus = self._gpu_reader.read()
        fans = self._fan_reader.read()

        # 温度是**附加信息**：读不到不影响控速（控速只看 GPU 温度），
        # 所以放在最后，失败也不计入 consecutive_failures
        if self._board_reader is not None:
            self._snapshot.board_temps = self._board_reader.read()
            self._snapshot.board_source = self._board_reader.last_source
        if self._cpu_reader is not None:
            self._snapshot.cpu_temps = self._cpu_reader.read()
            self._snapshot.cpu_source = self._cpu_reader.last_source

        self._snapshot.gpus = gpus
        self._snapshot.fans = fans
        self._snapshot.gpu_source = self._gpu_reader.last_source
        self._snapshot.fan_source = self._fan_reader.last_source

        if not gpus:
            self._handle_blind("读不到任何 GPU 指标")
        else:
            self._snapshot.consecutive_failures = 0
            if self._mode == "auto" and self._enabled:
                self._apply_curve(gpus)
            elif self._mode == "manual":
                logger.debug("手动模式，跳过自动调档")
            elif not self._enabled:
                logger.debug("控制总开关已关闭，跳过自动调档")

        self._snapshot.last_tick_ts = time.time()
        self._snapshot.last_tick_duration = time.monotonic() - started
        self._guard.beat(self._heartbeat_note())

    # ------------------------------------------------------------ 曲线下发

    def _apply_curve(self, gpus: list[GPUMetric]) -> None:
        # 分配还没配置 → **不放任 BMC 失明档烤卡**。
        # 能走到这里说明总开关已开 —— 用户明确要求了自动控制，此时 BMC 的
        # 自动档对 GPU 完全失明（它读不到 GPU 温度），放着不管就是 16:56
        # 那种 86°C 险情。兜底：所有 GPU 散热位跟随最热卡跑同一条曲线
        # （只会多吹、不会漏吹）。用户在界面上完成分配后立即切换到精确分配。
        if not self._bindings_configured:
            logger.warning(
                "分配尚未配置 —— 兜底生效：GPU 散热位 %s 跟随最热卡跑曲线"
                "（完成「风扇分配」后自动切换为精确控制）",
                ", ".join(GPU_COOLING_SLOTS),
            )
            self._run_fallback(gpus)
            return

        # 没被任何源接管的卡：它的散热完全依赖 BMC 自动档，而 BMC 读不到 GPU
        # 温度（这台机器的现实）。过热时必须把这件事说破，否则用户会以为
        # 「程序在管」—— 实际上一个风扇都没分给它。
        self._warn_orphan_gpus(gpus)

        # 紧急判定只看**被分配且纳入管控**的源 —— 只有它们驱动的位是程序能动的
        assigned_temps: list[float] = [
            g.temperature
            for g in gpus
            if g.temperature is not None
            and f"gpu:{g.uuid}" in self._assignments
            and self._is_managed(g.uuid)
        ]
        if any(a.kind == "cpu" for a in self._assignments.values()):
            tctl = next(
                (t for t in self._snapshot.cpu_temps if t.label == "Tctl"), None
            )
            if tctl is not None and tctl.celsius is not None:
                assigned_temps.append(tctl.celsius)
        hottest = max(assigned_temps) if assigned_temps else None

        if hottest is not None:
            self._update_emergency(hottest)

        updates: dict[str, int | None] = {}
        pending: list[tuple[str, int]] = []

        for assignment in self._assignments.values():
            if not assignment.slots:
                continue

            # 未纳入管控的 GPU（设置页勾掉的）：它的分配保留但**不驱动**，
            # 风扇实际行为交回 BMC —— 用户明确说不管它，就不碰
            if (
                assignment.kind == "gpu"
                and assignment.gpu_uuid
                and not self._is_managed(assignment.gpu_uuid)
            ):
                for slot in assignment.slots:
                    status = self._snapshot.slots[slot]
                    status.bound_detail = "GPU 未纳入管控（设置页可改）"
                    status.temperature = None
                continue

            temp, detail = self._resolve_temperature(assignment, gpus)

            # ⚠️ 拿不到温度（掉卡）→ **交回 BMC 自动**，而不是拉满狂转。
            # 掉卡的卡本身已经不发热了，为它狂转只是噪音；把位交回 BMC，
            # 由它按机箱内其他传感器维持基本风道。卡恢复上线后自动重新接管。
            # （2026-09-28 17:20 超哥定：掉卡保持默认就好。）
            if temp is None:
                logger.warning(
                    "⚠️ %s 取不到温度（%s）—— 该风扇位交回 BMC 自动控制",
                    assignment.key,
                    detail,
                )
                for slot in assignment.slots:
                    status = self._snapshot.slots[slot]
                    status.temperature = None
                    status.bound_detail = detail
                    # 只在当前不是自动时才写，避免每轮重复打扰 BMC
                    if status.duty is not None:
                        updates[slot] = None
                        pending.append((slot, 0))
                        # 档位作废：恢复上线时按当时温度重新定档
                        self._curve_states[slot].reset()
                continue

            for slot in assignment.slots:
                status = self._snapshot.slots[slot]
                status.bound_detail = detail
                status.temperature = temp
                state = self._curve_states.setdefault(slot, CurveState())

                if self._emergency:
                    duty = 100
                    logger.warning(
                        "🚨 紧急状态：%s 直接拉满（%s）",
                        slot,
                        detail,
                    )
                else:
                    duty = self._curve.step(temp, state)

                max_duty = self._max_duty.get(slot)
                if max_duty is not None:
                    duty = min(duty, max_duty)

                status.curve_index = state.index

                # 变化不够大就别打扰 BMC
                if (
                    status.duty is not None
                    and not self._emergency
                    and abs(duty - status.duty) < self.config.control.min_write_delta
                ):
                    continue

                updates[slot] = duty
                pending.append((slot, duty))

        # 分配之外的位一根线都不碰 —— 这是「未分配 = 交回 BMC」的承诺
        if not updates:
            return

        if not self._guard.engaged:
            # 关键顺序：先武装护栏，再第一次写手动值。
            # 反过来的话，两次调用之间崩溃就没人负责回退了。
            self._guard.engage()

        result = self._ipmi.apply(updates)
        detail = ", ".join(f"{slot}={duty}%" for slot, duty in pending)

        if result.ok:
            now = time.time()
            for slot, duty in pending:
                self._snapshot.slots[slot].duty = duty
                self._snapshot.slots[slot].updated_ts = now
            if self._store is not None:
                self._store.log("ipmi_write", "auto_curve", detail, ok=True)
        else:
            self._snapshot.last_error = f"下发失败: {result.summary()}"
            if self._store is not None:
                self._store.log(
                    "ipmi_write",
                    "auto_curve",
                    f"{detail} | {result.summary()}",
                    ok=False,
                )

    def _run_fallback(self, gpus: list[GPUMetric]) -> None:
        """分配未配置时的安全兜底：所有 GPU 散热位跟随**最热卡**跑曲线。

        语义（超哥 16:56 明确要求）：总开关开了，就该按曲线控制，
        而不是把风扇扔给读不到 GPU 温度的 BMC 自动档。
        """
        temps = [g.temperature for g in gpus if g.temperature is not None]
        if not temps:
            self._handle_blind("兜底模式：GPU 指标里没有任何温度读数")
            return

        hottest = max(temps)
        self._update_emergency(hottest)

        updates: dict[str, int | None] = {}
        pending: list[tuple[str, int]] = []

        for slot in GPU_COOLING_SLOTS:
            if slot not in self._snapshot.slots and slot not in FAN_SLOT_INDEX:
                continue
            self._ensure_slot(slot)
            status = self._snapshot.slots[slot]
            status.owner_key = "__fallback__"
            status.source = "gpu"
            status.bound_uuids = []
            status.bound_detail = f"兜底 · 跟随最热卡（{hottest:.0f}°C）"
            status.temperature = hottest

            state = self._curve_states.setdefault(slot, CurveState())
            if self._emergency:
                duty = 100
                logger.warning("🚨 紧急状态：%s 兜底直接拉满", slot)
            else:
                duty = self._curve.step(hottest, state)

            max_duty = self._max_duty.get(slot)
            if max_duty is not None:
                duty = min(duty, max_duty)
            status.curve_index = state.index

            # 变化不够大就别打扰 BMC
            if (
                status.duty is not None
                and not self._emergency
                and abs(duty - status.duty) < self.config.control.min_write_delta
            ):
                continue

            updates[slot] = duty
            pending.append((slot, duty))

        if not updates:
            return

        if not self._guard.engaged:
            self._guard.engage()

        result = self._ipmi.apply(updates)
        detail = ", ".join(f"{slot}={duty}%" for slot, duty in pending)

        if result.ok:
            now = time.time()
            for slot, duty in pending:
                st = self._snapshot.slots[slot]
                st.duty = duty
                st.updated_ts = now
            if self._store is not None:
                self._store.log("ipmi_write", "auto_fallback", detail, ok=True)
        else:
            self._snapshot.last_error = f"兜底下发失败: {result.summary()}"
            if self._store is not None:
                self._store.log(
                    "ipmi_write",
                    "auto_fallback",
                    f"{detail} | {result.summary()}",
                    ok=False,
                )

    def _warn_orphan_gpus(self, gpus: list[GPUMetric]) -> None:
        """告警「没有任何风扇分给它的 GPU」—— 它的散热只剩 BMC 自动档兜底。

        只针对**纳入管控**的卡：用户在设置页主动排除的卡不唠叨。
        """
        for g in gpus:
            if f"gpu:{g.uuid}" in self._assignments:
                continue
            if not self._is_managed(g.uuid):
                continue
            if g.temperature is not None and g.temperature >= self._emergency_resume:
                logger.error(
                    "⚠️ GPU %s（%.0f°C）没有被分配任何风扇位 —— "
                    "它现在只有 BMC 自动档在散热，而 BMC 读不到 GPU 温度。"
                    "请到界面上给它分配风扇",
                    g.short_uuid,
                    g.temperature,
                )

    def export_assignments(self) -> list[dict[str, Any]]:
        """导出当前分配（供持久化到 SQLite / 给前端展示）。"""
        return [
            {
                "key": a.key,
                "kind": a.kind,
                "gpu_uuid": a.gpu_uuid,
                "slots": list(a.slots),
            }
            for a in self._assignments.values()
        ]

    def _describe_assignments(self, gpus: list[GPUMetric]) -> list[dict[str, Any]]:
        """给前端的分配视图。

        **每张上报的 GPU 都是一个源**（哪怕还没分配风扇也列出来，slots 为空），
        外加一个 CPU 核温度源。

        配置过但当前离线的 GPU **照样列出**（标记 online=False）—— 否则它占着
        的风扇位会在界面上凭空消失，用户会以为丢配置了。
        """
        by_key = {a.key: a for a in self._assignments.values()}
        result: list[dict[str, Any]] = []

        # ① 每张上报的 GPU
        for g in gpus:
            key = f"gpu:{g.uuid}"
            stored = by_key.get(key)
            result.append(
                {
                    "key": key,
                    "kind": "gpu",
                    "gpu_uuid": g.uuid,
                    "label": f"GPU {g.short_uuid} · {g.model_name}",
                    "slots": list(stored.slots) if stored else [],
                    "temperature": g.temperature,
                    "online": True,
                    "managed": self._is_managed(g.uuid),
                }
            )

        # ② 配置过但离线的 GPU（掉卡）
        for key, stored in by_key.items():
            if stored.kind != "gpu" or not stored.gpu_uuid:
                continue
            if any(g.uuid == stored.gpu_uuid for g in gpus):
                continue
            result.append(
                {
                    "key": key,
                    "kind": "gpu",
                    "gpu_uuid": stored.gpu_uuid,
                    "label": f"GPU {stored.gpu_uuid[4:12]} · 离线（掉卡？）",
                    "slots": list(stored.slots),
                    "temperature": None,
                    "online": False,
                    "managed": self._is_managed(stored.gpu_uuid),
                }
            )

        # ③ 合成源：CPU 核温度
        cpu = by_key.get("cpu")
        result.append(
            {
                "key": "cpu",
                "kind": "cpu",
                "gpu_uuid": None,
                "label": "CPU 核温度（Tctl）",
                "slots": list(cpu.slots) if cpu else [],
                "temperature": None,
                "online": True,
                "managed": True,
            }
        )

        return result

    def _resolve_temperature(
        self, assignment: SourceAssignment, gpus: list[GPUMetric]
    ) -> tuple[float | None, str]:
        """解析某个**源**当前的温度。

        Returns:
            ``(温度, 人话说明)``。温度为 ``None`` 表示这一路取不到 ——
            调用方会据此走「保守拉满」的降级分支。
        """
        # ① CPU 源
        if assignment.kind == "cpu":
            tctl = next(
                (t for t in self._snapshot.cpu_temps if t.label == "Tctl"), None
            )
            if tctl is None or tctl.celsius is None:
                return None, "CPU 温度（Tctl）不可用"
            return tctl.celsius, "CPU Tctl"

        # ② 具体某张 GPU
        gpu = next((g for g in gpus if g.uuid == assignment.gpu_uuid), None)
        if gpu is None or gpu.temperature is None:
            return (
                None,
                f"GPU {assignment.key[4:12]} 不在上报列表中（掉卡或未接入？）",
            )
        return gpu.temperature, f"GPU {gpu.short_uuid}"

    def _update_emergency(self, hottest: float) -> None:
        """维护紧急状态（进入和解除都要滞回，否则会在临界点反复横跳）。"""
        if not self._emergency:
            if hottest >= self._emergency_temp:
                logger.error(
                    "🚨 进入紧急散热：%.1f°C ≥ %.1f°C",
                    hottest,
                    self._emergency_temp,
                )
                self._emergency = True
        elif hottest <= self._emergency_resume:
            logger.warning(
                "紧急散热解除：%.1f°C ≤ %.1f°C",
                hottest,
                self._emergency_resume,
            )
            self._emergency = False
            # 解除后重置曲线状态，重新从当前温度定档
            for state in self._curve_states.values():
                state.reset()

    # ------------------------------------------------------------ 失明处理

    def _handle_blind(self, reason: str) -> None:
        """读不到温度时的处理：**什么都不做 + 报警**。

        保持现状是这里唯一合理的选择——盲目拉满会把凉快的机器吹成噪音源，
        盲目按旧值调档则可能一路降速。把决定权留给人。
        """
        self._snapshot.consecutive_failures += 1
        self._snapshot.last_error = reason
        failures = self._snapshot.consecutive_failures

        if failures == 1:
            logger.warning("GPU 指标读取失败: %s", reason)
        if failures >= self.config.control.max_consecutive_failures:
            logger.error(
                "⚠️ 已连续 %d 次读不到 GPU 温度 —— 保持当前占空比不动，"
                "请在界面上确认散热是否正常（当前管控位: %s）",
                failures,
                ", ".join(
                    f"{slot}={status.duty if status.duty is not None else 'auto'}"
                    for slot, status in self._snapshot.slots.items()
                ),
            )

    # ------------------------------------------------------------ 杂项

    def _heartbeat_note(self) -> str:
        parts = [
            f"{slot}:{'auto' if s.duty is None else f'{s.duty}%'}"
            for slot, s in self._snapshot.slots.items()
        ]
        return " ".join(parts)

    def _ensure_slot(self, slot: str) -> SlotStatus:
        """确保某个风扇位的运行状态存在（探测到新位时**懒创建**）。

        为什么不靠配置文件：风扇位清单应该是**探测出来的**，不是部署时写死的
        （2026-09-28 超哥指出：界面固定两个位没道理，机器上明明有 4 个在转）。
        现在的规则 —— ``fan_slots`` = 配置声明的位 ∪ ipmi_exporter 实测到
        有读数的位，每轮 describe() 都会刷新，用户在界面上看到的就是
        这台机器真实存在的全部风扇接口，勾谁控谁。
        """
        status = self._snapshot.slots.get(slot)
        if status is None:
            status = SlotStatus(slot=slot)
            self._snapshot.slots[slot] = status
            logger.info("探测到风扇位 %s（自动纳入可选清单）", slot)
        self._curve_states.setdefault(slot, CurveState())
        return status

    def describe(self) -> dict[str, Any]:
        """给前端的完整状态描述。"""
        snap = self.snapshot()
        # 风扇位**全部列出**（包括没有转速读数的）—— 控制器能写的位就是
        # FAN_SLOT_INDEX 覆盖的这些，少列一个用户就少一个可选项
        # （2026-09-28 超哥要求：没转速的也要展示）。
        for slot in FAN_SLOT_INDEX:
            self._ensure_slot(slot)
        return {
            "running": snap.running,
            "mode": snap.mode,
            "interval": snap.interval,
            "emergency": snap.emergency,
            "last_tick_ts": snap.last_tick_ts,
            "last_error": snap.last_error,
            "consecutive_failures": snap.consecutive_failures,
            "sources": {"gpu": snap.gpu_source, "fan": snap.fan_source},
            "gpus": [
                {
                    "uuid": g.uuid,
                    "short_uuid": g.short_uuid,
                    "index": g.index,
                    "pci_bus_id": g.pci_bus_id,
                    "model": g.model_name,
                    "temperature": g.temperature,
                    "power_watts": g.power_watts,
                    "utilization": g.utilization,
                    "memory_used_mib": g.memory_used_mib,
                    "memory_total_mib": g.memory_total_mib,
                    "memory_percent": g.memory_percent,
                }
                for g in snap.gpus
            ],
            "fans": [
                {
                    "slot": status.slot,
                    "duty": status.duty,
                    "temperature": status.temperature,
                    "curve_index": status.curve_index,
                    "updated_ts": status.updated_ts,
                    "source": status.source,
                    "owner_key": status.owner_key,
                    "bound_uuids": status.bound_uuids,
                    "bound_detail": status.bound_detail,
                    "rpm": (
                        snap.fans[status.slot].rpm
                        if status.slot in snap.fans
                        else None
                    ),
                }
                for status in snap.slots.values()
            ],
            # 「散热源 → 风扇位」的分配（**主体是源**，界面上按 GPU 一行一行选风扇）
            "assignments": self._describe_assignments(snap.gpus),
            #: 全部风扇位 + 各自当前转速（没有读数的位 rpm=null，照样列出）
            "fan_slots": [
                {
                    "slot": slot,
                    "rpm": (snap.fans[slot].rpm if slot in snap.fans else None),
                }
                for slot in sorted(self._snapshot.slots.keys())
            ],
            #: false = 首次使用，前端应弹分配向导，且控制器不会调档
            "bindings_configured": self._bindings_configured,
            "binding_options": {
                "gpus": [
                    {
                        "uuid": g.uuid,
                        "short_uuid": g.short_uuid,
                        "label": f"{g.short_uuid} · {g.model_name}",
                    }
                    for g in snap.gpus
                ],
                "cpu_label": "CPU 核温度（Tctl）",
            },
            "curve": self._curve.describe(),
            # 运行时设置（可在界面上改，落 SQLite）
            "settings": self.export_settings(),
            "ipmi_target": self._ipmi.target_state,
            # 温度：CPU 核（node_exporter）+ 板载（BMC）。
            # ⚠️ GPU 温度不在这里 —— 它是控速的输入，放在 gpus[] 里。
            "temperatures": {
                "cpu_cores": [
                    {"label": t.label, "celsius": t.celsius}
                    for t in snap.cpu_temps
                ],
                "board": [
                    {"name": t.name, "celsius": t.celsius, "state": t.state}
                    for t in sorted(
                        snap.board_temps.values(),
                        key=lambda x: (x.celsius is None, -(x.celsius or 0)),
                    )
                ],
                "sources": {"cpu": snap.cpu_source, "board": snap.board_source},
            },
        }
