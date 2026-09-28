"""核心逻辑单元测试（只依赖标准库，可离线跑）。

重点覆盖两类「出错就烧硬件」的逻辑：

1. **8 字节 payload 拼装** —— 少一个字节 BMC 会静默忽略整条命令；
   而「必须全量写」又意味着改一个位时不能把其它位踩成 0x00。
2. **曲线滞回** —— 没有滞回，温度在阈值附近抖动会让风扇转速反复横跳。

跑法（在项目根目录）::

    python -m unittest discover -s app/tests -t . -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.curve import (  # noqa: E402
    CurvePoint,
    CurveState,
    FanCurve,
    build_curve_from_config,
)
from app.ipmi import (  # noqa: E402
    FAN_SLOT_INDEX,
    PAYLOAD_LEN,
    RESERVED_INDEX,
    IPMIClient,
    _fmt_byte,
    encode_duty,
)
from app.sensors import (  # noqa: E402
    FanMetricsReader,
    parse_prometheus,
    parse_sdr_line,
)


class TestDutyEncoding(unittest.TestCase):
    """占空比 ↔ 字节值的编码。"""

    def test_auto_maps_to_zero(self) -> None:
        self.assertEqual(encode_duty(None), 0x00)

    def test_valid_duty_passes_through(self) -> None:
        for duty in (1, 20, 50, 100):
            self.assertEqual(encode_duty(duty), duty)

    def test_zero_is_rejected(self) -> None:
        # 0 被保留用于表示「自动」，不能当手动值下发
        with self.assertRaises(ValueError):
            encode_duty(0)

    def test_over_hundred_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            encode_duty(101)

    def test_non_int_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            encode_duty(50.5)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            encode_duty(True)  # type: ignore[arg-type]

    def test_hex_formatting_matches_spec(self) -> None:
        """字节的十进制值就是百分比 —— 这是最容易写错的一处。"""
        self.assertEqual(_fmt_byte(20), "0x14")
        self.assertEqual(_fmt_byte(30), "0x1e")
        self.assertEqual(_fmt_byte(50), "0x32")
        self.assertEqual(_fmt_byte(100), "0x64")


class TestPayloadAssembly(unittest.TestCase):
    """8 字节 payload 拼装 —— 本文件里最要紧的一组测试。"""

    def setUp(self) -> None:
        self.client = IPMIClient(dry_run=True)

    def test_all_auto_is_eight_zeros(self) -> None:
        payload = self.client.build_payload()
        self.assertEqual(len(payload), PAYLOAD_LEN)
        self.assertEqual(payload, [0] * PAYLOAD_LEN)

    def test_length_is_always_eight(self) -> None:
        payload = self.client.build_payload({"FRNT_FAN1": 60, "REAR_FAN2": 45})
        self.assertEqual(len(payload), PAYLOAD_LEN)

    def test_single_slot_maps_to_correct_index(self) -> None:
        payload = self.client.build_payload({"FRNT_FAN1": 60})
        self.assertEqual(payload[FAN_SLOT_INDEX["FRNT_FAN1"]], 60)
        # b5 之外全是 0
        others = [v for i, v in enumerate(payload) if i != FAN_SLOT_INDEX["FRNT_FAN1"]]
        self.assertEqual(others, [0] * (PAYLOAD_LEN - 1))

    def test_reserved_byte_stays_zero(self) -> None:
        """b2 是保留位，无论怎么设都必须保持 0x00。"""
        payload = self.client.build_payload({"FRNT_FAN1": 80, "REAR_FAN2": 80})
        self.assertEqual(payload[RESERVED_INDEX], 0x00)

    def test_updating_one_slot_keeps_the_other(self) -> None:
        """核心用例：BMC 要求全量写，所以改一个位绝不能踩掉另一个位。"""
        self.client.build_payload({"FRNT_FAN1": 60})
        payload = self.client.build_payload({"REAR_FAN2": 45})

        self.assertEqual(payload[FAN_SLOT_INDEX["FRNT_FAN1"]], 60, "FRNT_FAN1 被踩掉了")
        self.assertEqual(payload[FAN_SLOT_INDEX["REAR_FAN2"]], 45)
        self.assertEqual(payload[FAN_SLOT_INDEX["CPU1_FAN1"]], 0, "未接管的位应保持自动")

    def test_can_release_single_slot_back_to_auto(self) -> None:
        self.client.build_payload({"FRNT_FAN1": 60})
        payload = self.client.build_payload({"FRNT_FAN1": None})
        self.assertEqual(payload[FAN_SLOT_INDEX["FRNT_FAN1"]], 0)

    def test_unknown_slot_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.client.build_payload({"NOT_A_FAN": 50})

    def test_restore_auto_zeroes_everything(self) -> None:
        self.client.build_payload({"FRNT_FAN1": 90, "REAR_FAN2": 90})
        payload = self.client.build_payload(
            {slot: None for slot in FAN_SLOT_INDEX}
        )
        self.assertEqual(payload, [0] * PAYLOAD_LEN)

    def test_dry_run_apply_does_not_call_ipmitool(self) -> None:
        """dry-run 模式下 apply 不该真的调用 ipmitool。"""
        result = self.client.apply({"FRNT_FAN1": 55})
        self.assertTrue(result.ok)
        self.assertIn("0x3a", result.args)
        self.assertIn("0x37", result.args)  # 55 → 0x37


class TestFanCurve(unittest.TestCase):
    """温度-占空比曲线与滞回。"""

    def setUp(self) -> None:
        self.curve = FanCurve(
            [
                CurvePoint(50, 40),
                CurvePoint(60, 50),
                CurvePoint(70, 65),
                CurvePoint(80, 85),
                CurvePoint(90, 100),
            ],
            hysteresis=3.0,
            min_duty=30,
            max_duty=100,
        )

    def test_stateless_lookup(self) -> None:
        """阶梯语义：温度**达到**折点才用那一档，未达到就用低一档（偏保守）。"""
        self.assertEqual(self.curve.duty_at(45), 40)     # 低于最低折点 → 最低档
        self.assertEqual(self.curve.duty_at(50), 40)
        self.assertEqual(self.curve.duty_at(59.9), 40)   # 还差一点到 60
        self.assertEqual(self.curve.duty_at(60), 50)
        self.assertEqual(self.curve.duty_at(75), 65)     # 70 ≤ 75 < 80 → 第 2 档
        self.assertEqual(self.curve.duty_at(80), 85)
        self.assertEqual(self.curve.duty_at(95), 100)

    def test_first_step_sets_index(self) -> None:
        state = CurveState()
        self.assertEqual(self.curve.step(72, state), 65)   # 70 ≤ 72 < 80
        self.assertEqual(state.index, 2)

    def test_upshift_is_immediate(self) -> None:
        """升温必须立即升档 —— 散热是安全方向，不能有任何延迟。"""
        state = CurveState()
        self.curve.step(55, state)          # 第 0 档
        duty = self.curve.step(80, state)   # 冲到 80 → 立即到第 3 档
        self.assertEqual(duty, 85)
        self.assertEqual(state.index, 3)

    def test_downshift_requires_leaving_hysteresis_band(self) -> None:
        """降温方向：跌出滞回带前不许降档。"""
        state = CurveState()
        self.curve.step(80, state)          # 第 3 档（折点 80°C / 85%）
        self.assertEqual(state.index, 3)

        # 78°C 还没跌出 80-3=77 的滞回带 → 保持第 3 档
        self.assertEqual(self.curve.step(78, state), 85)
        self.assertEqual(state.index, 3)

        # 76°C 已跌出 → 允许降到第 2 档（70°C / 65%）
        self.assertEqual(self.curve.step(76, state), 65)
        self.assertEqual(state.index, 2)

    def test_no_flapping_around_threshold(self) -> None:
        """阈值附近抖动不应导致转速横跳（"直升机效应"回归测试）。"""
        state = CurveState()
        self.curve.step(70.0, state)        # 定在第 2 档
        baseline = state.index

        for temp in (69.9, 70.1, 69.5, 70.4, 69.8, 70.2):
            self.curve.step(temp, state)

        self.assertEqual(state.index, baseline, "温度微抖导致档位漂移")

    def test_min_duty_clamp(self) -> None:
        curve = FanCurve([CurvePoint(50, 10)], min_duty=30)
        self.assertEqual(curve.duty_at(30), 30)

    def test_max_duty_clamp(self) -> None:
        curve = FanCurve([CurvePoint(50, 100)], max_duty=80)
        self.assertEqual(curve.duty_at(99), 80)

    def test_points_are_sorted(self) -> None:
        curve = FanCurve([CurvePoint(90, 100), CurvePoint(50, 40)])
        self.assertEqual([p.temp for p in curve.points], [50, 90])

    def test_duplicate_temp_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FanCurve([CurvePoint(50, 40), CurvePoint(50, 60)])

    def test_empty_points_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FanCurve([])

    def test_state_reset(self) -> None:
        state = CurveState(index=4)
        state.reset()
        self.assertIsNone(state.index)

    def test_below_first_point_uses_first_duty(self) -> None:
        """语义锁定：低于最低折点时给的是**首档**，不是 min_duty。

        这条很容易被误读成「低温 → 下限」，界面上必须写清楚。
        """
        curve = FanCurve([CurvePoint(50, 70)], min_duty=30)
        self.assertEqual(curve.duty_at(40), 70, "低温应给首档 70%，不是下限 30%")

    def test_min_duty_above_max_is_rejected(self) -> None:
        """上下限倒置会让钳制退化成常量（永远给下限），必须挡住。"""
        with self.assertRaises(ValueError):
            FanCurve([CurvePoint(50, 60)], min_duty=80, max_duty=40)

    def test_duty_must_be_integer_percent(self) -> None:
        """70.5 不能静默砍成 70 —— 要么报错，要么按 70.5 处理，不能装没看见。"""
        with self.assertRaises(ValueError):
            build_curve_from_config([{"temp": 50, "duty": 70.5}])

    def test_temp_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CurvePoint(500, 50)

    def test_hysteresis_has_upper_bound(self) -> None:
        """滞回带 50°C 等于「降温永不降档」，风扇会一直顶着高档转。"""
        with self.assertRaises(ValueError):
            FanCurve([CurvePoint(50, 60)], hysteresis=50)

    def test_sample_follows_step_curve(self) -> None:
        curve = FanCurve([CurvePoint(50, 40), CurvePoint(80, 85)], min_duty=30)
        samples = {round(s["temp"]): s["duty"] for s in curve.sample(40, 80, 10)}
        self.assertEqual(samples[40], 40)   # 低于首折点
        self.assertEqual(samples[50], 40)   # 达到 50 → 首档
        self.assertEqual(samples[70], 40)   # 还没到 80
        self.assertEqual(samples[80], 85)   # 达到 80 → 升档

    def test_sample_rejects_bad_range(self) -> None:
        curve = FanCurve([CurvePoint(50, 40)])
        with self.assertRaises(ValueError):
            curve.sample(80, 40, 1)
        with self.assertRaises(ValueError):
            curve.sample(40, 80, 0)


class TestCurvePreview(unittest.TestCase):
    """「改着看」的曲线试算（preview_curve）—— 只读，不能污染运行状态。"""

    def _controller(self):
        return _make_controller()

    def test_preview_matches_what_runtime_would_emit(self) -> None:
        """预览出来的值 = 保存后真正会下发的值（同一套构造 + 钳制）。"""
        c = self._controller()
        payload = {
            "points": [{"temp": 50, "duty": 40}, {"temp": 80, "duty": 85}],
            "hysteresis": 3.0,
            "min_duty": 30,
            "max_duty": 100,
            "from": 40,
            "to": 80,
            "step": 10,
        }
        preview = {
            round(s["temp"]): s["duty"] for s in c.preview_curve(payload)["samples"]
        }

        # 同一条曲线真正落进去，逐点比对
        c.apply_settings({"curve": payload})
        for temp, duty in preview.items():
            self.assertEqual(
                c._curve.duty_at(float(temp)), duty, f"{temp}°C 预览与实跑不一致"
            )

    def test_preview_does_not_touch_runtime_curve(self) -> None:
        """预览是「改着看」—— 没点保存就不能改到正在跑的曲线。"""
        c = self._controller()
        before = c._curve.describe()
        c.preview_curve(
            {"points": [{"temp": 30, "duty": 10}], "from": 30, "to": 60, "step": 10}
        )
        self.assertEqual(c._curve.describe(), before)

    def test_preview_rejects_bad_draft(self) -> None:
        """草稿非法就在试算阶段撞出来，别等点保存才 400。"""
        c = self._controller()
        for bad in (
            {"points": []},                                        # 空曲线
            {"points": [{"temp": 50, "duty": 40}, {"temp": 50, "duty": 60}]},  # 温度重复
            {"points": [{"temp": 50, "duty": 0}]},                 # 占空比越界
            {"points": [{"temp": 50, "duty": 40}], "min_duty": 90, "max_duty": 50},
        ):
            with self.assertRaises(ValueError):
                c.preview_curve(bad)

    def test_emergency_resume_must_be_below_trigger(self) -> None:
        """解除 ≥ 触发会让紧急状态一进就出，风扇 100% ↔ 曲线档反复横跳。"""
        c = self._controller()
        before = (c._emergency_temp, c._emergency_resume)
        with self.assertRaises(ValueError):
            c.apply_settings(
                {"safety.emergency_temp": 70.0, "safety.emergency_resume_temp": 80.0}
            )
        # 校验失败不能留下改了一半的状态
        self.assertEqual((c._emergency_temp, c._emergency_resume), before)

    def test_emergency_thresholds_accept_valid_pair(self) -> None:
        c = self._controller()
        c.apply_settings(
            {"safety.emergency_temp": 88.0, "safety.emergency_resume_temp": 78.0}
        )
        self.assertEqual(c._emergency_temp, 88.0)
        self.assertEqual(c._emergency_resume, 78.0)


class TestPrometheusParsing(unittest.TestCase):
    """Prometheus 文本解析 —— 用 pve02 上抓到的真实格式。"""

    SAMPLE = """\
