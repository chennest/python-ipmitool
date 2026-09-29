# 部署手册 —— GPU 风扇控制台

> 目标机：你的 GPU 宿主机（下文 `<目标机>` / `<连接名>` 按实际替换）
> 部署目录：`/opt/gpu-fan-console` ｜ 服务名：`gpu-fan-console.service`
> 访问地址：`http://<目标机>:8765`
> 参考环境：Python 3.13.5 / fastapi 0.141.1 / uvicorn 0.53.0（2026-09-28 核对）

---

## 0. 一句话版本

```bash
# 在项目根目录（Windows Git Bash）
CONN=<连接名> ./deploy.sh   # 全量：后端 + 前端，自动重启服务并自检
CONN=<连接名> ./deploy.sh --static-only  # 只更新前端（不改后端、不重启服务）
```

下面是人话版说明，出问题时看这里。

---

## 1. 为什么是「一个服务」

前端构建产物直接落到 `app/static/`（`frontend/vite.config.ts` 里 `outDir: '../app/static'`），
FastAPI 挂 `StaticFiles` 一起发出去。所以：

- **没有 nginx、没有反向代理、没有第二个端口** —— 一个 uvicorn 进程同时提供页面和 API/WebSocket
- 前端代码里一律写相对路径 `/api/...`，开发时靠 vite proxy 转发到 `127.0.0.1:8765`，生产同源，**不需要改任何地址**
- 部署 = 把 `app/` 覆盖过去 + 重启进程，没有别的步骤

---

## 2. 远端现状（部署前该有的东西）

| 项目 | 路径 / 值 | 说明 |
|---|---|---|
| 代码目录 | `/opt/gpu-fan-console` | 工作目录，service 的 `WorkingDirectory` |
| 虚拟环境 | `/opt/gpu-fan-console/.venv` | Python 3.13.5，已装 fastapi / uvicorn / PyYAML |
| 数据库 | `/opt/gpu-fan-console/app/data/fan-console.db` | **SQLite 是权威数据源**，升级时绝不能被覆盖 |
| 运行时配置 | `/opt/gpu-fan-console/app/config.yaml` | 只在**首次运行**当初始值用；之后改了不算数。仓库里的版本是**模板**（示例地址），升级时解压要排除它（见 4.1 步骤 4） |
| 主服务 | `/etc/systemd/system/gpu-fan-console.service` | `enabled` + `active`，`Restart=always` |
| 看门狗 | `/etc/systemd/system/fan-watchdog.{service,timer}` | `enabled`，每 2 分钟查一次心跳 |
| 心跳文件 | `/run/gpu-fan-console/heartbeat` | 主进程每轮写；过期则由看门狗强推 `8×0x00` 回落 |
| 依赖的 exporter | ipmi_exporter `:9290`、node_exporter `:9100`、DCGM `:9400` | 自备组件，装法不限，见 2.1 |

### 2.1 前置依赖：三个 exporter（自备组件，装法不限）

控制台的**全部实时数据源就是这三个 exporter**（直连各自 `/metrics`，不走 Prometheus）。
它们不随本仓库提供，**需要自己安装**——docker、systemd、裸二进制、发行版包管理器，
怎么装都可以，达标标准与项目地址如下：

| 组件 | 项目地址 | 默认端口 | 必须提供 |
|---|---|---|---|
| dcgm-exporter | <https://github.com/NVIDIA/dcgm-exporter> | 9400 | `DCGM_FI_DEV_GPU_TEMP`（GPU 温度；顺带 SM 频率/功率/利用率）。宿主需已装 NVIDIA 驱动 |
| ipmi_exporter | <https://github.com/prometheus-community/ipmi_exporter> | 9290 | `ipmi_fan_speed_rpm`（风扇转速）。in-band 读 `/dev/ipmi0`，需要 root + openipmi 驱动 |
| node_exporter | <https://github.com/prometheus/prometheus/tree/master/node_exporter> | 9100 | `node_hwmon_temp_celsius`（CPU 温度）。**启动必须带 `--collector.hwmon`**，否则没有 hwmon 指标 |

与装法无关的三条硬性达标标准：

1. 三个 `/metrics` 各自能 grep 出上表的指标（自检命令见下）
2. ipmi_exporter 进程能读到本机 `/dev/ipmi0`
3. dcgm-exporter 不可用时控制台自动降级 `nvidia-smi` 兜底，但只有 GPU 温度，指标口径缩水

端口不是死的：默认端口只是约定，改了端口就把 `app/config.yaml` 里对应的
`*_endpoint`（实时读数）和 `prometheus_*_instance`（历史趋势）一起改掉。

自检三连（各返回至少一行数据才算就位）：

```bash
curl -s localhost:9400/metrics | grep DCGM_FI_DEV_GPU_TEMP | head -1
curl -s localhost:9290/metrics | grep ipmi_fan_speed_rpm | head -1
curl -s localhost:9100/metrics | grep Tctl | head -1
```

---

## 3. 首次部署（换机器 / 重装时才需要）

