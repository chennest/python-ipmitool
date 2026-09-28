#!/bin/bash
# 心跳看门狗 —— 进程内捕获不到的退出场景的最后一道防线。
#
# 背景：主进程在退出路径上会把风扇交回 BMC 自动控制（SafetyGuard 负责）。
# 但有两种情况它做不到：
#   1. 被 SIGKILL 强杀（信号捕获不了）
#   2. 卡死在某个调用里，进程还在但控制回路已经停了
#
# 本脚本由独立的 systemd timer 定期触发，发现心跳过期就强制回落。
# 这个脚本故意写得"笨"——它只做一件事，且不依赖主程序的任何代码。
#
# 部署：见同目录的 fan-watchdog.service / fan-watchdog.timer

set -u

HEARTBEAT="${HEARTBEAT:-/run/gpu-fan-console/heartbeat}"
# 心跳超时阈值（秒）。必须大于主进程控制周期 × 若干倍，
# 否则正常的短暂卡顿就会误触发。
TIMEOUT="${TIMEOUT:-180}"
IPMITOOL="${IPMITOOL:-/usr/bin/ipmitool}"
SERVICE="${SERVICE:-gpu-fan-console.service}"

# 全部风扇位交回 BMC 自动（8 字节必须写满，少一个字节 BMC 会静默忽略）
AUTO_PAYLOAD=(raw 0x3a 0x01 0x00 0x00 0x00 0x00 0x00 0x00 0x00 0x00)

log() {
    local msg="[fan-watchdog] $*"
    echo "$(date '+%F %T') $msg"
    command -v logger >/dev/null 2>&1 && logger -t fan-watchdog "$*"
    return 0
}

# ---------------------------------------------------------------- 主逻辑

if [ ! -f "$HEARTBEAT" ]; then
    # 没有心跳文件 = 主进程从未启动过，或者已经正常停止（正常停止会清理心跳
    # 并且已经回落过）。两种情况都不需要干预。
    exit 0
fi

mtime=$(stat -c %Y "$HEARTBEAT" 2>/dev/null || echo 0)
now=$(date +%s)
age=$(( now - mtime ))

if [ "$age" -le "$TIMEOUT" ]; then
    exit 0   # 心跳新鲜，一切正常
fi

log "⚠️ 心跳已过期 ${age}s（阈值 ${TIMEOUT}s）—— 主进程可能已卡死或被强杀"

if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    log "服务仍显示 active 但心跳过期，判定为卡死，执行强制回落"
else
    log "服务已非 active，执行强制回落"
fi

if "$IPMITOOL" "${AUTO_PAYLOAD[@]}" >/dev/null 2>&1; then
    log "✅ 已强制回落 BMC 自动控制"
    # 清掉过期心跳，避免下一次触发时重复告警
    rm -f "$HEARTBEAT"
    exit 0
else
    log "❌ 强制回落失败！请立即手动检查风扇状态（ipmitool sdr type fan）"
    exit 1
fi