# HELP DCGM_FI_DEV_GPU_TEMP GPU temperature (in C).
# TYPE DCGM_FI_DEV_GPU_TEMP gauge
DCGM_FI_DEV_GPU_TEMP{gpu="0",UUID="GPU-e49ed30f-f0f4-dc17-0225-2c1235602b39",pci_bus_id="00000000:01:00.0",device="nvidia0",modelName="Tesla T10"} 45
DCGM_FI_DEV_GPU_TEMP{gpu="1",UUID="GPU-e60e8f23-b150-6718-8508-3cb00e0d9fc6",pci_bus_id="00000000:82:00.0",device="nvidia1",modelName="Tesla T10"} 52
DCGM_FI_DEV_POWER_USAGE{gpu="0",UUID="GPU-e49ed30f-f0f4-dc17-0225-2c1235602b39"} 43.017
# HELP ipmi_fan_speed_rpm Fan speed in rotations per minute.
ipmi_fan_speed_rpm{id="24",name="FRNT_FAN1"} 3000
ipmi_fan_speed_rpm{id="29",name="REAR_FAN2"} 3000
"""

    def test_parses_all_samples(self) -> None:
        samples = parse_prometheus(self.SAMPLE)
        self.assertEqual(len(samples), 5)

    def test_ignores_comments_and_help(self) -> None:
        samples = parse_prometheus(self.SAMPLE)
        self.assertFalse([s for s in samples if s.name.startswith("#")])

    def test_extracts_labels(self) -> None:
        samples = parse_prometheus(self.SAMPLE)
        temps = [s for s in samples if s.name == "DCGM_FI_DEV_GPU_TEMP"]
        self.assertEqual(len(temps), 2)
        self.assertEqual(temps[0].labels["gpu"], "0")
        self.assertEqual(temps[0].labels["modelName"], "Tesla T10")
        self.assertAlmostEqual(temps[0].value, 45.0)

    def test_uuid_label_present(self) -> None:
        """UUID 标签是「按卡绑定」的基础，必须解析出来。"""
        samples = parse_prometheus(self.SAMPLE)
        uuids = {
            s.labels["UUID"]
            for s in samples
            if s.name == "DCGM_FI_DEV_GPU_TEMP"
        }
        self.assertEqual(
            uuids,
            {
                "GPU-e49ed30f-f0f4-dc17-0225-2c1235602b39",
                "GPU-e60e8f23-b150-6718-8508-3cb00e0d9fc6",
            },
        )

    def test_ignores_unparsable_lines(self) -> None:
        self.assertEqual(parse_prometheus("garbage line without value"), [])
        self.assertEqual(parse_prometheus(""), [])


class TestGpuDcgmReader(unittest.TestCase):
    """DCGM 字段映射 → ``GPUMetric``（含新增的 SM 时钟 = 概览卡片的「GPU 频率」）。"""

    SAMPLE = """\
