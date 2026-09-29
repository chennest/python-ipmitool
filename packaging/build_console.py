"""构建 GPU 风扇控制台独立可执行文件（PyInstaller onedir）。

跨平台脚本：Windows / Linux 都在**仓库根目录**执行 ``python packaging/build_console.py``。
产物在 ``dist/gpu-fan-console/`` —— exe 与 ``config.yaml``（种子配置）同级，
整目录压缩后即可分发。前置条件：

1. 前端已构建：``cd frontend && npm ci && npm run build``（产物落在 app/static/）
2. 已安装依赖：``pip install -r app/requirements.txt pyinstaller``
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "gpu-fan-console"

# Windows 控制台可能是 cp1252/GBK，打印中文会炸 —— 统一按 UTF-8 输出
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def make_archive(exe_dir: Path) -> Path:
    """把整目录打成对应平台的分发压缩包（Windows → zip / POSIX → tar.gz）。

    放在 Python 里做而不是 workflow 里调 tar/PowerShell：跨平台行为一致，
    zip 内条目用正斜杠（Compress-Archive 的反斜杠条目在部分解压器下会显示异常）。
    """
    import tarfile
    import zipfile

    suffix = "windows-x64.zip" if os.name == "nt" else "linux-x64.tar.gz"
    out = ROOT / "dist" / f"{NAME}-{suffix}"
    if out.exists():
        out.unlink()
    if suffix.endswith(".zip"):
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(exe_dir.rglob("*")):
                zf.write(path, path.relative_to(exe_dir.parent).as_posix())
    else:
        with tarfile.open(out, "w:gz") as tf:
            for path in sorted(exe_dir.rglob("*")):
                tf.add(path, arcname=path.relative_to(exe_dir.parent).as_posix())
    return out


def main() -> int:
    static_dir = ROOT / "app" / "static"
    if not (static_dir / "index.html").is_file():
        print("错误: app/static/index.html 不存在 —— 先构建前端 "
              "(cd frontend && npm ci && npm run build)", file=sys.stderr)
        return 1

    # --add-data 的分隔符 Windows 是 ';'，POSIX 是 ':'
    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean", "--noconfirm",
        "--name", NAME,
        "--paths", ".",
        # 前端产物打进解包目录（只读资源）；config.yaml 与数据库运行时落在 exe 旁
        "--add-data", f"app/static{sep}static",
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        str(ROOT / "entry_console.py"),
    ]
    print("+", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=ROOT)
    if proc.returncode != 0:
        return proc.returncode

    # 种子配置放到 exe 旁边：冻结后 DEFAULT_CONFIG_PATH 指向这里
    exe_dir = ROOT / "dist" / NAME
    shutil.copy2(ROOT / "app" / "config.yaml", exe_dir / "config.yaml")
    print(f"构建完成: {exe_dir}")
    print(f"分发包: {make_archive(exe_dir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
