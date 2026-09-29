"""Prometheus HTTP API 查询封装。

**背景**：原先风扇转速是直接执行 ``ipmitool sdr type fan`` 再解析文本得到的。
2026-09-28 起改为统一走 Prometheus（数据源是 ipmi_exporter）。

这么改的好处：

1. 读数口径与看板一致，不会出现「控制器一个值、Grafana 另一个值」
2. 不必每轮 spawn 一个 ipmitool 进程，也不用管 BMC 连接
3. ipmitool 的人类可读输出格式在不同机型/版本差异极大（Dell 与 ASRock Rack
   就完全是两套列结构），而 Prometheus 指标格式是标准化的

**代价（必须知道，不然会踩坑）**：

1. 拿到的是**上一次 scrape 的快照**，不是实时值。延迟由 Prometheus 的
   ``scrape_interval`` 决定。所以「设完转速立刻回读验证」这类操作会读到旧值，
   需要等一个 scrape 周期。
2. **Prometheus 不可用时完全读不到数据** —— 本地 in-band 的 ipmitool 反而
   不依赖它。所以调用方必须处理空结果，不能假设一定有数据。
3. 所有被监控机器的 ``ipmi_fan_speed_rpm`` 混在同一个指标里，**必须用
   ``instance`` 标签限定目标机器**，否则会把 A 机器的转速当成 B 机器的。

只用标准库实现，不引入 ``requests`` —— 这个项目当前的依赖只有 PyYAML，
保持这一点。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class PrometheusError(RuntimeError):
    """Prometheus 查询失败（网络不通、返回非法 JSON、查询报错等）。"""


@dataclass(frozen=True)
class PromSample:
    """一条查询结果样本。"""

    labels: dict = field(default_factory=dict)
    value: float = 0.0

    @property
    def name(self) -> str:
        """``name`` 标签，即传感器/风扇位名称。"""
        return self.labels.get("name", "")

    @property
    def instance(self) -> str:
        return self.labels.get("instance", "")


class PrometheusClient:
    """极简 Prometheus HTTP API 客户端（只用到瞬时查询）。"""

    def __init__(self, base_url: str, timeout: float = 10.0):
        """
        Args:
            base_url: Prometheus 地址，如 ``http://192.0.2.20:30091``。
            timeout: 单次查询超时（秒）。**必须有** —— 上游项目就是因为
                不带超时的阻塞式 subprocess 读取而静默挂死过。
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------ 基础查询

    def query(self, promql: str) -> list[PromSample]:
        """执行瞬时查询（``/api/v1/query``）。

        Raises:
            PrometheusError: 网络不通、返回非法 JSON，或查询本身报错。
        """
        params = urllib.parse.urlencode({"query": promql})
        url = f"{self.base_url}/api/v1/query?{params}"
        request = urllib.request.Request(
            url, headers={"User-Agent": "python-ipmitool/1.0"}
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.URLError as exc:
            raise PrometheusError(f"连不上 Prometheus {self.base_url}: {exc}") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise PrometheusError(f"Prometheus 返回的不是合法 JSON: {exc}") from exc

        if payload.get("status") != "success":
            raise PrometheusError(
                f"Prometheus 查询失败: {payload.get('error', payload)}"
            )

        samples: list[PromSample] = []
        for item in payload.get("data", {}).get("result", []):
            try:
                value = float(item["value"][1])
            except (KeyError, IndexError, ValueError, TypeError):
                continue  # 非向量结果或 NaN 之类，跳过
            samples.append(PromSample(labels=item.get("metric", {}), value=value))
        return samples

    # ------------------------------------------------------------ 风扇转速

    def _fan_selector(self, instance: str | None, job: str | None) -> str:
        selectors = []
        if instance:
            selectors.append(f'instance="{instance}"')
        if job:
            selectors.append(f'job="{job}"')
        return "{" + ",".join(selectors) + "}" if selectors else ""

    def get_fan_speeds_by_name(
        self, instance: str | None = None, job: str | None = None
    ) -> dict[str, float]:
        """查询风扇转速，返回 ``{风扇位名: RPM}``。

        Args:
            instance: 限定 Prometheus 的 ``instance`` 标签，如
                ``192.0.2.10:9290``。**多机环境务必传**，否则会把别的
                机器的转速混进来。
            job: 可选，进一步限定抓取任务名。
        """
        promql = "ipmi_fan_speed_rpm" + self._fan_selector(instance, job)
        samples = self.query(promql)
        readings = {s.name: s.value for s in samples if s.name}
        if not readings:
            logger.warning(
                "Prometheus 里查不到风扇转速（查询: %s）—— "
                "确认目标机器的 ipmi_exporter 已接入且 instance 标签填对了",
                promql,
            )
        else:
            logger.debug(
                "从 Prometheus 取到 %d 个风扇位: %s",
                len(readings),
                ", ".join(f"{k}={v:.0f}" for k, v in readings.items()),
            )
        return readings

    def get_fan_speeds(
        self, instance: str | None = None, job: str | None = None
    ) -> list[float]:
        """查询风扇转速，返回 RPM 列表。

        注意：返回的是**列表**，风扇位顺序由 Prometheus 返回顺序决定，
        不保证与机箱物理编号一致。调用方若只关心「最高的那个」（比如
        上游的异常转速判断）则不受影响；若要按位对应，请改用
        :meth:`get_fan_speeds_by_name`。
        """
        return list(self.get_fan_speeds_by_name(instance, job).values())

    # ------------------------------------------------------------ GPU 温度

    def get_gpu_temperatures(
        self, instance: str | None = None, job: str | None = None
    ) -> list[float]:
        """查询 GPU 温度（DCGM），返回 °C 列表。

        ``DCGM_FI_DEV_GPU_TEMP`` 每张卡一条，靠 ``UUID`` 标签区分。这里只返回
        温度值列表，调用方通常取 ``max()`` —— 多卡机器上「最热的那张」才是
        决定风量的那张。

        Args:
            instance: DCGM exporter 的 instance 标签，如 ``192.0.2.10:9400``。
        """
        selectors = []
        if instance:
            selectors.append(f'instance="{instance}"')
        if job:
            selectors.append(f'job="{job}"')
        promql = "DCGM_FI_DEV_GPU_TEMP"
        if selectors:
            promql += "{" + ",".join(selectors) + "}"

        samples = self.query(promql)
        temperatures = [s.value for s in samples]
        if not temperatures:
            logger.warning(
                "Prometheus 里查不到 GPU 温度（查询: %s）—— "
                "确认 DCGM exporter 已接入且 instance 标签填对了",
                promql,
            )
        return temperatures
