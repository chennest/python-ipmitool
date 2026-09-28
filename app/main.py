"""FastAPI 应用入口。

单进程承担三件事：

1. **控制回路** —— 后台 asyncio 任务（``controller.run()``）
2. **API 服务** —— REST + WebSocket（``api.router``）
3. **静态托管** —— 直接把前端构建产物挂在根路径

进程退出路径是这里最需要小心的地方：``lifespan`` 的 ``finally`` 必须
无条件执行 ``guard.close()``。uvicorn 自己会接 SIGTERM 并触发优雅关闭，
所以我们不抢它的信号处理，只依靠 ``atexit``（``SafetyGuard.engage`` 时
注册）作为第二道保险。真正的兜底是那个独立的心跳看门狗 ——
SIGKILL 和断电在进程内是不可能捕获的。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router as api_router
from .config import load_config
from .controller import FanController
from .curve import build_curve_from_config
from .ipmi import IPMIClient
from .safety import SafetyGuard
from .sensors import (
    BoardTemperatureReader,
    CPUCoreTemperatureReader,
    FanMetricsReader,
    GPUMetricsReader,
)
from .store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

#: 前端构建产物目录（``vite build`` 的默认输出）
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    store = Store()

    ipmi = IPMIClient(
        binary=config.sources.ipmitool_binary,
        remote=config.sources.ipmi_remote,
        timeout=config.sources.ipmi_timeout,
    )
    guard = SafetyGuard(
        ipmi,
        heartbeat_path=Path(config.safety.heartbeat_path),
        restore_timeout=config.safety.restore_timeout,
    )
    gpu_reader = GPUMetricsReader(
        dcgm_endpoint=config.sources.dcgm_endpoint,
        timeout=config.sources.http_timeout,
    )
    fan_reader = FanMetricsReader(
        exporter_endpoint=config.sources.ipmi_exporter_endpoint,
        ipmi_binary=config.sources.ipmitool_binary,
        timeout=config.sources.http_timeout,
    )
    curve = build_curve_from_config(
        config.curve.points,
        hysteresis=config.curve.hysteresis,
        min_duty=config.curve.min_duty,
        max_duty=config.curve.max_duty,
    )
    board_reader = BoardTemperatureReader(
        exporter_endpoint=config.sources.ipmi_exporter_endpoint,
        ipmi_binary=config.sources.ipmitool_binary,
        timeout=config.sources.http_timeout,
    )
    cpu_reader = CPUCoreTemperatureReader(
        exporter_endpoint=config.sources.node_exporter_endpoint,
        ipmi_binary=config.sources.ipmitool_binary,
        timeout=config.sources.http_timeout,
    )
    controller = FanController(
        config,
        ipmi,
        guard,
        gpu_reader,
        fan_reader,
        curve,
        board_reader=board_reader,
        cpu_reader=cpu_reader,
        store=store,
    )

    # 运行时设置：**SQLite 是唯一权威**，config.yaml 只充当首次初始化的种子。
    # 启动时先应用库里已有的键，再把最终生效值**全量回写**——
    # 这样任何运行时状态（开关/模式/周期/曲线/阈值/管控范围）重启后都从库恢复，
    # 不存在「回退到配置文件」的暗路径（超哥 20:19 明确要求）。
    # ⚠️ 这一段必须先于分配恢复 —— 管控范围（managed_gpus）是分配校验的前提，
    #    顺序反了的话，未管控卡的存量分配会把整份恢复作废（16:56 事故根因之一）。
    stored_settings = store.load_settings()
    if stored_settings:
        try:
            controller.apply_settings(stored_settings)
            logger.info(
                "已应用数据库里的运行时设置: %s", ", ".join(sorted(stored_settings))
            )
        except (ValueError, TypeError) as exc:
            logger.error("数据库里的设置无效（%s）—— 缺失的键用配置文件种子补齐", exc)
    try:
        # 库里没有的键（首次启动 / 新增设置项）由配置文件种子补上并落库
        seed = controller.export_settings()
        store.save_settings(seed)
        newly = sorted(set(seed) - set(stored_settings or {}))
        if newly:
            logger.info("设置项首次落库（来自配置文件种子）: %s", ", ".join(newly))
    except Exception:  # noqa: BLE001 - 回写失败不影响启动
        logger.exception("设置回写 SQLite 失败")

    # 「散热源 → 风扇位」的分配以数据库为**唯一权威**（配置文件只声明管控哪些位）。
    #
    # ⚠️ 库里空的时候**不写任何默认值** —— 首次使用必须由用户在界面上亲自给
    # 每张 GPU 挑风扇接口，程序不替人猜（猜错 = 某张卡散热不足）。在用户完成
    # 之前 controller 保持未配置状态、不调档，前端会弹分配初始化向导。
    stored = store.load_assignments()
    if stored:
        # 与管控范围冲突的存量分配先自愈（清空冲突项），再整体恢复 ——
        # 保证「设置 ↔ 分配」在后端也是一条完整链路
        stored = controller.sanitize_stored_assignments(stored)
        try:
            controller.update_assignments(stored)
            logger.info("已应用数据库里的分配（%d 条）", len(stored))
        except ValueError as exc:
            logger.error(
                "数据库里的分配无效（%s）—— 保持未配置状态，请在界面上重新分配", exc
            )
    else:
        logger.warning(
            "数据库中还没有分配配置 —— 控制器暂不调档，"
            "请在界面上完成「风扇分配初始化」（首次使用必做）"
        )

    app.state.config = config
    app.state.ipmi = ipmi
    app.state.guard = guard
    app.state.controller = controller
    app.state.store = store

    logger.info(
        "GPU 风扇控制台 v%s | 模式=%s | 周期=%.1fs | 预置风扇位: %s（其余靠运行时探测）",
        __version__,
        config.control.mode,
        config.control.interval,
        ", ".join(b.slot for b in config.fans) or "（无）",
    )

    # 记一笔启动 —— 「风扇什么时候被谁改过」以后查这张表
    store.log(
        "lifecycle",
        "startup",
        f"服务启动 | 模式={config.control.mode} | 周期={config.control.interval}s"
        f" | 管控位={','.join(b.slot for b in config.fans)}"
        f" | control.enabled={config.control.enabled}",
    )

    task = asyncio.create_task(controller.run(), name="fan-control-loop")
    try:
        yield
    finally:
        logger.info("开始关闭流程")
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        # 这一步是整个应用最不能跳过的代码
        guard.close("server-shutdown")
        guard.clear_heartbeat()
        store.log("lifecycle", "shutdown", "服务正常关闭，已把风扇交回 BMC 自动控制")
        logger.info("已安全退出")


app = FastAPI(
    title="GPU Fan Console",
    description="pve02 GPU 温度联动风扇控制台（单机应用）",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(api_router, prefix="/api")


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
else:

    @app.get("/", include_in_schema=False)
    async def _frontend_missing() -> dict[str, str]:
        return {
            "message": "前端尚未构建",
            "hint": "进入 frontend/ 执行 npm install && npm run build，"
            "产物会输出到 app/static/",
            "api_docs": "/docs",
        }