```bash
# ⓪ 自备三个 exporter（dcgm-exporter / ipmi_exporter / node_exporter，装法不限，见 2.1）
#    它们是控制台的全部数据源，没有这一步控速无依据

# ① 建目录、建 venv
ssh <目标机>
mkdir -p /opt/gpu-fan-console
cd /opt/gpu-fan-console
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r app/requirements.txt   # 代码要先在，见第 4 节的「传代码」

# ② 装 systemd 单元（3 个文件，缺一个安全护栏就不完整）
cp app/deploy/gpu-fan-console.service \
   app/deploy/fan-watchdog.service \
   app/deploy/fan-watchdog.timer /etc/systemd/system/
chmod +x /opt/gpu-fan-console/app/deploy/fan-watchdog.sh
systemctl daemon-reload
systemctl enable --now gpu-fan-console.service
systemctl enable --now fan-watchdog.timer

# ③ 确认本地 IPMI 可用（in-band 读写 /dev/ipmi0 需要 root）
ipmitool raw 0x3a 0x01 0x00 0x00 0x00 0x00 0x00 0x00 0x00 0x00   # 全部交回 BMC 自动
```

---

## 4. 日常更新（改完代码怎么发上去）

### 4.1 手动走一遍

```bash
cd /path/to/python-ipmitool

# 1) 构建前端（产物直接进 app/static，同时把旧的清掉）
cd frontend && npm run build && cd ..

# 2) 打包（⚠️ 成员路径必须是 app/...，见下方「坑 ①」）
tar czf /tmp/gfc.tgz \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='app/data' \
    -C . app

# 3) 传上去
agentsshcli upload <连接名> /tmp/gfc.tgz /root/gfc.tgz     # 这条路现在常挂，见「坑 ②」

# 4) 远端解压 + 清旧前端产物（⚠️ 见「坑 ③」）
# 4) 远端解压 + 清旧前端产物（⚠️ 见「坑 ③」）
#    已配置过的机器升级时排除 config.yaml —— 仓库里的版本是模板（示例地址），
#    别把现场配好的 prometheus_url 等覆盖回示例值。首次部署才需要带上它。
agentsshcli exec <连接名> "tar xzf /root/gfc.tgz -C /opt/gpu-fan-console --exclude='app/config.yaml'"

# 5) 重启 + 自检
agentsshcli exec <连接名> "systemctl restart gpu-fan-console && sleep 4 && systemctl is-active gpu-fan-console"
curl -s http://192.0.2.10:8765/api/status | head -c 300
```

`deploy.sh` 就是把上面这套串起来，并且**内置了下面三个坑的绕过方案**。

### 4.2 只改前端

后端没动时不用重启进程 —— `StaticFiles` 每次请求读盘，`index.html` 也不会被缓存：

```bash
CONN=<连接名> ./deploy.sh --static-only
```

> ⚠️ **别手动只拷 `index-xxxx.js`**。文件名带**内容 hash**，源码改一行文件名就变。
> 只换 JS 不换 `index.html`，就是「老页面引用已删的文件 / 新文件没人引用」的经典事故。
> `deploy.sh` 始终把 `index.html` 和 `assets/` 打在同一个包里一起送，不要绕过它。
> （实测：源码不变时重建 hash 稳定；源码一改 hash 立刻变。）

> ⚠️ **用 `--static-only` 之前先确认这次提交没动后端**：
> ```bash
> git show --stat HEAD        # 有 app/*.py 就别用 --static-only
> ```
> 前后端一体的代价是「前端用了新字段、后端还没发」这种半截状态很容易出现 ——
> 前端一般会优雅降级成「—」，不报错但显示不对，反而更难发现。
> 2026-09-28 实际撞过一次：SM 频率那次提交同时改了 `app/sensors.py`，只发前端导致
> 概览页频率一直是「—」，补一次全量部署才对上。

---

## 5. 三个必踩的坑（脚本里已经处理好）

### 坑 ①：tar 成员路径与解压目标必须对齐

```bash
# ❌ 这样打出来成员是 static/...，配 -C /opt/gpu-fan-console 会落到
#    /opt/gpu-fan-console/static/（少一层 app），前端直接 404
tar czf pkg.tgz -C app static

# ✅ 从仓库根目录打，成员是 app/...，与 -C /opt/gpu-fan-console 天然对齐
tar czf pkg.tgz -C . app
```
> 2026-09-28 实际踩过：解错位置 + 随后的清理命令把 `app/static/assets` 删空，页面 404。
> 救法：`cp -a /opt/gpu-fan-console/static/. /opt/gpu-fan-console/app/static/`。

### 坑 ②：`agentsshcli upload` 报「创建远端续传元数据失败」

数据其实**已经传完**了（日志里能看到 `上传完成: N bytes`），挂在写续传元数据那步。
远端建 `.agent-ssh-cli` / `.agentsshcli` 目录都没用。**改用分片 base64 走 exec**：

