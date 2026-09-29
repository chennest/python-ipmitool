# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

这是一个跨平台的 IPMI 风扇控制器,用于监控服务器 CPU 温度并根据预定义的温度区间自动调整风扇转速。支持 Windows 和 Linux 平台。

### 两种运行模式

1. **循环控制模式** (`fancontroller.py`): 程序内部循环监控,适合长期后台运行
2. **单次执行模式** (`fancontroller_once.py`): 执行一次后退出,适合外部调度工具（cron、systemd timer）定时调用

## 核心架构

### 三层控制器架构
- **fancontroller.py**: 循环模式入口,负责配置加载、日志初始化和多线程管理
- **fancontroller_once.py**: 单次执行模式入口,执行一次后退出
- **base_controller.py**: `IPMIFanController` 基类,定义通用的 IPMI 命令执行逻辑
  - `adjust_fans_once()`: 核心方法,执行一次温度检测和风扇调整
  - `process_server_loop()`: 循环模式,持续调用 `adjust_fans_once()`
  - `process_server_once()`: 单次模式,调用一次 `adjust_fans_once()` 后退出
- **dell730_controller.py**: `Dell730FanController` 子类,实现 Dell 730 系列服务器的具体控制逻辑
  - `_initialize_dell730()`: Dell 730 特定初始化（禁用 PCIe 散热响应、设置手动模式）
  - `start_fan_control()`: 启动循环模式
  - `run_once()`: 执行单次模式

### 多线程模型
- 每个服务器实例在独立线程中运行
- 线程命名格式: `Thread-{server_ip}`
- 主线程等待所有子线程完成 (`thread.join()`)

### 配置系统
- 使用 YAML 格式配置: `fan_settings.yaml` (从 `fan_settings.yaml.template` 复制)
- 支持多服务器配置,每个服务器可定义多个温度区间和对应风扇转速
- IP 地址可设置为 `"local"` 以在本地直接执行命令(无需远程 IPMI)
- **邮件告警** (可选，默认关闭): 风扇调节连续失败时自动发送告警邮件
  - 可配置转速阈值和失败次数
  - 支持多个收件人
  - 防止邮件轰炸（最小间隔 1 小时）

### 日志系统
- 使用 `TimedRotatingFileHandler` 按天轮转日志
- 日志保留天数由配置文件中的 `log_backup_count` 控制
- 同时输出到文件 (`logs/fancontroller.log`) 和控制台
- 日志格式: `时间戳 - 线程名 - 消息内容`

### 温度监控逻辑
- 使用 `max(cpu_temps)` 作为判断依据(最热 CPU 温度)
- 温度范围匹配: `min_temp <= avg_temp < max_temp`
- 优化策略: 温度未跨越区间且风扇未异常飙升时跳过设置
- 风扇异常检测: `max_speed >= 15000` (接近 `max_fan_rotational_speed = 16000`)

## 常用开发命令

### 安装依赖
```bash
pip install -r requirements.txt
```

### 配置文件准备
```bash
# 复制模板文件
cp fan_settings.yaml.template fan_settings.yaml

# 编辑配置文件,设置服务器 IP、用户名、密码和温度区间
# 注意: 只能使用 IP 地址,不能使用域名
```

### 运行程序

**循环控制模式（程序内部循环）**
```bash
# 前台运行
python fancontroller.py

# 后台运行（Windows）
start /b python fancontroller.py

# 后台运行（Linux）
nohup python3 fancontroller.py &
```

**单次执行模式（外部调度）**
```bash
# 直接执行一次
python fancontroller_once.py

# cron 定时执行（每 10 分钟）
*/10 * * * * /usr/bin/python3 /path/to/fancontroller_once.py

# Windows 任务计划程序
schtasks /create /tn "IPMI Fan Controller" /tr "python C:\path\to\fancontroller_once.py" /sc minute /mo 10
```

