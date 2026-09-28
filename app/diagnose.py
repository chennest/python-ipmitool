"""只读诊断工具 —— 不碰风扇，只报告读数。

**安全设计：本模块永远不会调用 ``apply()``，只读不写。**

用途：

1. 首次部署时验证数据源是否通（DCGM / ipmi_exporter / ipmitool）
2. 排障时确认「读数到底对不对」
3. 调曲线之前预估每个风扇位会拿到什么占空比

用法::

    python -m app.diagnose
    python -m app.diagnose --json      # 给脚本消费
    python -m app.diagnose --config /path/to/config.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from .config import AppConfig, load_config
from .curve import CurveState, build_curve_from_config
from .ipmi import IPMIClient
from .sensors import BoardTemperatureReader, FanMetricsReader, GPUMetricsReader


def collect(config: AppConfig) -> dict[str, Any]:
    """采集一次完整读数（只读）。"""
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
    ipmi = IPMIClient(
        binary=config.sources.ipmitool_binary,
        remote=config.sources.ipmi_remote,
        timeout=config.sources.ipmi_timeout,
        dry_run=True,
    )

    gpus = gpu_reader.read()
    fans = fan_reader.read()
    board_reader = BoardTemperatureReader(
        exporter_endpoint=config.sources.ipmi_exporter_endpoint,
        ipmi_binary=config.sources.ipmitool_binary,
        timeout=config.sources.http_timeout,
    )
    board_temps = board_reader.read()

    gpu_payload = [
        {
            "index": g.index,
            "uuid": g.uuid,
            "short_uuid": g.short_uuid,
            "pci_bus_id": g.pci_bus_id,
            "model": g.model_name,
            "temperature_c": g.temperature,
            "power_watts": g.power_watts,
            "utilization_pct": g.utilization,
            "memory_used_mib": g.memory_used_mib,
            "memory_total_mib": g.memory_total_mib,
            "memory_pct": g.memory_percent,
        }
        for g in gpus
    ]

    all_temps = [g.temperature for g in gpus if g.temperature is not None]
    hottest = max(all_temps) if all_temps else None

    decisions = []
    for binding in config.fans:
        if binding.gpu_uuids:
            temps = [
                g.temperature
                for g in gpus
                if g.uuid in binding.gpu_uuids and g.temperature is not None
            ]
            scope = f"绑定 {len(binding.gpu_uuids)} 张卡"
        else:
            temps = all_temps
            scope = "跟随所有卡（未绑定）"

        if not temps:
            decisions.append(
                {
                    "slot": binding.slot,
                    "scope": scope,
                    "temperature_c": None,
                    "target_duty": None,
                    "note": "无温度读数，实际运行时本轮会跳过",
                }
            )
            continue

        temp = max(temps)
        duty = curve.step(temp, CurveState())
        if binding.max_duty is not None:
            duty = min(duty, binding.max_duty)
        decisions.append(
            {
                "slot": binding.slot,
                "scope": scope,
                "temperature_c": temp,
                "target_duty": duty,
                "note": "",
            }
        )

    return {
        "sources": {
            "gpu": gpu_reader.last_source or "(未取到)",
            "fan": fan_reader.last_source or "(未取到)",
            "dcgm_endpoint": config.sources.dcgm_endpoint,
            "ipmi_exporter_endpoint": config.sources.ipmi_exporter_endpoint,
        },
        "gpus": gpu_payload,
        "fans": [
            {"slot": r.slot, "rpm": r.rpm, "state": r.state, "source": r.source}
            for r in fans.values()
        ],
        "board_temperatures": [
            {"name": t.name, "celsius": t.celsius, "state": t.state}
            for t in board_temps.values()
        ],
        "hottest_gpu_c": hottest,
        "emergency": bool(
            hottest is not None and hottest >= config.safety.emergency_temp
        ),
        "curve": curve.describe(),
        "decisions": decisions,
        "ipmi_would_send": {
            "target_state": ipmi.target_state,
            "note": "dry-run，本工具不会真的下发",
        },
    }


def render(data: dict[str, Any]) -> str:
    """人类可读输出。"""
    lines: list[str] = []
    add = lines.append

    add("=" * 68)
    add("GPU 风扇控制台 · 只读诊断（不会下发任何命令）")
    add("=" * 68)

    src = data["sources"]
    add("")
    add(f"数据源  GPU: {src['gpu']}   ←  {src['dcgm_endpoint']}")
    add(f"        风扇: {src['fan']}   ←  {src['ipmi_exporter_endpoint']}")

    add("")
    add(f"GPU（{len(data['gpus'])} 张）")
    if not data["gpus"]:
        add("  ⚠️  一张都没读到 —— 检查 DCGM exporter 是否在跑")
    for g in data["gpus"]:
        temp = f"{g['temperature_c']}°C" if g["temperature_c"] is not None else "n/a"
        power = f"{g['power_watts']:.1f}W" if g["power_watts"] is not None else "n/a"
        util = (
            f"{g['utilization_pct']}%"
            if g["utilization_pct"] is not None
            else "n/a"
        )
        mem = (
            f"{g['memory_used_mib']:.0f}/{g['memory_total_mib']:.0f} MiB"
            if g["memory_total_mib"]
            else "n/a"
        )
        add(
            f"  [{g['index']}] {g['short_uuid']}  {g['model']}  "
            f"{g['pci_bus_id'].replace('00000000:', '')}"
        )
        add(f"      温度 {temp} | 功耗 {power} | 利用率 {util} | 显存 {mem}")

    add("")
    add(f"风扇（{len(data['fans'])} 个位）")
    if not data["fans"]:
        add("  ⚠️  一个都没读到 —— 检查 ipmi_exporter 是否在跑")
    for f in data["fans"]:
        rpm = f"{f['rpm']:.0f} RPM" if f["rpm"] is not None else "no reading"
        add(f"  {f['slot']:<12} {rpm:>12}   {f['state']}")

    if data.get("board_temperatures"):
        add("")
        add("BMC 板载温度（⚠️ 都不是 GPU 温度 —— BMC 读不到 GPU）")
        for t in sorted(
            data["board_temperatures"],
            key=lambda x: (x["celsius"] is None, -(x["celsius"] or 0)),
        ):
            celsius = f"{t['celsius']:.0f}°C" if t["celsius"] is not None else "n/a"
            add(f"  {t['name']:<18} {celsius:>7}   {t['state']}")

    add("")
    hottest = data["hottest_gpu_c"]
    add(f"最热 GPU: {hottest}°C" if hottest is not None else "最热 GPU: n/a")
    if data["emergency"]:
        add("🚨 已超过紧急阈值 —— 实际运行时会直接拉满 100%")

    add("")
    add("曲线")
    pts = " ".join(f"{p['temp']}°C→{p['duty']}%" for p in data["curve"]["points"])
    add(f"  {pts}")
    add(
        f"  滞回 {data['curve']['hysteresis']}°C | 下限 {data['curve']['min_duty']}%"
        f" | 上限 {data['curve']['max_duty']}%"
    )

    add("")
    add("按当前温度，曲线会给出的目标占空比")
    for d in data["decisions"]:
        if d["target_duty"] is None:
            add(f"  {d['slot']:<12} ——      {d['scope']}｜{d['note']}")
        else:
            add(
                f"  {d['slot']:<12} {d['target_duty']:>3}%    "
                f"（{d['scope']}，{d['temperature_c']}°C）"
            )

    add("")
    add("=" * 68)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="GPU 风扇控制台只读诊断（不写 IPMI）"
    )
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示调试日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    )

    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"❌ 配置加载失败: {exc}", file=sys.stderr)
        return 2

    data = collect(config)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