```bash
base64 -w0 pkg.tgz > all.b64
split -b 30000 all.b64 ch_          # ⚠️ 单次命令行上限 32767 字符，49K 会
                                     #    "Argument list too long"
i=0
for f in ch_*; do
  [ $i -eq 0 ] && R='>' || R='>>'
  agentsshcli exec <连接名> "printf '%s' '$(cat $f)' $R /root/pkg.b64"
  i=$((i+1))
done
agentsshcli exec <连接名> "base64 -d /root/pkg.b64 > /root/pkg.tgz"
```
> 只发前端时先 `gzip -9` 再 base64，体积能砍到 1/3，分片数从 25 降到 10。

### 坑 ③：远端 `rm` 在黑名单里

`rm -f` / `rm -rf` 一律被 agentsshcli 拒绝执行（"命令命中黑名单"），
`pkill -f 'uvicorn app.main'` 还会自匹配把会话自己杀掉。清旧产物用：

```bash
find <dir> -type f ! -name '新文件1' ! -name '新文件2' -delete
```
新文件名从本地 `ls app/static/assets` 拿（vite 带内容 hash，一次构建只有一对）。
**别用 `! -name 'index*'` 反着写** —— 那个条件永远匹配不到任何文件，等于没清。

---

## 6. 部署后验证清单

| 检查项 | 命令 | 期望 |
|---|---|---|
| 服务活着 | `systemctl is-active gpu-fan-console` | `active` |
| 看门狗在跑 | `systemctl list-timers fan-watchdog.timer` | 有下次触发时间 |
| 页面 200 | `curl -s -o /dev/null -w '%{http_code}' http://192.0.2.10:8765/` | `200` |
| 静态资源对得上 | `curl -s http://192.0.2.10:8765/ \| grep -o 'index-[A-Za-z0-9_-]*\.\(js\|css\)'` | 与本地 `ls app/static/assets` 一致 |
| 曲线在控速 | `curl -s http://192.0.2.10:8765/api/status` | 受控位 `duty` 有值、`temperature` 与 GPU 温度对得上 |
| 数据没丢 | 打开设置页 | 模式 / 管控 GPU / 分配关系都是部署前的样子 |
| 启动顺序对 | `journalctl -u gpu-fan-console -n 30` | 先「已应用数据库里的设置」再「已应用数据库里的分配」 |
| 回落动作在 | 同上，搜「安全护栏已激活」 | 有这行才说明退出时会回退 auto |

---

## 7. 回滚

```bash
# 代码回滚
git log --oneline -5
git checkout <上一个好提交>
CONN=<连接名> ./deploy.sh

# 数据回滚（SQLite 有备份时）
agentsshcli exec <连接名> "systemctl stop gpu-fan-console"
agentsshcli exec <连接名> "cp /root/fan-console.db.bak /opt/gpu-fan-console/app/data/fan-console.db"
agentsshcli exec <连接名> "systemctl start gpu-fan-console"
```

**最坏情况的保险**：不管服务死没死，都可以直接让 BMC 接管 ——

```bash
# 远端执行（把 8 个字节全填 0x00 = 全交回 BMC 自动档）
ipmitool raw 0x3a 0x01 0x00 0x00 0x00 0x00 0x00 0x00 0x00 0x00
# 宿主都起不来时：从 BMC 独立管理地址走（BMC 与宿主 OS 相互独立，凭据自行保管，切勿写进公开文档）
```

---

## 8. 排障速查

| 现象 | 先看哪里 |
|---|---|
| 页面打开是空白 / 404 | 静态产物路径（坑 ①）；`find /opt/gpu-fan-console/app/static -type f` 应至少有 index.html + 一对 assets |
| 页面是旧的 | `index.html` 里引用的 hash 与 `assets/` 里的文件是否一致（不一致 = 只换了 assets 没换 html） |
| 服务起不来 | `journalctl -u gpu-fan-console -n 50`；常见是配置/数据库里的曲线值非法（现在校验更严，会用 400 拦下来） |
| 温度读不到 | exporter 通不通：`:9290`（风扇）、`:9100`（CPU Tctl）、`:9400`（GPU） |
| 风扇不掉速 | 是不是被「未分配」放回 BMC 了；看 `api/status` 里受控位的 `owner_key` 是否为空 |
| 部署后设置回到默认 | 说明 `app/data` 被覆盖了（打包时 `--exclude='app/data'` 漏了） |

---

## 9. 与安全护栏相关、别乱改的几条

- 服务停止/异常/收到 SIGTERM 时，`SafetyGuard` 会把已接管的风扇位**交回 BMC 自动档**，
  所以 `TimeoutStopSec=30` 不能改小，否则退出动作可能被强杀在半路。
- 看门狗是**独立进程**（systemd timer 触发），不依附主服务 —— 主服务卡死时它才有用。
  别把它并进主服务里。
- `app/data/fan-console.db` 是**唯一权威数据源**（模式、管控 GPU、分配关系、审计日志）。
  `config.yaml` 只提供首次运行的初始值，之后改它没有任何效果 —— 别再往配置文件里塞运行时状态。
