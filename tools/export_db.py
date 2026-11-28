"""一致性导出 SQLite 主库，用于备份 / 迁移到新环境。

关键点：
- 生产库运行在 WAL 模式，直接 cp 会漏掉 -wal 中未 checkpoint 的数据；
  这里使用 SQLite 官方 backup API，得到包含全部 WAL 内容的一致快照。
- 同时导出 SQL dump（可跨 SQLite 版本重建）、manifest.json（行数 + SHA256 + 完整性校验）。
- 全程只读源库，不修改、不清理任何采集原数据。

用法：
    python tools/export_db.py                      # 导出到 output/db_export
    python tools/export_db.py --out /tmp/backup    # 指定输出目录
    python tools/export_db.py --no-sql             # 只导出 .db + manifest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import storage  # noqa: E402


def _resolve_source(config_path: Path | None) -> Path:
    cfg = json.loads(config_path.read_text(encoding="utf-8")) if config_path else json.loads(
        (ROOT / "config.json").read_text(encoding="utf-8")
    )
    return Path(storage.db_path(cfg, ROOT))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _table_stats(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    stats: dict[str, int] = {}
    for name in names:
        try:
            stats[name] = int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        except sqlite3.DatabaseError:
            stats[name] = -1
    return stats


def export_database(source: Path, out_dir: Path, with_sql: bool = True) -> dict:
    if not source.exists():
        raise FileNotFoundError(f"源数据库不存在：{source}")
    out_dir.mkdir(parents=True, exist_ok=True)

    snapshot = out_dir / "market_data.db"
    tmp_snapshot = out_dir / ".market_data.db.exporting"
    if tmp_snapshot.exists():
        tmp_snapshot.unlink()

    # backup API 会自动把 WAL 中未落盘的事务合并进目标文件
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(tmp_snapshot)
    try:
        src.backup(dst)
        # 目标切回 DELETE 模式：导出物是单文件，不带 -wal/-shm 副作用，便于 scp/上传
        journal_mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        tables = _table_stats(dst)
    finally:
        dst.close()
        src.close()
    if integrity != "ok":
        tmp_snapshot.unlink(missing_ok=True)
        raise RuntimeError(f"导出快照完整性校验失败：{integrity}")
    tmp_snapshot.replace(snapshot)
    for suffix in ("-wal", "-shm"):
        (out_dir / f".market_data.db.exporting{suffix}").unlink(missing_ok=True)
        (out_dir / f"market_data.db{suffix}").unlink(missing_ok=True)

    manifest: dict = {
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_db": str(source),
        "source_size_bytes": source.stat().st_size,
        "snapshot_sha256": _sha256(snapshot),
        "snapshot_size_bytes": snapshot.stat().st_size,
        "journal_mode_in_snapshot": journal_mode,
        "integrity_check": integrity,
        "tables": tables,
        "total_rows": sum(v for v in tables.values() if v > 0),
    }

    if with_sql:
        sql_path = out_dir / "market_data.sql"
        snp = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        try:
            with sql_path.open("w", encoding="utf-8") as fh:
                for line in snp.iterdump():
                    fh.write(line + "\n")
        finally:
            snp.close()
        manifest["sql_dump_sha256"] = _sha256(sql_path)
        manifest["sql_dump_size_bytes"] = sql_path.stat().st_size

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="一致性导出 SQLite 主库（含 WAL）")
    parser.add_argument("--source", default=None, help="源库路径，默认读 config.json")
    parser.add_argument("--config", default=None, help="config.json 路径")
    parser.add_argument("--out", default=None, help="输出目录，默认 output/db_export")
    parser.add_argument("--no-sql", action="store_true", help="不生成 SQL dump")
    args = parser.parse_args()

    source = Path(args.source) if args.source else _resolve_source(
        Path(args.config) if args.config else None
    )
    out_dir = Path(args.out) if args.out else ROOT / "output" / "db_export"
    manifest = export_database(source, out_dir, with_sql=not args.no_sql)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