**作为 systemd 服务运行 (Linux 推荐)**
```bash
# 创建服务文件
sudo nano /etc/systemd/system/fancontroller.service

# 重载配置并启动服务
sudo systemctl daemon-reload
sudo systemctl start fancontroller.service
sudo systemctl status fancontroller.service
sudo systemctl enable fancontroller.service

# 查看服务日志
journalctl -u fancontroller.service -f
```

### 查看日志
```bash
# 实时查看日志
tail -f logs/fancontroller.log

# 查看历史日志
ls logs/
cat logs/fancontroller.log.YYYY-MM-DD
```

## 添加新服务器型号支持

> **架构原则（2026-09-28 改造后）：「读」统一，「写」分机型。**
> 风扇转速与温度都由基类从 Prometheus 取（数据源是 ipmi_exporter / DCGM），
> 子类只负责实现该机型**特有的「写」命令**。

1. 在 `fanController/` 目录下创建新的控制器类文件（如 `epycd8_controller.py`）
2. 继承 `IPMIFanController` 基类，实现该机型特有的写操作：
   - `set_fan_speed(fan_index, percentage)`: 设置风扇转速的 IPMI raw 命令
   - `_get_base_command()`: 生成基础命令前缀（`ip: "local"` 时返回空串）
   - `set_ipmi_manual_mode()` / `set_ipmi_auto_mode()`: 手动/自动模式切换
     （部分机型如 EPYCD8 没有独立的「切手动」命令，写占空比本身就是手动）
   - `_build_temperature_query()` (**可选**): 换温度源时覆盖它。
     默认查 **CPU 核心温度**（node_exporter 的 hwmon，按语义标签 `Tctl` 过滤）；
     EPYCD8 覆盖此方法去查 **GPU 温度**（DCGM），因为它的机箱风扇是给 GPU 散热的
   - `start_fan_control()` / `run_once()`: 入口方法
3. **不需要实现 `get_fan_rotational_speed()` 或 `get_cpu_temperature()`**
   —— 两者都已由基类统一从 Prometheus 获取（原先各机型各写一份 sdr 解析，
   列结构还互不相同）
4. 在 `fancontroller.py` 与 `fancontroller_once.py` 的 `CONTROLLER_TYPES`
   字典里登记一行：`'新机型名': 新控制器类`

## 重要注意事项

### IPMI 命令执行
- Windows 平台使用配置文件中的 `windows_ipmi_tool_path` 指定的 ipmitool.exe
- Linux 平台直接使用系统的 `ipmitool` 命令(需预先安装)
- 本地模式 (`ip: "local"`): 命令格式无需 `-I lanplus -H ...` 前缀

### Dell 730 特定细节

**初始化命令 (必须)**
- 禁用"第三方 PCIe 卡的散热响应策略": `raw 0x30 0xce 0x00 0x16 0x05 0x00 0x00 0x00 0x05 0x00 0x01 0x00 0x00`
- 成功返回: `16 05 00 00 00`
- 此命令需要在设置手动模式前执行,否则风扇控制可能不生效

**IPMI 命令**
- CPU 温度传感器识别: `"0Eh"` 或 `"0Fh"` 标识符
- 手动模式 IPMI 命令: `raw 0x30 0x30 0x01 0x00`
- 自动模式 IPMI 命令: `raw 0x30 0x30 0x01 0x01`
- 设置风扇转速: `raw 0x30 0x30 0x02 0x{fan_index:02x} 0x{percentage:02x}`

### Prometheus 数据源（2026-09-28 改造）

**「读」全部统一走 Prometheus**，不再由各机型子类执行 `ipmitool sdr`：

```yaml
prometheus:
  base_url: "http://192.168.6.31:30091"
  timeout: 10

servers:
  - type: epycd8
    prometheus:                          # per-server 覆盖（多机场景必需）
      fan_instance:  "192.168.6.7:9290"  # 风扇转速 ← ipmi_exporter
      temp_instance: "192.168.6.7:9100"  # CPU 温度  ← node_exporter
      gpu_instance:  "192.168.6.7:9400"  # GPU 温度  ← DCGM exporter
```

