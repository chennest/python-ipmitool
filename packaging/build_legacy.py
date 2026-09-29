"""构建旧版 IPMI 风扇脚本的单文件可执行（PyInstaller onefile）。

在**仓库根目录**执行 ``python packaging/build_legacy.py``，产物在 ``dist/``：
``fancontroller``（循环模式）与 ``fancontroller_once``（单次模式）。
配置文件 ``fan_settings.yaml`` 与 ``logs/`` 都在 exe 旁边解析（见各脚本
``_app_directory()``），前置条件：``pip install pyinstaller``。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = ["fancontroller", "fancontroller_once"]

# Windows 控制台可能是 cp1252/GBK，打印中文会炸 —— 统一按 UTF-8 输出
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    for name in ENTRIES:
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--clean", "--noconfirm", "--onefile",
            "--name", name,
            "--paths", ".",
            f"{name}.py",
        ]
        print("+", " ".join(cmd))
        proc = subprocess.run(cmd, cwd=ROOT)
        if proc.returncode != 0:
            return proc.returncode
    print("构建完成:", ROOT / "dist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
