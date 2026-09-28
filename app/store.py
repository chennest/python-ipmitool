"""SQLite 持久化 —— 绑定关系 + 操作审计。

**为什么用 SQLite**：``sqlite3`` 是 Python 标准库，**零额外依赖**（这个项目到
现在只有 PyYAML 一个第三方依赖，想守住这条线）；同时它比 JSON 文件规范得多 ——
有事务、有类型、能查询，将来要扩展（曲线历史、按时间检索审计）也不用改结构。

存两类东西：

1. **``fan_bindings``** —— 风扇位 ↔ 温度源的绑定。用户运行时改的绑定得扛得住重启。
2. **``audit_log``** —— 操作审计。**这张表是今天被逼出来的**：两个 GPU 风扇位在
   11:34~11:59 之间从 3000 RPM 掉回 BMC 自动档，结果 uvicorn 日志被重启覆盖、
   IPMI raw 命令又不进 BMC SEL，**翻遍两边都没查出是谁发的命令**。有了审计表，
   这类「到底谁改的」问题以后直接查库。

并发注意：``sqlite3`` 的连接不能跨线程共享，而本应用有 asyncio 工作线程
（``asyncio.to_thread``）。所以这里**每次操作开一个新连接**（SQLite 打开极快），
再配 WAL 模式提升读写并发。写入频率本来就很低，不必上连接池。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

#: 默认数据库位置
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "fan-console.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fan_assignments (
    source_key  TEXT PRIMARY KEY,   -- "gpu:<uuid>" / "gpu:all" / "cpu"
    kind        TEXT NOT NULL,       -- gpu / gpu_group / cpu
    gpu_uuid    TEXT,               -- kind=gpu 时的 GPU UUID（绝不用 index）
    slots       TEXT NOT NULL DEFAULT '[]',  -- 分配给该源的风扇位（JSON 数组）
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    actor       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""


class Store:
    """极简持久化层。所有方法都是线程安全的（每次开新连接）。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------ 基础设施

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            # WAL：读写并发更友好，掉电安全性也更好
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        logger.info("持久化已就绪: %s", self.path)

    # ------------------------------------------------------------ 分配

    def load_assignments(self) -> list[dict[str, Any]] | None:
        """读取全部「散热源 → 风扇位」分配。

        表中无记录时返回 ``None``（首次使用，前端应弹分配向导）。
        """
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT source_key, kind, gpu_uuid, slots FROM fan_assignments"
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("读取分配失败: %s", exc)
            return None

        if not rows:
            return None

        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                slots = json.loads(row["slots"])
            except (json.JSONDecodeError, TypeError):
                slots = []
            result.append(
                {
                    "key": row["source_key"],
                    "kind": row["kind"],
                    "gpu_uuid": row["gpu_uuid"],
                    "slots": slots,
                }
            )
        return result

    def save_assignments(self, assignments: list[dict[str, Any]]) -> None:
        """整体覆盖分配（一个事务内完成，不会出现改了一半的状态）。"""
        now = time.time()
        with self._conn() as conn:
            # 先清空：分配是「全量语义」，不在列表里的源就等于未分配
            conn.execute("DELETE FROM fan_assignments")
            conn.executemany(
                "INSERT INTO fan_assignments (source_key, kind, gpu_uuid, slots, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        item.get("key", ""),
                        item.get("kind", "gpu"),
                        item.get("gpu_uuid"),
                        json.dumps(item.get("slots") or [], ensure_ascii=False),
                        now,
                    )
                    for item in assignments
                ],
            )
        logger.info("分配已持久化（%d 条）", len(assignments))

    # ------------------------------------------------------------ 审计

    def log(
        self,
        kind: str,
        actor: str,
        detail: str = "",
        ok: bool = True,
    ) -> None:
        """记一条审计。

        Args:
            kind: 类别 —— ``ipmi_write`` / ``api_call`` / ``lifecycle`` / ``error``
            actor: 触发者 —— ``auto_curve`` / ``manual`` / ``restore_auto`` /
                ``emergency`` / ``watchdog`` / ``api`` / ``startup`` / ``shutdown``
            detail: 人话描述（会被记进库，方便回溯）
            ok: 是否成功

        审计**绝不能反过来影响主流程** —— 所以这里吞掉所有异常，只记日志。
        """
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO audit_log (ts, kind, actor, detail, ok) VALUES (?, ?, ?, ?, ?)",
                    (time.time(), kind, actor, detail[:500], 1 if ok else 0),
                )
        except sqlite3.Error:
            logger.exception("写审计失败（不影响主流程）")

    def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        """最近的审计记录（新的在前）。"""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT id, ts, kind, actor, detail, ok FROM audit_log "
                    "ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("读取审计失败: %s", exc)
            return []

        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "kind": row["kind"],
                "actor": row["actor"],
                "detail": row["detail"],
                "ok": bool(row["ok"]),
            }
            for row in rows
        ]

    def prune_audit(self, keep_days: int = 30) -> int:
        """清理过期审计，返回删除条数。"""
        cutoff = time.time() - keep_days * 86400
        with self._conn() as conn:
            cursor = conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
            deleted = cursor.rowcount
        if deleted:
            logger.info("清理了 %d 条过期审计（保留 %d 天）", deleted, keep_days)
        return deleted

    # ------------------------------------------------------------ 运行时设置

    def load_settings(self) -> dict[str, Any]:
        """读取全部运行时设置（value 是 JSON，已反序列化）。

        返回空 dict 表示「还没有任何运行时设置」—— 调用方应沿用配置文件里的值。
        """
        try:
            with self._conn() as conn:
                rows = conn.execute("SELECT key, value FROM settings").fetchall()
        except sqlite3.Error as exc:
            logger.warning("读取设置失败，沿用配置文件: %s", exc)
            return {}

        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                logger.warning("设置项 %s 的值不是合法 JSON，已忽略", row["key"])
        return result

    def save_settings(self, settings: dict[str, Any]) -> None:
        """写入/更新设置。

        **部分更新语义** —— 只覆盖传进来的键，没传的保持原样。
        （绑定那张表相反，是全量覆盖，因为绑定本身就是一整份配置。）
        """
        if not settings:
            return
        now = time.time()
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                [
                    (key, json.dumps(value, ensure_ascii=False), now)
                    for key, value in settings.items()
                ],
            )
        logger.info("设置已持久化: %s", ", ".join(sorted(settings)))
