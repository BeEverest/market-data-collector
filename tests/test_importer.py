"""Choice「份额与规模_<代码>.xlsx」导入器测试（合成数据，不依赖真实文件）。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import importer
import storage


def _make_choice_file(path: Path, rows: list[list], footnote: str = "数据来源：妙想Choice") -> Path:
    frame = pd.DataFrame(rows, columns=["截止日期", "流通份额(份)", "流通规模(元)", "公告日期"])
    if footnote:
        frame.loc[len(frame)] = [footnote, None, None, None]
    frame.to_excel(path, index=False)
    return path


def test_code_from_filename():
    assert importer.code_from_filename("份额与规模_159629.xlsx") == "159629"
    assert importer.code_from_filename("/tmp/份额与规模_510300.xlsx") == "510300"
    with pytest.raises(ValueError):
        importer.code_from_filename("随便一个文件.xlsx")


@pytest.mark.parametrize("code,market", [
    ("159629", "SZ"), ("159915", "SZ"), ("510300", "SH"), ("588080", "SH"), ("999999", ""),
])
def test_infer_market(code, market):
    assert importer.infer_market(code) == market


def test_parse_choice_file_conversion_and_footnote(tmp_path: Path):
    path = _make_choice_file(tmp_path / "份额与规模_159629.xlsx", [
        ["2026-11-20", 795252663, 1882204002.7884, "2026-11-21"],
        ["2026-11-19", 815252663, 2028511676.0766, "2026-11-20"],
    ])
    df = importer.parse_choice_file(path)
    assert len(df) == 2  # 脚注行被过滤
    first = df.iloc[0]
    assert first["基金代码"] == "159629"
    assert first["交易市场"] == "SZ"
    assert first["交易日期"] == "2026-11-20"
    assert first["数据源"] == importer.DEFAULT_SOURCE
    assert first["份额(亿份)"] == pytest.approx(795252663 / 1e8, abs=1e-8)
    assert first["规模(亿元)"] == pytest.approx(1882204002.7884 / 1e8, abs=1e-8)
    assert first["单位净值(元)"] == pytest.approx(1882204002.7884 / 795252663, abs=1e-4)


def test_parse_choice_file_missing_columns(tmp_path: Path):
    path = tmp_path / "份额与规模_159629.xlsx"
    pd.DataFrame({"日期": ["2026-11-20"], "数值": [1]}).to_excel(path, index=False)
    with pytest.raises(ValueError, match="缺少必需列"):
        importer.parse_choice_file(path)


def test_import_choice_file_writes_sqlite(tmp_path: Path, db_path: Path):
    path = _make_choice_file(tmp_path / "份额与规模_510300.xlsx", [
        ["2026-11-20", 100000000, 350000000, "2026-11-21"],
    ])
    summary = importer.import_choice_file(path, db_path, run_id="import-test")
    assert summary["code"] == "510300"
    assert summary["rows"] == 1
    total, out = storage.read_etf(db_path, "510300")
    assert total == 1
    row = out.iloc[0]
    assert row["份额(亿份)"] == pytest.approx(1.0)
    assert row["单位净值(元)"] == pytest.approx(3.5)
    assert row["规模(亿元)"] == pytest.approx(3.5)
    assert row["数据源"] == importer.DEFAULT_SOURCE


def test_import_idempotent(tmp_path: Path, db_path: Path):
    """同一 biz_key 重复导入应更新而非重复插入。"""
    path = _make_choice_file(tmp_path / "份额与规模_159629.xlsx", [
        ["2026-11-20", 100000000, 200000000, "2026-11-21"],
    ])
    importer.import_choice_file(path, db_path)
    importer.import_choice_file(path, db_path)
    total, _ = storage.read_etf(db_path, "159629")
    assert total == 1


def test_import_choice_paths_directory(tmp_path: Path, db_path: Path):
    d = tmp_path / "files"
    d.mkdir()
    _make_choice_file(d / "份额与规模_159629.xlsx", [
        ["2026-11-20", 100000000, 200000000, "2026-11-21"],
    ])
    _make_choice_file(d / "份额与规模_510300.xlsx", [
        ["2026-11-20", 100000000, 300000000, "2026-11-21"],
    ])
    (d / "别的文件.xlsx").write_bytes(b"")
    results = importer.import_choice_paths([d], db_path)
    assert {r["code"] for r in results} == {"159629", "510300"}
    assert storage.read_etf(db_path, "159629")[0] == 1
    assert storage.read_etf(db_path, "510300")[0] == 1
