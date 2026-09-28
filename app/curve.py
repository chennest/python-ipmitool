"""温度-占空比控制曲线（带滞回）。

**为什么不用 PID？**

这个场景的被控对象是「机箱风扇 + 整条风道」，热惯性很大；而可用的调节手段
只有 1%~100% 的整数占空比，分辨率相当粗。PWM 的粒度（1%）远大于温度噪声
（±1°C），PID 在「粗粒度 + 大滞后」的组合下很容易震荡，调参成本还高。

分段曲线 + 滞回足够稳，而且有个更实际的好处：**一眼能看懂，随时能改**。
出问题时你不需要去猜三个增益参数在干什么。

**滞回的必要性**

温度在阈值附近抖动（比如 69.8 ↔ 70.2°C）时，没有滞回会让占空比在
40% ↔ 60% 之间来回切换，风扇转速忽大忽小——就是俗称的「直升机效应」，
既吵又伤风扇。所以：

- **升温方向立即生效**（散热是安全方向，不能延迟）
- **降温方向必须跌出滞回带才降档**
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CurvePoint:
    """曲线上的一个折点：温度达到 ``temp`` 时用 ``duty``。"""

    temp: float
    duty: int

    def __post_init__(self) -> None:
        if not 1 <= self.duty <= 100:
            raise ValueError(f"占空比需在 1~100 之间，收到 {self.duty}")


@dataclass
class CurveState:
    """曲线的运行状态。

    由调用方持有而非曲线自身持有 —— 曲线保持无状态，方便测试，
    也方便 API 层随时用不同温度试算而不污染运行状态。
    """

    index: int | None = None

    def reset(self) -> None:
        self.index = None


class FanCurve:
    """分段温度-占空比曲线，带滞回防抖。

    **阶梯语义**：温度必须**达到**某个折点才用那一档。例如折点
    ``[(70, 65), (80, 85)]`` 下，79°C 给的是 65% 而不是 85%。

    这是刻意选的保守约定。想要更平滑就多插几个折点，而不是改成线性插值 ——
    阶梯的行为可预测，排障时拿计算器一算就知道该给多少风。
    """

    def __init__(
        self,
        points: Sequence[CurvePoint],
        hysteresis: float = 3.0,
        min_duty: int = 20,
        max_duty: int = 100,
    ) -> None:
        if not points:
            raise ValueError("曲线至少需要一个折点")

        ordered = sorted(points, key=lambda p: p.temp)
        for prev, curr in zip(ordered, ordered[1:]):
            if prev.temp == curr.temp:
                raise ValueError(f"曲线折点温度重复: {curr.temp}°C")

        self._points: tuple[CurvePoint, ...] = tuple(ordered)
        self.hysteresis = max(0.0, hysteresis)
        self.min_duty = min_duty
        self.max_duty = max_duty

    # ------------------------------------------------------------ 属性

    @property
    def points(self) -> tuple[CurvePoint, ...]:
        return self._points

    # ------------------------------------------------------------ 计算

    def _index_for(self, temp: float) -> int:
        """温度 → 折点下标。低于最低折点时返回 0（即最低档）。"""
        index = 0
        for i, point in enumerate(self._points):
            if temp >= point.temp:
                index = i
            else:
                break
        return index

    def step(self, temp: float, state: CurveState) -> int:
        """推进一次曲线，返回目标占空比（已按上下限钳制）。

        Args:
            temp: 当前温度（°C），多卡场景下传最大值。
            state: 可变状态，记录当前档位以实现滞回。
        """
        target = self._index_for(temp)
        current = state.index

        if current is None:
            state.index = target
            logger.debug("曲线首次定档: %.1f°C → 第 %d 档 %d%%", temp, target, self._points[target].duty)
        elif target > current:
            # 升温：立即升档，不做延迟
            logger.debug(
                "升温升档: %.1f°C → 第 %d 档 %d%%",
                temp, target, self._points[target].duty,
            )
            state.index = target
        elif target < current:
            # 降温：必须跌出滞回带才降档
            release_temp = self._points[current].temp - self.hysteresis
            if temp <= release_temp:
                logger.debug(
                    "降温降档: %.1f°C ≤ %.1f°C → 第 %d 档 %d%%",
                    temp, release_temp, target, self._points[target].duty,
                )
                state.index = target
            else:
                logger.debug(
                    "降温但未跌出滞回带: %.1f°C > %.1f°C，保持第 %d 档",
                    temp, release_temp, current,
                )

        duty = self._points[state.index].duty
        return max(self.min_duty, min(self.max_duty, duty))

    def duty_at(self, temp: float) -> int:
        """无状态试算：这个温度理论上该给多少占空比。

        给 API 层画曲线预览用，不影响运行状态。
        """
        duty = self._points[self._index_for(temp)].duty
        return max(self.min_duty, min(self.max_duty, duty))

    def describe(self) -> dict:
        """序列化给前端展示。"""
        return {
            "points": [{"temp": p.temp, "duty": p.duty} for p in self._points],
            "hysteresis": self.hysteresis,
            "min_duty": self.min_duty,
            "max_duty": self.max_duty,
        }


def build_curve_from_config(raw_points: Iterable[dict], **kwargs) -> FanCurve:
    """从配置里的一串 ``{"temp": .., "duty": ..}`` 构造曲线。"""
    points = [CurvePoint(float(p["temp"]), int(p["duty"])) for p in raw_points]
    return FanCurve(points, **kwargs)