DCGM_FI_DEV_GPU_TEMP{gpu="0",UUID="GPU-e49ed30f-f0f4-dc17-0225-2c1235602b39",pci_bus_id="00000000:01:00.0",modelName="Tesla T10"} 45
DCGM_FI_DEV_POWER_USAGE{gpu="0",UUID="GPU-e49ed30f-f0f4-dc17-0225-2c1235602b39"} 43.017
DCGM_FI_DEV_SM_CLOCK{gpu="0",UUID="GPU-e49ed30f-f0f4-dc17-0225-2c1235602b39"} 1380
DCGM_FI_DEV_FB_USED{gpu="0",UUID="GPU-e49ed30f-f0f4-dc17-0225-2c1235602b39"} 1234
DCGM_FI_DEV_FB_FREE{gpu="0",UUID="GPU-e49ed30f-f0f4-dc17-0225-2c1235602b39"} 15150
"""

    def _read(self, text: str):
        from app.sensors import GPUMetricsReader

        reader = GPUMetricsReader()
        with mock.patch("app.sensors._fetch_text", return_value=text):
            return reader._read_dcgm()

    def test_maps_sm_clock(self) -> None:
        """SM 时钟必须映射到 ``clock_mhz``（概览卡片展示的就是它）。"""
        metrics = self._read(self.SAMPLE)
        self.assertEqual(len(metrics), 1)
        m = metrics[0]
        self.assertEqual(m.temperature, 45.0)
        self.assertEqual(m.clock_mhz, 1380.0)
        self.assertEqual(m.memory_total_mib, 16384.0, "FB_USED + FB_FREE = 显存总量")

    def test_missing_clock_stays_none(self) -> None:
        """老版 exporter 没暴露 SM_CLOCK 时不炸，字段为 None（前端显示占位符）。"""
        lines = [
            line for line in self.SAMPLE.splitlines() if "SM_CLOCK" not in line
        ]
        metrics = self._read("\n".join(lines))
        self.assertEqual(len(metrics), 1)
        self.assertIsNone(metrics[0].clock_mhz)


class TestSdrFanParsing(unittest.TestCase):
    """``ipmitool sdr type fan`` 输出解析。

    样例取自 2026-09-28 在 pve02 上的真实输出 —— 这组用例的由来就是一个
    真实 bug：最初以为「第二列是读数」，结果把传感器 ID ``62h`` 当成了 RPM。
    """

    SAMPLE_OK = "FRNT_FAN1        | 62h | ok  |  7.0 | 3000 RPM"
    SAMPLE_NS = "FRNT_FAN2        | 63h | ns  |  7.0 | No Reading"
    SAMPLE_CPU = "CPU1_FAN1        | 60h | ok  |  7.0 | 1300 RPM"

    def test_reading_comes_from_last_column(self) -> None:
        """核心断言：读数是最后一列，不是第二列的传感器 ID。"""
        parsed = parse_sdr_line(self.SAMPLE_OK)
        self.assertIsNotNone(parsed)
        slot, reading, state = parsed
        self.assertEqual(slot, "FRNT_FAN1")
        self.assertEqual(reading, "3000 RPM")
        self.assertEqual(state, "ok")

    def test_extracts_rpm_value(self) -> None:
        import re

        _, reading, _ = parse_sdr_line(self.SAMPLE_CPU)
        match = re.search(r"(\d+)\s*RPM", reading)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "1300")

    def test_no_reading_row_is_parsed_without_rpm(self) -> None:
        parsed = parse_sdr_line(self.SAMPLE_NS)
        self.assertIsNotNone(parsed)
        slot, reading, state = parsed
        self.assertEqual(slot, "FRNT_FAN2")
        self.assertEqual(reading, "No Reading")
        self.assertEqual(state, "ns")

    def test_never_mistakes_sensor_id_for_reading(self) -> None:
        """回归断言：``62h`` 这种传感器 ID 绝不能被当成读数。"""
        _, reading, _ = parse_sdr_line(self.SAMPLE_OK)
        self.assertNotIn("h", reading.lower().replace("reading", ""))

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(parse_sdr_line(""))
        self.assertIsNone(parse_sdr_line("no pipes here"))
        self.assertIsNone(parse_sdr_line("only | one"))


class TestSdrTemperatureParsing(unittest.TestCase):
    """``ipmitool sdr type Temperature`` 解析（EPYCD8 真实输出）。

    与风扇共用同一套五列结构，所以复用 :func:`parse_sdr_line`。
    """

    SAMPLE = "Card Side Temp   | 32h | ok  |  3.0 | 47 degrees C"
    SAMPLE_NS = "TR1 Temp         | 33h | ns  |  3.0 | No Reading"

    def test_parses_temperature_row(self) -> None:
        parsed = parse_sdr_line(self.SAMPLE)
        self.assertIsNotNone(parsed)
        name, reading, state = parsed
        self.assertEqual(name, "Card Side Temp")
        self.assertEqual(reading, "47 degrees C")
        self.assertEqual(state, "ok")

    def test_no_reading_temperature(self) -> None:
        name, reading, state = parse_sdr_line(self.SAMPLE_NS)
        self.assertEqual(name, "TR1 Temp")
        self.assertEqual(reading, "No Reading")
        self.assertEqual(state, "ns")


class TestFanReaderIpmitoolFallback(unittest.TestCase):
    """ipmitool 兜底路径 —— 必须跳过未接的风扇位。

    样例是 2026-09-28 在 pve02 上抓的真实输出：14 个风扇传感器位里只有
    4 个有读数，其余全是 ``No Reading``。不跳过的话界面上会凭空多出
    10 个空风扇位。
    """

    SDR_OUTPUT = """\
