"""GPU 风扇控制台 PyInstaller 打包入口。

打包（仓库根目录执行，见 packaging/build_console.py）：
产物在 ``dist/gpu-fan-console/``，其中 exe 同级会放一份 config.yaml 作种子。

源码运行等价命令：``python -m uvicorn app.main:app --host 0.0.0.0 --port 8765``
"""

import uvicorn

from app.config import load_config
from app.main import app

if __name__ == "__main__":
    cfg = load_config()
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port)