| 数据 | 指标 | instance（pve02 实测） |
|------|------|----------------------|
| 风扇转速 | `ipmi_fan_speed_rpm{name="FRNT_FAN1"}` | `192.168.6.7:9290` |
| CPU 温度 | `node_hwmon_temp_celsius`（按语义标签 `label="Tctl"` 过滤） | `192.168.6.7:9100` |
| GPU 温度 | `DCGM_FI_DEV_GPU_TEMP` | `192.168.6.7:9400` |

⚠️ **三个 instance 对应三个不同的 exporter，混用会直接查不到数据。** 配置项
分别为 `fan_instance` / `temp_instance` / `gpu_instance`（笼统的 `instance` 仍
作兜底）。

⚠️ **拿到的是上一次 scrape 的快照，不是实时值。** pve 各 target 的
`scrape_interval` 实测为 **30s**，也就是温度最多滞后 30 秒 —— 对控速决策是明显
滞后，**建议把这些 target 的抓取间隔调小**（node_exporter 采集很轻，10s 毫无压力）。

⚠️ Prometheus 不可用时读不到任何数据（本地 in-band 的 ipmitool 反而不依赖它）。
调用方已做空值保护，但排查时先查 Prometheus 连通性。

> 补充：CPU 温度走 `node_exporter --collector.hwmon`，它读的就是内核 hwmon
> （`/sys/class/hwmon/`），与直接读 sysfs 是同一份数据。注意 hwmon 里 k10temp
> 的 chip 名是 PCI 路径形式（`pci0000:00_0000:00:18_3`）而非可读的 `k10temp`，
> 所以查询用 `node_hwmon_sensor_label{label="Tctl"}` 做语义过滤，别硬编码 chip 名。

### 依赖项
- **PyYAML**: 用于解析 YAML 配置文件
- 其他功能仅依赖 Python 标准库（Prometheus 查询用 `urllib` 实现，无需 `requests`）

## 兼容的服务器型号

| 品牌 | 型号 | Type 配置值 |
|------|------|------------|
| Dell | 730XD | `dell730` |
| Dell | 730 | `dell730` |
| ASRock Rack | EPYCD8 | `epycd8` |

---

## 新控制台（`app/` + `frontend/`）与部署

上面的内容是**上游遗留**的 `fancontroller.py` 那套（Dell 730 为主，CPU 温度控速）。

本仓库真正在用的是 `app/`（FastAPI 控制台）+ `frontend/`（Vue3 前端）：

- 控速依据是 **GPU 温度**（DCGM），目标机 pve02 的机箱风扇，写 `ipmitool raw 0x3a 0x01`
- 运行时状态（模式 / 管控 GPU / 分配关系 / 审计）**全部落 SQLite**，配置文件只是首次运行的种子

**实时数据源：三个外部 exporter（自备组件，不随仓库提供，装法不限）**

| 数据 | exporter | 项目地址 | 指标 | 默认端口 |
|---|---|---|---|---|
| GPU 温度/功率/频率 | dcgm-exporter | <https://github.com/NVIDIA/dcgm-exporter> | `DCGM_FI_DEV_GPU_TEMP` 等 | 9400 |
| 风扇转速 | ipmi_exporter | <https://github.com/prometheus-community/ipmi_exporter> | `ipmi_fan_speed_rpm` | 9290 |
| CPU 温度 | node_exporter | <https://github.com/prometheus/prometheus/tree/master/node_exporter> | `node_hwmon_temp_celsius`（`label="Tctl"`；启动必须带 `--collector.hwmon`） | 9100 |

端点在 `app/config.yaml` 的 `sources.*_endpoint`（实时读数）与 `prometheus_*_instance`
（仅 `/api/history` 用）可配；DCGM 不可用时 `sensors.py` 降级 `nvidia-smi` 兜底。
ipmi_exporter in-band 读 `/dev/ipmi0` 需要 root。安装/自检详见 [`DEPLOY.md`](./DEPLOY.md) 2.1 节。

- 部署方式、踩坑、回滚、验证清单 → **见 [`DEPLOY.md`](./DEPLOY.md)**，一键脚本 `./deploy.sh`