CPU1_FAN1        | 60h | ok  |  7.0 | 1300 RPM
FRNT_FAN1        | 62h | ok  |  7.0 | 3000 RPM
FRNT_FAN2        | 63h | ns  |  7.0 | No Reading
FRNT_FAN3        | 64h | ns  |  7.0 | No Reading
REAR_FAN1        | 66h | ok  |  7.0 | 400 RPM
REAR_FAN2        | 67h | ok  |  7.0 | 3000 RPM
CPU1_FAN1_2      | 68h | ns  |  7.0 | No Reading
FRNT_FAN1_2      | 6Ah | ns  |  7.0 | No Reading
"""

    def _read_with_fake_ipmitool(self) -> dict:
        reader = FanMetricsReader(exporter_endpoint=None)
        fake = mock.Mock(returncode=0, stdout=self.SDR_OUTPUT, stderr="")
        with mock.patch("app.sensors.subprocess.run", return_value=fake):
            return reader._read_ipmitool()

    def test_only_live_fans_are_returned(self) -> None:
        readings = self._read_with_fake_ipmitool()
        self.assertEqual(
            set(readings), {"CPU1_FAN1", "FRNT_FAN1", "REAR_FAN1", "REAR_FAN2"}
        )

    def test_no_reading_slots_are_skipped(self) -> None:
        readings = self._read_with_fake_ipmitool()
        for slot in ("FRNT_FAN2", "FRNT_FAN3", "CPU1_FAN1_2", "FRNT_FAN1_2"):
            self.assertNotIn(slot, readings, f"{slot} 是未接位，不该出现在结果里")

    def test_rpm_values_are_correct(self) -> None:
        readings = self._read_with_fake_ipmitool()
        self.assertEqual(readings["FRNT_FAN1"].rpm, 3000.0)
        self.assertEqual(readings["REAR_FAN2"].rpm, 3000.0)
        self.assertEqual(readings["REAR_FAN1"].rpm, 400.0)


def _make_controller():
    """构造一个最小可用的控制器（mock 掉 IPMI / 传感器）。

    抽成模块级函数 —— 曲线试算那组测试也要用，不复制一遍。
    """
    from app.config import AppConfig, FanBinding
    from app.controller import FanController
    from app.curve import build_curve_from_config
    from app.ipmi import IPMIClient
    from app.safety import SafetyGuard
    from app.sensors import FanMetricsReader, GPUMetricsReader

    # 配置里显式预置两个位（模拟 config.yaml 占位；探测路径另有专项测试）
    config = AppConfig(fans=[FanBinding(slot="FRNT_FAN1"), FanBinding(slot="REAR_FAN2")])
    ipmi = mock.MagicMock(spec=IPMIClient)
    guard = mock.MagicMock(spec=SafetyGuard)
    guard.engaged = False
    gpu_reader = mock.MagicMock(spec=GPUMetricsReader)
    fan_reader = mock.MagicMock(spec=FanMetricsReader)
    # last_source 是实例属性（reader.read() 时才赋值），spec 的 Mock 上没有
    gpu_reader.last_source = "test"
    fan_reader.last_source = "test"
    curve = build_curve_from_config(
        [
            {"temp": 45, "duty": 40},
            {"temp": 75, "duty": 80},
        ]
    )
    return FanController(config, ipmi, guard, gpu_reader, fan_reader, curve)


class TestSourceAssignments(unittest.TestCase):
    """「散热源 → 风扇位」分配模型（2026-09-28 倒置：GPU 是主体）。

    这组测试防的是两类事故：
    1. 校验漏洞 —— 两个源抢同一个风扇位 / 分配了不存在的位（数据模型被写脏）；
    2. 控制越界 —— 没被分配的风扇位被程序动了（「未分配 = 交回 BMC」的承诺）。
    """

    GPU_A = "GPU-e60e8f23-b150-6718-8508-3cb00e0d9fc6"
    GPU_B = "GPU-e49ed30f-f0f4-dc17-0225-2c1235602b39"

    def _controller(self):
        return _make_controller()

    @staticmethod
    def _gpu(uuid: str, temp: float):
        """构造一个最小可用的 GPUMetric。"""
        from app.sensors import GPUMetric

        return GPUMetric(
            uuid=uuid,
            index=0,
            pci_bus_id="00000000:01:00.0",
            model_name="Tesla T10",
            temperature=temp,
        )

    # ------------------------------------------------------------ 校验

    def test_conflicting_slot_is_rejected(self) -> None:
        """一个风扇位只能给一个源 —— 冲突必须整体拒绝。"""
        c = self._controller()
        with self.assertRaises(ValueError):
            c.update_assignments(
                [
                    {"key": f"gpu:{self.GPU_A}", "kind": "gpu", "gpu_uuid": self.GPU_A, "slots": ["FRNT_FAN1"]},
                    {"key": "gpu:all", "kind": "gpu_group", "slots": ["FRNT_FAN1", "REAR_FAN2"]},
                ]
            )
        # 整体拒绝：不能留下改了一半的状态
        self.assertFalse(c.describe()["bindings_configured"])

    def test_unknown_slot_is_rejected(self) -> None:
        c = self._controller()
        with self.assertRaises(ValueError):
            c.update_assignments(
                [{"key": f"gpu:{self.GPU_A}", "kind": "gpu", "gpu_uuid": self.GPU_A, "slots": ["NOPE"]}]
            )

    def test_gpu_kind_requires_uuid(self) -> None:
        c = self._controller()
        with self.assertRaises(ValueError):
            c.update_assignments([{"key": "gpu:bad", "kind": "gpu", "slots": ["FRNT_FAN1"]}])

    def test_valid_assignment_builds_owner_index(self) -> None:
        c = self._controller()
        c.update_assignments(
            [
                {"key": f"gpu:{self.GPU_A}", "kind": "gpu", "gpu_uuid": self.GPU_A, "slots": ["FRNT_FAN1"]},
                {"key": f"gpu:{self.GPU_B}", "kind": "gpu", "gpu_uuid": self.GPU_B, "slots": ["REAR_FAN2"]},
            ]
        )
        self.assertTrue(c.describe()["bindings_configured"])
        # describe() 里每张卡都能看到自己的风扇位
        rows = {a["key"]: a["slots"] for a in c.describe()["assignments"]}
        self.assertEqual(rows[f"gpu:{self.GPU_A}"], ["FRNT_FAN1"])
        self.assertEqual(rows[f"gpu:{self.GPU_B}"], ["REAR_FAN2"])

    # ------------------------------------------------------------ 控制行为

    def _apply(self, c, gpus):
        """跑一轮曲线，返回 IPMI 收到的 updates 字典。"""
        with mock.patch.object(type(c), "_warn_orphan_gpus", lambda self, g: None):
            c._apply_curve(gpus)
        if c._ipmi.apply.called:
            return c._ipmi.apply.call_args[0][0]
        return {}

    def test_only_assigned_slots_are_driven(self) -> None:
        """控制越界防护：GPU_A 分了 FRNT_FAN1，GPU_B 什么都没分 ——
        再热也只能告警，REAR_FAN2 一根线不能碰。"""
        c = self._controller()
        c.update_assignments(
            [{"key": f"gpu:{self.GPU_A}", "kind": "gpu", "gpu_uuid": self.GPU_A, "slots": ["FRNT_FAN1"]}]
        )
        updates = self._apply(
            c,
            [self._gpu(self.GPU_A, 70.0), self._gpu(self.GPU_B, 88.0)],
        )
        self.assertIn("FRNT_FAN1", updates)
        self.assertNotIn("REAR_FAN2", updates, "未分配的风扇位绝不能被程序写入")

    def test_each_slot_follows_its_own_gpu(self) -> None:
        """两张卡各自驱动自己的风扇位 —— 60°C 的卡不能拉着 80°C 卡的风扇降速。"""
        c = self._controller()
        c.update_assignments(
            [
                {"key": f"gpu:{self.GPU_A}", "kind": "gpu", "gpu_uuid": self.GPU_A, "slots": ["FRNT_FAN1"]},
                {"key": f"gpu:{self.GPU_B}", "kind": "gpu", "gpu_uuid": self.GPU_B, "slots": ["REAR_FAN2"]},
            ]
        )
        updates = self._apply(
            c,
            [self._gpu(self.GPU_A, 60.0), self._gpu(self.GPU_B, 80.0)],
        )
        # A 卡 60°C 在第一档（40%）；B 卡 80°C 已过 75 折点（80%）
        self.assertEqual(updates["FRNT_FAN1"], 40)
        self.assertEqual(updates["REAR_FAN2"], 80)

    def test_offline_gpu_releases_its_slots(self) -> None:
        """绑定的卡掉卡（读不到温度）→ 它的风扇位交回 BMC 自动（不狂转）。"""
        c = self._controller()
        c.update_assignments(
            [{"key": f"gpu:{self.GPU_B}", "kind": "gpu", "gpu_uuid": self.GPU_B, "slots": ["REAR_FAN2"]}]
        )
        updates = self._apply(c, [self._gpu(self.GPU_A, 55.0)])  # B 不在上报里
        self.assertIsNone(updates.get("REAR_FAN2"))

    def test_gpu_group_kind_is_rejected(self) -> None:
        """「所有 GPU 最热」合成源已删除（超哥不理解 = 坏选项）—— 传了要拒。"""
        c = self._controller()
        with self.assertRaises(ValueError):
            c.update_assignments(
                [{"key": "gpu:all", "kind": "gpu_group", "slots": ["FRNT_FAN1"]}]
            )

    def test_all_slots_listed_even_without_rpm(self) -> None:
        """可分配清单 = **全部可控位**（含没有转速读数的），一个都不能少。"""
        c = self._controller()
        from app.ipmi import FAN_SLOT_INDEX
        from app.sensors import FanReading

        # 实测只有 3 个位在转，其余 4 个没接（No Reading）—— 也必须列出
        for slot, rpm in (("FRNT_FAN1", 3000.0), ("REAR_FAN2", 3000.0), ("CPU1_FAN1", 1300.0)):
            c._snapshot.fans[slot] = FanReading(slot=slot, rpm=rpm)

        fan_slots = c.describe()["fan_slots"]
        self.assertEqual(
            [f["slot"] for f in fan_slots], sorted(FAN_SLOT_INDEX)
        )
        rpm_of = {f["slot"]: f["rpm"] for f in fan_slots}
        self.assertEqual(rpm_of["FRNT_FAN1"], 3000.0)
        self.assertIsNone(rpm_of["FRNT_FAN3"], "没读数的位也要列出（rpm=null）")

        # 全部 7 个位都可以正常分配
        c.update_assignments(
            [{"key": f"gpu:{self.GPU_A}", "kind": "gpu", "gpu_uuid": self.GPU_A, "slots": ["FRNT_FAN3"]}]
        )
        rows = {a["key"]: a["slots"] for a in c.describe()["assignments"]}
        self.assertEqual(rows[f"gpu:{self.GPU_A}"], ["FRNT_FAN3"])

    def test_assignment_to_unmanaged_gpu_is_rejected(self) -> None:
        """打通「设置 ↔ 分配」：未纳入管控的卡不能配风扇（明确 400，不留暗状态）。"""
        c = self._controller()
        c.apply_settings({"control.managed_gpus": [self.GPU_A]})
        with self.assertRaises(ValueError):
            c.update_assignments(
                [{"key": f"gpu:{self.GPU_B}", "kind": "gpu", "gpu_uuid": self.GPU_B, "slots": ["REAR_FAN2"]}]
            )

    def test_unmanage_clears_assignments(self) -> None:
        """设置页取消勾选 → 该卡的分配立即停用并清除（不驱动、不残留）。"""
        c = self._controller()
        c.update_assignments(
            [
                {"key": f"gpu:{self.GPU_A}", "kind": "gpu", "gpu_uuid": self.GPU_A, "slots": ["FRNT_FAN1"]},
                {"key": f"gpu:{self.GPU_B}", "kind": "gpu", "gpu_uuid": self.GPU_B, "slots": ["REAR_FAN2"]},
            ]
        )
        # 移出 GPU_B
        c.apply_settings({"control.managed_gpus": [self.GPU_A]})
        rows = {a["key"]: a for a in c.export_assignments()}
        self.assertEqual(rows[f"gpu:{self.GPU_B}"]["slots"], [], "被移出管控的卡分配应清空")
        # FRNT_FAN1 归 GPU_A，REAR_FAN2 已无主
        self.assertEqual(c._slot_owner.get("FRNT_FAN1"), f"gpu:{self.GPU_A}")
        self.assertNotIn("REAR_FAN2", c._slot_owner)
        # 再给 GPU_B 分配 → 拒绝
        with self.assertRaises(ValueError):
            c.update_assignments(
                [{"key": f"gpu:{self.GPU_B}", "kind": "gpu", "gpu_uuid": self.GPU_B, "slots": ["REAR_FAN2"]}]
            )

    def test_startup_sanitizes_stale_assignments(self) -> None:
        """启动自愈：库里的脏数据（未管控的卡带着分配）只清冲突项，不作废整份。"""
        c = self._controller()
        c.apply_settings({"control.managed_gpus": [self.GPU_A]})
        stale = [
            {"key": f"gpu:{self.GPU_A}", "kind": "gpu", "gpu_uuid": self.GPU_A, "slots": ["FRNT_FAN1"]},
            # 脏数据：B 未管控却带着分配（模拟上次持久化失败残留）
            {"key": f"gpu:{self.GPU_B}", "kind": "gpu", "gpu_uuid": self.GPU_B, "slots": ["REAR_FAN2"]},
        ]
        cleaned = c.sanitize_stored_assignments(stale)
        self.assertEqual(cleaned[1]["slots"], [], "冲突项应被清空")
        # 自愈后整体恢复成功，合法项不受牵连
        c.update_assignments(cleaned)
        rows = {a["key"]: a["slots"] for a in c.export_assignments()}
        self.assertEqual(rows[f"gpu:{self.GPU_A}"], ["FRNT_FAN1"])
        self.assertEqual(rows[f"gpu:{self.GPU_B}"], [])

    def test_fallback_ignores_unmanaged_gpus(self) -> None:
        """兜底模式只跟「受管控」的卡 —— 移出管控的卡不该驱动兜底风扇位。"""
        c = self._controller()
        # 只管 GPU_A；GPU_B 高温但已被移出管控
        c.apply_settings({"control.managed_gpus": [self.GPU_A]})
        updates = self._apply(
            c, [self._gpu(self.GPU_A, 50.0), self._gpu(self.GPU_B, 90.0)]
        )
        # 兜底温度应取 A（50°C → 第一档 40%），而不是 B 的 90°C（会拉满）
        self.assertEqual(updates["FRNT_FAN1"], 40)
        self.assertEqual(updates["REAR_FAN2"], 40)

    def test_cpu_key_must_be_canonical(self) -> None:
        """kind=cpu 但 key 不是 'cpu' → 拒绝（防幽灵源）。"""
        c = self._controller()
        with self.assertRaises(ValueError):
            c.update_assignments([{"key": "foo", "kind": "cpu", "slots": ["FRNT_FAN1"]}])

    def test_cpu_source_drives_tctl(self) -> None:
        c = self._controller()
        c.update_assignments([{"key": "cpu", "kind": "cpu", "slots": ["FRNT_FAN1"]}])
        # CPU 温度在 snapshot.cpu_temps 里（模拟 node_exporter 读数）
        from app.sensors import CPUCoreTemperature

        c._snapshot.cpu_temps = [CPUCoreTemperature(label="Tctl", celsius=65.0)]
        updates = self._apply(c, [])
        self.assertEqual(updates["FRNT_FAN1"], 40)  # 65°C 低于 75 折点 → 第一档


if __name__ == "__main__":
    unittest.main(verbosity=2)
