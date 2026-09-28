"""REST + WebSocket 路由。

控制器实例挂在 ``app.state`` 上，路由通过 ``request.app.state`` 取用 ——
不引入全局单例，方便将来写测试时替换成假的控制器。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Literal

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

from .controller import FanController

logger = logging.getLogger(__name__)

router = APIRouter()

#: WebSocket 推送间隔（秒）。比控制周期快，前端看起来才像"实时"。
WS_PUSH_INTERVAL = 2.0


# ------------------------------------------------------------------ 请求模型


class ModeRequest(BaseModel):
    mode: Literal["auto", "manual"] = Field(description="auto=曲线自动；manual=手动固定")


class ManualDutyRequest(BaseModel):
    slot: str = Field(description="风扇位名，如 FRNT_FAN1")
    duty: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="占空比百分比。传 null 表示把该位交回 BMC 自动控制。",
    )


class AssignmentItem(BaseModel):
    """一个散热源的风扇分配。**主体是源（GPU），它去挑风扇接口。**"""

    key: str = Field(description="源的唯一键：gpu:<uuid> 或 cpu")
    kind: Literal["gpu", "cpu"] = Field(
        default="gpu",
        description="gpu = 某张具体的卡；cpu = CPU 核温度 Tctl",
    )
    gpu_uuid: str | None = Field(
        default=None, description="kind=gpu 时的 GPU UUID（绝不用 index）"
    )
    slots: list[str] = Field(
        default_factory=list, description="分配给该源的风扇位（可多选）"
    )


class AssignmentsRequest(BaseModel):
    assignments: list[AssignmentItem]


# ------------------------------------------------------------------ 工具


def _controller(request: Request) -> FanController:
    controller = getattr(request.app.state, "controller", None)
    if controller is None:  # pragma: no cover - 正常启动流程不会走到
        raise HTTPException(status_code=503, detail="控制器尚未初始化")
    return controller


def _query_range(
    base_url: str, promql: str, start: float, end: float, step: float
) -> list[dict[str, Any]]:
    """执行一次 Prometheus ``query_range``（阻塞调用，外层丢线程池）。

    只返回 ``[{"metric": {...}, "points": [[ms, value], ...]}]``，
    时间戳统一换算成**毫秒** —— ECharts 的 time 轴要毫秒。
    """
    params = urllib.parse.urlencode(
        {
            "query": promql,
            "start": f"{start:.3f}",
            "end": f"{end:.3f}",
            "step": f"{int(step)}",
        }
    )
    url = f"{base_url.rstrip('/')}/api/v1/query_range?{params}"

    with urllib.request.urlopen(url, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))

    if payload.get("status") != "success":
        raise RuntimeError(str(payload.get("error", "query_range 返回失败")))

    series: list[dict[str, Any]] = []
    for item in payload.get("data", {}).get("result", []):
        points = [
            [int(float(ts) * 1000), float(val)]
            for ts, val in item.get("values", [])
        ]
        if points:
            series.append({"metric": item.get("metric", {}), "points": points})
    return series


# ------------------------------------------------------------------ 查询


@router.get("/health", summary="健康检查")
async def health(request: Request) -> dict[str, Any]:
    controller = _controller(request)
    snap = controller.snapshot()
    return {
        "status": "ok",
        "control_loop_running": snap.running,
        "mode": snap.mode,
        "emergency": snap.emergency,
        "consecutive_failures": snap.consecutive_failures,
    }


@router.get("/status", summary="完整状态快照")
async def status(request: Request) -> dict[str, Any]:
    return _controller(request).describe()


@router.get("/gpus", summary="GPU 指标")
async def gpus(request: Request) -> list[dict[str, Any]]:
    return _controller(request).describe()["gpus"]


@router.get("/fans", summary="风扇位状态")
async def fans(request: Request) -> list[dict[str, Any]]:
    return _controller(request).describe()["fans"]


@router.get("/curve", summary="当前控制曲线")
async def curve(request: Request) -> dict[str, Any]:
    return _controller(request).describe()["curve"]


class SettingsPatch(BaseModel):
    """运行时设置的部分更新。

    只传要改的键即可（**部分更新语义**）。
    """

    control_enabled: bool | None = Field(
        default=None, description="控制总开关。false = 完全不调档，风扇保持现状"
    )
    control_interval: float | None = Field(
        default=None, gt=0, le=3600, description="控制周期（秒）"
    )
    curve: dict[str, Any] | None = Field(
        default=None,
        description="整条曲线 {points:[{temp,duty}], hysteresis, min_duty, max_duty}",
    )
    emergency_temp: float | None = Field(
        default=None, description="紧急散热触发温度（°C）"
    )
    emergency_resume_temp: float | None = Field(
        default=None, description="紧急散热解除温度（°C）"
    )
    managed_gpus: list[str] | None = Field(
        default=None,
        description=(
            "管控的 GPU UUID 列表（只控制这几张卡）。"
            "空数组 = 全部管控（保守默认，新插的卡自动纳入）"
        ),
    )


@router.get("/settings", summary="读取运行时设置")
async def get_settings(request: Request) -> dict[str, Any]:
    """当前生效的设置。

    这些值来自 SQLite（配置文件只提供初始值）—— 也就是**界面上改过的就是权威值**。
    """
    return _controller(request).export_settings()


@router.patch("/settings", summary="修改运行时设置")
async def patch_settings(
    payload: SettingsPatch, request: Request
) -> dict[str, Any]:
    """部分更新：只传要改的键。

    改完立即生效（控制周期会在下一轮循环读到，曲线立即用于下一轮计算），
    **不需要重启服务**，同时落库持久化。
    """
    controller = _controller(request)
    store = getattr(request.app.state, "store", None)

    # 把 API 的扁平字段映射回内部的点号键
    patch: dict[str, Any] = {}
    if payload.control_enabled is not None:
        patch["control.enabled"] = payload.control_enabled
    if payload.control_interval is not None:
        patch["control.interval"] = payload.control_interval
    if payload.curve is not None:
        patch["curve"] = payload.curve
    if payload.emergency_temp is not None:
        patch["safety.emergency_temp"] = payload.emergency_temp
    if payload.emergency_resume_temp is not None:
        patch["safety.emergency_resume_temp"] = payload.emergency_resume_temp
    if payload.managed_gpus is not None:
        patch["control.managed_gpus"] = payload.managed_gpus

    if not patch:
        raise HTTPException(status_code=400, detail="没有提供任何要修改的设置")

    try:
        controller.apply_settings(patch)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 管控范围变化会连带清除被移出卡的分配（controller 内处理），
    # 这里把分配的最新状态一并落库，保证「设置 ↔ 分配」持久层也打通
    if payload.managed_gpus is not None and store is not None:
        try:
            store.save_assignments(controller.export_assignments())
        except Exception:  # noqa: BLE001
            logger.exception("分配联动清除已生效，但持久化失败")

    if store is not None:
        try:
            store.save_settings(patch)
            store.log(
                "api_call",
                "api",
                "修改设置: "
                + ", ".join(f"{k}={v!r}" for k, v in sorted(patch.items())),
            )
        except Exception:  # noqa: BLE001 - 持久化失败不该让设置回滚
            logger.exception("设置已生效，但持久化失败（重启后会回到旧值）")

    return {"ok": True, "settings": controller.export_settings()}


@router.get("/history", summary="历史趋势（代理 Prometheus query_range）")
async def history(
    request: Request,
    minutes: int = Query(default=30, ge=5, le=1440, description="回溯时长（分钟）"),
) -> dict[str, Any]:
    """历史曲线。

    **为什么由后端代理而不是前端直连 Prometheus**：避免跨域，也别把
    Prometheus 地址（以及它背后的整个监控栈）暴露到浏览器里。

    为什么历史要单独走 Prometheus：控制器只持有「此刻」的快照，历史时序
    是 Prometheus 的主场。注意 Prometheus 是 30s 一次 scrape，所以曲线在
    短时间内是阶梯状的 —— 这是数据源特性，不是画错了。

    未配置 ``sources.prometheus_url`` 时返回空序列（前端会提示），
    不算错误 —— 历史图是可选的。
    """
    config = request.app.state.config
    src = config.sources

    if not src.prometheus_url:
        return {
            "minutes": minutes,
            "series": [],
            "note": "未配置 sources.prometheus_url，历史趋势不可用（实时数据不受影响）",
        }

    end = time.time()
    start = end - minutes * 60
    # step 不小于 30s（= Prometheus 的 scrape_interval），
    # 再小只会在区间里拿到重复样本
    step = max(30.0, minutes * 60 / 400)

    targets: list[tuple[str, str, str, str]] = []
    if src.prometheus_gpu_instance:
        targets.append(
            (
                f'DCGM_FI_DEV_GPU_TEMP{{instance="{src.prometheus_gpu_instance}"}}',
                "gpu_temp",
                "°C",
                "left",
            )
        )
    if src.prometheus_fan_instance:
        targets.append(
            (
                f'ipmi_fan_speed_rpm{{instance="{src.prometheus_fan_instance}"}}',
                "fan_rpm",
                "RPM",
                "right",
            )
        )
    # CPU 温度 = **CPU 核温度 Tctl**，走 node_exporter —— 与「CPU 温度统一走
    # node_exporter」的决定一致。
    # ⚠️ 不用 BMC 的 `ipmi_temperature_celsius{name="CPU Temp"}` —— 那是主板
    # 传感器读数，语义上是「CPU 插槽附近的环境温度」，不是 CPU 核温度。
    # k10temp 在 hwmon 里的 chip 名是 PCI 路径形式（pci0000:00_0000:00:18_3），
    # 所以必须靠 node_hwmon_sensor_label 做语义过滤，别硬编码 chip 名。
    if src.prometheus_node_instance:
        targets.append(
            (
                "node_hwmon_temp_celsius * on(chip, sensor) group_left(label) "
                'node_hwmon_sensor_label{label="Tctl", '
                f'instance="{src.prometheus_node_instance}"}}',
                "cpu_temp",
                "°C",
                "left",
            )
        )

    series: list[dict[str, Any]] = []
    for promql, kind, unit, axis in targets:
        try:
            result = await asyncio.to_thread(
                _query_range, src.prometheus_url, promql, start, end, step
            )
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            logger.warning("查询历史失败（%s）: %s", kind, exc)
            continue

        for item in result:
            metric = item["metric"]
            if kind == "gpu_temp":
                uuid = str(metric.get("UUID", ""))
                label = f"GPU{metric.get('gpu', '?')} {uuid[4:12] or '?'}"
            elif kind == "cpu_temp":
                label = "CPU 温度"
            else:
                label = str(metric.get("name", "?"))
            series.append(
                {
                    "key": f"{kind}_{label}",
                    "label": label,
                    "unit": unit,
                    "axis": axis,
                    "points": item["points"],
                }
            )

    return {"minutes": minutes, "series": series}


# ------------------------------------------------------------------ 调控


@router.post("/mode", summary="切换控制模式")
async def set_mode(payload: ModeRequest, request: Request) -> dict[str, Any]:
    controller = _controller(request)
    try:
        controller.set_mode(payload.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "mode": controller.mode}


@router.post("/manual", summary="手动设定某个风扇位的占空比")
async def set_manual(payload: ManualDutyRequest, request: Request) -> dict[str, Any]:
    controller = _controller(request)
    try:
        controller.set_manual_duty(payload.slot, payload.duty)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "slot": payload.slot, "duty": payload.duty}


@router.post("/restore-auto", summary="立即把全部风扇位交回 BMC 自动控制")
async def restore_auto(request: Request) -> dict[str, Any]:
    """"我要松手了" 按钮。

    这是**安全方向**的操作，任何时候都允许调用，不受当前模式限制 ——
    用户想紧急交还控制权的时候不该被任何状态检查拦住。
    """
    ipmi = getattr(request.app.state, "ipmi", None)
    if ipmi is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="IPMI 客户端尚未初始化")

    result = await asyncio.to_thread(ipmi.restore_auto)
    if result is None:
        raise HTTPException(status_code=500, detail="回退失败，请手动检查风扇！")

    store = getattr(request.app.state, "store", None)
    if store is not None:
        store.log(
            "ipmi_write",
            "restore_auto",
            f"手动触发「交回 BMC」 | {result.summary()}",
            ok=result.ok,
        )
    return {"ok": result.ok, "output": result.stdout.strip(), "rc": result.returncode}


@router.put("/assignments", summary="更新散热源与风扇位的分配")
async def update_assignments(payload: AssignmentsRequest, request: Request) -> dict[str, Any]:
    """设置「哪个源用哪些风扇」—— **GPU 是主体，它挑自己的风扇接口**。

    - ``kind=gpu`` 必须带 ``gpu_uuid``（**绝不用 index** —— 这台机器的两张卡
      换过一次 PCI 槽位，index 和 pci_bus_id 都变过，只有 UUID/SN 稳定）
    - 一个风扇位只能被一个源占用（冲突返回 400）
    - ``slots`` 传空数组 = 该源不占用任何风扇（未分配的位程序一根线不碰）
    - ``gpu:all`` 是「所有 GPU 最热」的合成源，用于还没摸清物理对应关系时
      的安全配置
    """
    controller = _controller(request)
    store = getattr(request.app.state, "store", None)
    items = [a.model_dump() for a in payload.assignments]

    try:
        controller.update_assignments(items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if store is not None:
        try:
            # 存控制器的**当前完整状态**，而不是请求体 —— 请求体可能只带了一部分。
            store.save_assignments(controller.export_assignments())
            store.log(
                "api_call",
                "api",
                "更新分配: "
                + "; ".join(
                    f"{a['key']}→{a['slots'] or '(无)'}" for a in items
                ),
            )
        except Exception:  # noqa: BLE001 - 持久化失败不该让分配改动回滚
            logger.exception("分配已生效，但持久化失败（重启后会回到旧值）")

    return {"ok": True, "assignments": controller.describe()["assignments"]}


@router.get("/audit", summary="操作审计")
async def audit(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000, description="返回条数"),
) -> dict[str, Any]:
    """最近的 IPMI 写入与 API 调用记录。

    **这张表存在的意义**：2026-09-28 两个 GPU 风扇位从 3000 RPM 掉回 BMC
    自动档，uvicorn 日志被重启覆盖、IPMI raw 命令又不进 BMC SEL，**两边都查不出
    是谁发的**。有了审计表，这类问题直接查库即可。
    """
    store = getattr(request.app.state, "store", None)
    if store is None:  # pragma: no cover
        return {"records": []}
    return {"records": store.recent_audit(limit)}


# ------------------------------------------------------------------ 实时推送


@router.websocket("/ws")
async def websocket_status(websocket: WebSocket) -> None:
    """按固定间隔推送状态快照。前端断了就静默收工，不刷日志。"""
    await websocket.accept()
    controller: FanController | None = getattr(
        websocket.app.state, "controller", None
    )
    if controller is None:  # pragma: no cover
        await websocket.close(code=1011)
        return

    try:
        while True:
            await websocket.send_json(controller.describe())
            await asyncio.sleep(WS_PUSH_INTERVAL)
    except WebSocketDisconnect:
        logger.debug("前端 WebSocket 断开")
    except Exception:  # pragma: no cover - 网络抖动不该刷满日志
        logger.debug("WebSocket 推送异常，连接关闭", exc_info=True)
