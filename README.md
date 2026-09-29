[English Readme](./README_EN.md)（英文版目前只覆盖旧版脚本，尚未同步新控制台）

# python-ipmitool

[![CI](https://github.com/chennest/python-ipmitool/actions/workflows/ci.yml/badge.svg)](https://github.com/chennest/python-ipmitool/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-WebSocket-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Vue 3](https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs&logoColor=white)](https://vuejs.org/)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20Windows-lightgrey)](#)

通过 IPMI 控制服务器风扇。

**Server fan control via IPMI: a web-based console that regulates chassis fans by
GPU temperature (closed-loop curve with hysteresis, FastAPI + Vue3 + SQLite,
DCGM / ipmi_exporter / Prometheus), plus legacy CPU-temperature fan control
scripts for Dell PowerEdge servers.**

本项目包含两部分：

| 组成 | 位置 | 状态 | 说明 |
|---|---|---|---|
| **GPU 风扇控制台**<br>GPU Fan Console | `app/` + `frontend/` | 当前主力 | Web 控制台，按 GPU 温度闭环调速机箱风扇（FastAPI + Vue3） |
| **IPMI 风扇脚本**<br>Fan Control Script | `fancontroller.py` 等 | 遗留可用 | 无界面的命令行脚本，按 CPU 温度调整风扇（Dell 730 等） |

## GPU 风扇控制台（GPU Fan Console）

### 它做什么

目标平台是 **ASRock Rack EPYCD8**（BMC 固件 2.20）：Tesla T10 这类被动散热卡全靠
机箱风扇吹，控制台读取 GPU 温度，按可编辑的分段曲线 + 滞回计算出占空比，
通过 `ipmitool raw 0x3a 0x01` 写给 BMC。

- **数据采集**：GPU 温度走本地 DCGM exporter（`:9400`），风扇转速走 ipmi_exporter
  （`:9290`），直连 exporter 读当下值；历史趋势另走 Prometheus。DCGM 不可用时降级 `nvidia-smi` 兜底
- **控制曲线**：分段折线 + 滞回带（升温立即生效，降温须跌出滞回带才降档，
  避免「直升机效应」）。界面上可编辑曲线并**试算预览**（`/api/curve/preview` 走真实算法）
- **三种模式**：自动调档（按曲线）/ 手动定速（界面指定占空比）/ BMC 自动档（交还 BMC）
- **GPU ↔ 风扇位分配**：每个风扇位绑定一块 GPU，取所绑 GPU 的最高温度作为控速依据；
  未分配的风扇位交回 BMC
- **状态落 SQLite**：模式、曲线、分配关系、审计日志全部存 `app/data/fan-console.db`，
  它是唯一权威数据源；`app/config.yaml` 只在首次运行时当种子
- **安全护栏（三道保险）**：
  1. 服务停止 / 异常 / 收到 SIGTERM 时，`SafetyGuard` 把已接管的风扇位交回 BMC 自动档
  2. 进程内 `atexit` 二道保险
  3. **独立心跳看门狗**（systemd timer，每 2 分钟）：主进程心跳过期则强推 `8×0x00` 回落，
     连 SIGKILL / 断电场景也兜得住
- **概览页**：GPU 卡片（温度 / SM 频率）、风扇转速、温度与转速独立趋势图、审计日志

### 目录结构

```
app/
  main.py          # 入口：单进程 = 控制回路 + API + 静态托管
  api.py           # REST + WebSocket 路由
  controller.py    # 控制回路（15s 一轮）
  curve.py         # 分段曲线 + 滞回
  ipmi.py          # raw 0x3a 0x01 命令族
  sensors.py       # DCGM / ipmi_exporter / nvidia-smi / Prometheus 读取
  safety.py        # 安全护栏
  store.py         # SQLite 持久化
  config.yaml      # 配置（仅首次运行当种子）
  deploy/          # systemd 单元（主服务 + 看门狗）与看门狗脚本
  data/            # fan-console.db（运行时生成，勿覆盖）
  static/          # 前端构建产物（npm run build 自动落到这里）
  tests/           # 单元测试
frontend/          # Vue3 + TS + Vite + Tailwind（Dashboard / Fans / Settings 三页）
deploy.sh          # 一键部署脚本
DEPLOY.md          # 部署手册（踩坑、回滚、验证清单）
```

### 本地运行

```bash
# 1) 构建前端（产物直接输出到 app/static/）
cd frontend && npm install && npm run build && cd ..

# 2) 安装后端依赖
pip install -r app/requirements.txt

# 3) 启动（单进程同时提供页面 + API + 控制回路）
python -m uvicorn app.main:app --host 0.0.0.0 --port 8765
# 打开 http://127.0.0.1:8765
```

前端开发时热更新：

```bash
cd frontend && npm run dev    # vite 起在 :5173，/api 请求代理到 127.0.0.1:8765
```

运行测试：

```bash
python -m unittest discover -s app/tests -t . -v
```

### API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 当前状态（模式、受控位、温度、占空比） |
| GET | `/api/gpus` / `/api/fans` | GPU / 风扇列表 |
| GET/PUT | `/api/curve` | 读写控制曲线 |
| POST | `/api/curve/preview` | 曲线试算预览（走真实算法） |
| GET | `/api/history` | 历史趋势（来自 Prometheus） |
| GET/PUT | `/api/assignments` | GPU ↔ 风扇位分配关系 |
| POST | `/api/mode` | 切换 auto / manual / bmc-auto |
| POST | `/api/manual` | 手动指定占空比 |
| POST | `/api/restore-auto` | 一键交回 BMC 自动档 |
| GET | `/api/audit` | 审计日志 |
| GET | `/api/health` | 健康检查 |
| WS | `/ws` | 实时推送 |

### 部署

生产环境为 Linux + systemd（本地 in-band 读写 `/dev/ipmi0` 需要 root）。
**日常更新一条命令**：

```bash
./deploy.sh                 # 全量：后端 + 前端，自动重启服务并自检
./deploy.sh --static-only   # 只更新前端（不改后端、不重启服务）
```

首次部署、踩坑记录、回滚与验证清单见 **[DEPLOY.md](./DEPLOY.md)**。
最坏情况的兜底：直接向 BMC 发 `ipmitool raw 0x3a 0x01 0x00 0x00 0x00 0x00 0x00 0x00 0x00 0x00`
即可把全部风扇位交回 BMC 自动档（也可从 BMC 独立地址操作，不依赖宿主系统）。

### CI 与发布（GitHub Actions）

每次推送 / PR 自动执行（[.github/workflows/ci.yml](./.github/workflows/ci.yml)）：

1. **后端单测**：`python -m unittest`（Python 3.13，对齐线上）
2. **前端构建**：`npm ci && npm run build`（自带 vue-tsc 全量类型检查）
3. **部署包**：产出 `gpu-fan-console-app.tgz`（成员路径 `app/...`，排除 `app/data`），
   挂在 workflow 的 Artifacts 里——下载后传到 pve02 解压重启即可，本机无需装 Node

推送 main 时额外构建**双平台独立可执行文件**（PyInstaller）；打 `v*` tag 自动创建
GitHub Release 并附上全部产物：

| 产物 | 说明 |
|---|---|
| `gpu-fan-console-windows-x64.zip` / `gpu-fan-console-linux-x64.tar.gz` | 控制台整目录（exe + 前端 + 种子配置） |
| `fancontroller(-once)-windows-x64.exe` / `...-linux-x64` | 旧版脚本单文件可执行 |
| `gpu-fan-console-app.tgz` | 部署包（给已有 systemd 部署用） |

发版就两条命令：

```bash
git tag v1.0.0 && git push origin v1.0.0
```

**可执行文件使用要点**（exe 与源码运行唯一的区别：配置 / 数据都落在 exe 旁边）：

- 控制台：解压后 `gpu-fan-console` 目录里，`config.yaml`（种子配置，可直接改）、
  `data/`（SQLite，首次启动生成）与 exe 同级；仍需系统安装 `ipmitool`，Linux 下读写
  `/dev/ipmi0` 需要 root
- 旧脚本：`fan_settings.yaml` 与 `logs/` 放在 exe 同目录

---

## IPMI 风扇脚本（遗留 / Legacy CLI Scripts）

无界面的命令行版本：监控 CPU 温度，按预定义温度区间调整风扇转速，
Windows / Linux 通用。架构与新控制台一致地「读统一走 Prometheus、写分机型」，
支持多服务器、多线程、按天轮转日志与邮件告警（默认关闭）。

### 兼容服务器

| 品牌 | 型号 | 是否兼容 | type 类型 |
|:---:|:---:|:---:|:---:|
| Dell | 730XD | Y | `dell730` |
| Dell | 730 | Y | `dell730` |
| ASRock Rack | EPYCD8 | Y | `epycd8` |

### 使用方式

> Linux 需先安装 `ipmitool`：debain 系 `apt install -y ipmitool`，
> redhat 系 `yum install -y ipmitool`。Windows 使用仓库自带的 `ipmitool/ipmitool.exe`。

```bash
git clone https://github.com/chennest/python-ipmitool.git
cd python-ipmitool
pip install -r requirements.txt

# 复制并编辑配置（只能填 IP 地址，不能用域名；ip 可填 "local" 表示本机直连）
cp fan_settings.yaml.template fan_settings.yaml
```

`fan_settings.yaml` 核心字段（完整示例见模板文件）：

```yaml
auto: true                  # true 自动控速 / false 手动
interval: 60                # 控制间隔（秒）
log_backup_count: 30        # 日志保留天数
windows_ipmi_tool_path: ".\\ipmitool\\ipmitool.exe"
alert:                      # 邮件告警（可选，默认关闭）
  enabled: false
  fan_speed_threshold: 10000
  max_failed_attempts: 3
  email: { ... }            # SMTP 配置，支持多收件人、1 小时防轰炸
prometheus:
  base_url: "http://192.168.6.31:30091"
servers:
  - type: dell730
    ip: "192.168.71.90"
    user: root
    password: "123123"
    temperature_ranges:     # 温度区间 → 各风扇百分比
      - { min_temp: 0,  max_temp: 60, fan_speeds: [20, 20, 20, 20, 20, 20] }
      - { min_temp: 61, max_temp: 80, fan_speeds: [25, 25, 25, 25, 25, 25] }
```

两种运行模式：

```bash
# 模式一：循环控制（推荐长期后台运行）
python fancontroller.py

# 模式二：单次执行（推荐交给 cron / systemd timer / 任务计划程序调度）
python fancontroller_once.py
# crontab 示例：每 10 分钟一次
# */10 * * * * /usr/bin/python3 /path/to/python-ipmitool/fancontroller_once.py
```

Linux 长期运行建议配置为 systemd 服务（`/etc/systemd/system/fancontroller.service`，
`After=network.target` + `Restart=always`，日志走 `journalctl -u fancontroller -f`）。

> 邮件告警的触发条件：风扇转速超过阈值（默认 10000 RPM）或连续失败达到次数
> （默认 3 次）；同一服务器告警间隔至少 1 小时。Gmail 需使用应用专用密码。
> 各机型控制器的实现细节见 [CLAUDE.md](./CLAUDE.md)。

---

## 关键词

`IPMI` · `ipmitool` · `fan control` · `风扇控制` · `风扇调速` · `GPU 温度` · `GPU fan curve` ·
`server fan` · `服务器风扇` · `BMC` · `raw 0x3a 0x01` · `Dell PowerEdge 730 / 730XD` ·
`ASRock Rack EPYCD8` · `Tesla T10` · `被动散热` · `homelab` · `机房降噪` ·
`DCGM exporter` · `ipmi_exporter` · `node_exporter` · `Prometheus` · `监控` ·
`FastAPI` · `Vue3` · `Vite` · `Tailwind` · `SQLite` · `systemd` · `watchdog 看门狗` ·
`闭环控制` · `PID` · `滞回曲线 hysteresis` · `温度区间`

## 文档索引

- [DEPLOY.md](./DEPLOY.md) —— GPU 风扇控制台部署手册（一键脚本、踩坑、回滚、验证清单）
- [CLAUDE.md](./CLAUDE.md) —— 仓库架构说明（新旧两套的实现细节、加新机型的方法）
- [README_EN.md](./README_EN.md) —— 英文说明（仅旧版脚本）

## 贡献与反馈

欢迎提交 Issue 和 Pull Request 来帮助改进项目。如有任何问题或建议，请通过 GitHub Issues 反馈。

## 许可证

本项目采用 **GPL-3.0** 许可证，详情请参阅 [LICENSE](./LICENSE) 文件。

## 感谢项目

[perryclements/r410-fancontroller: Python fan controller for Dell R410 server (GitHub.com)](https://github.com/perryclements/r410-fancontroller)

[ipmitool/ipmitool: An open-source tool for controlling IPMI-enabled systems (GitHub.com)](https://github.com/ipmitool/ipmitool)
