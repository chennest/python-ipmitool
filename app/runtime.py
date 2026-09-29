"""运行期资源定位。

源码运行（uvicorn / python -m）一切以 ``app/`` 包目录为根，与历史行为完全一致；
PyInstaller 冻结运行时只有一处差别：**可变资源必须落在 exe 旁边**——
解包临时目录（``sys._MEIPASS``）每次启动都会被清空，数据库和配置放那里
等于每次开新档。只读资源（前端构建产物）仍打进包里，从解包目录读即可。
"""

from __future__ import annotations

import sys
from pathlib import Path

#: PyInstaller 冻结运行（``sys.frozen`` 由打包器在引导脚本里注入）
FROZEN = getattr(sys, "frozen", False)

#: 唯一根目录：源码运行 = ``app/`` 包目录；冻结运行 = exe 所在目录。
#: 配置文件和 SQLite 数据库都从根上找，冻结后用户可以直接编辑 exe 旁的 config.yaml。
APP_ROOT = (
    Path(sys.executable).resolve().parent
    if FROZEN
    else Path(__file__).resolve().parent
)

#: 前端构建产物目录。冻结时由 PyInstaller datas 提供在解包目录（只读，够用）。
STATIC_DIR = (
    Path(getattr(sys, "_MEIPASS", "")) / "static"
    if FROZEN
    else APP_ROOT / "static"
)

#: 默认配置文件位置
DEFAULT_CONFIG_PATH = APP_ROOT / "config.yaml"

#: SQLite 数据库默认位置（跨重启持久的唯一权威数据源）
DEFAULT_DB_PATH = APP_ROOT / "data" / "fan-console.db"

#: 心跳文件默认位置。Linux 常驻服务走 /run（tmpfs，重启即清）；
#: 冻结运行没有 systemd 看门狗伴随，落在 exe 旁边即可。
DEFAULT_HEARTBEAT_PATH = (
    str(APP_ROOT / "heartbeat")
    if FROZEN
    else "/run/gpu-fan-console/heartbeat"
)
