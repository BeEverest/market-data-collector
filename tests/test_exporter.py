"""exporter.py 单元测试：口径说明、ZIP 打包、分类导出与快照清理。"""
from __future__ import annotations

import zipfile

import pandas as pd

import exporter
from monitor import RUN_FIELDS


def _history_frame(run_records: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(run_records, columns=RUN_FIELDS)


class TestNotes:
    def test_notes_for_filters_by_category(self):
        macro_notes = exporter._notes_for({"ETF", "注意"})
        assert list(macro_notes.columns) == ["类别", "字段 / 指标", "口径说明"]
        assert set(macro_notes["类别"]) == {"ETF", "注意"}
        assert "规模 = 份额(亿份) × 单位净值(元)" in macro_notes["口径说明"].values[0] or any(
            "规模 = 份额" in v for v in macro_notes["口径说明"]
        )

    def test_notes_include_rate_lower_upper(self):
        notes = exporter._notes_for({"宏观-美国"})
        text = " ".join(notes["口径说明"].astype(str))
        assert "DFEDTARU" in text and "DFEDTARL" in text


class TestZip:
    def test_zip_files_contains_all_inputs(self, base):
        a = base / "a.csv"
        b = base / "b.txt"
        a.write_text("hello", encoding="utf-8")
        b.write_text("world", encoding="utf-8")
        target = base / "bundle.zip"
        result = exporter._zip_files([str(a), str(b)], target)
        assert result == str(target)
        with zipfile.ZipFile(target) as zf:
            assert sorted(zf.namelist()) == ["a.csv", "b.txt"]
            assert zf.read("a.csv") == b"hello"


class TestCategoryExports:
    def test_export_macro(self, base, macro_df, config):
        files = exporter.export_macro(macro_df, base, config, stamp="20261122_210000")
        assert len(files) == 4
        assert [f.rsplit(".", 1)[-1] for f in files] == ["csv", "csv", "xlsx", "zip"]
        assert files[-1].endswith("宏观数据_20261122_210000.zip")
        with zipfile.ZipFile(files[-1]) as zf:
            assert len(zf.namelist()) == 3
            assert any("口径说明" in n for n in zf.namelist())
        assert pd.ExcelFile(files[2]).sheet_names == ["宏观数据", "口径说明"]
        assert len(pd.read_csv(files[0])) == len(macro_df)

    def test_export_etf(self, base, etf_df, config):
        files = exporter.export_etf(etf_df, base, config, stamp="20261122_210001")
        assert len(files) == 4
        assert files[-1].endswith("ETF数据_20261122_210001.zip")
        with zipfile.ZipFile(files[-1]) as zf:
            assert len(zf.namelist()) == 3
        assert pd.ExcelFile(files[2]).sheet_names == ["ETF规模", "口径说明"]

    def test_export_monitor(self, base, config, run_records):
        files = exporter.export_monitor(_history_frame(run_records), base, config, stamp="20261122_210002")
        assert len(files) == 4
        assert files[-1].endswith("接口监控_20261122_210002.zip")
        sheets = pd.ExcelFile(files[2]).sheet_names
        assert sheets[:4] == ["运行总览", "失败明细", "接口健康度", "调用明细"]
        assert sheets[-1] == "口径说明"
        detail = pd.read_csv(files[0])
        assert len(detail) == len(run_records)

    def test_export_full_has_no_nested_zip(self, base, config, macro_df, etf_df, run_records):
        files = exporter.export_full(macro_df, etf_df, _history_frame(run_records), base, config)
        assert len(files) == 10  # 3 类 × 3 个明细文件 + 1 个全量 ZIP
        assert sum(f.lower().endswith(".zip") for f in files) == 1
        assert files[-1].endswith(".zip")
        with zipfile.ZipFile(files[-1]) as zf:
            names = zf.namelist()
            assert len(names) == 9
            assert not any(n.lower().endswith(".zip") for n in names)
            assert any("宏观数据" in n for n in names)
            assert any("ETF数据" in n for n in names)
            assert any("接口监控" in n for n in names)


class TestPruneSnapshots:
    def test_keeps_only_latest_n_batches(self, base, config):
        out_dir = base / "output"
        batches = [
            "20261101_010101", "20261102_010101", "20261103_010101",
            "20261104_010101", "20261105_010101",
        ]
        for stamp in batches:
            (out_dir / f"宏观数据_{stamp}.csv").write_text("x", encoding="utf-8")
            (out_dir / f"ETF数据_{stamp}.csv").write_text("x", encoding="utf-8")
        removed = exporter.prune_snapshots(base, config)  # keep_snapshots=3
        assert len(removed) == 4  # 最早 2 批 × 2 文件
        assert not (out_dir / f"宏观数据_{batches[0]}.csv").exists()
        assert (out_dir / f"宏观数据_{batches[-1]}.csv").exists()

    def test_no_prune_when_within_limit(self, base, config):
        (base / "output" / "宏观数据_20261101_010101.csv").write_text("x", encoding="utf-8")
        assert exporter.prune_snapshots(base, config) == []
        assert (base / "output" / "宏观数据_20261101_010101.csv").exists()


class TestLegacyExport:
    def test_export_results_respects_formats(self, base, macro_df, etf_df):
        cfg = {"output": {"dir": "output", "formats": ["csv"], "filename_prefix": "legacy"}}
        files = exporter.export_results(macro_df, etf_df, base, cfg, "20261122_180000")
        assert len(files) == 2
        assert all(f.endswith(".csv") for f in files)

        cfg["output"]["formats"] = ["excel"]
        files = exporter.export_results(macro_df, etf_df, base, cfg, "20261122_180001")
        assert len(files) == 1 and files[0].endswith(".xlsx")
        assert "口径说明" in pd.ExcelFile(files[0]).sheet_names
