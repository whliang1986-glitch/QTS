"""D-06：Parquet + DuckDB 数据湖（不可变分区、版本、manifest、保留策略）。"""

from __future__ import annotations

import os
import shutil
import stat
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pytest
from server.data.lake import (
    DATA_FILE,
    MANIFEST_FILE,
    DataLake,
    LakeError,
    Manifest,
    RetentionPolicy,
    bars_table,
)
from server.domain.enums import DataQuality
from server.domain.market import MarketBar

T0 = datetime(2026, 10, 22, 21, 0, tzinfo=UTC)
D1, D2 = date(2026, 10, 21), date(2026, 10, 22)


def bar(day: date, close: str, symbol: str = "SPY", **kw: object) -> MarketBar:
    start = datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC)
    data: dict[str, object] = {
        "symbol": symbol,
        "start": start,
        "end": start + timedelta(hours=6, minutes=30),
        "open": Decimal("500.10"),
        "high": Decimal("505.25"),
        "low": Decimal("498.00"),
        "close": Decimal(close),
        "volume": Decimal("1234567.5"),
        "source": "ibkr",
        "adjustment_version": "raw",
    }
    data.update(kw)
    return MarketBar.model_validate(data)


@pytest.fixture
def lake(tmp_path: Path) -> DataLake:
    return DataLake(tmp_path / "lake")


def raw_table(n: int = 2) -> pa.Table:
    return pa.table(
        {"symbol": ["SPY"] * n, "close": pa.array([Decimal("1.5")] * n, pa.decimal128(18, 6))}
    )


# ---------- 原始层：不可变、版本、幂等 ----------


def test_raw_partition_layout_and_manifest(lake: DataLake) -> None:
    ref = lake.write_partition(
        "raw",
        "ibkr_bars",
        D1,
        raw_table(),
        provider="ibkr",
        fetched_at=T0,
        license_url="https://x/terms",
    )
    assert ref.path == lake.root / "raw" / "ibkr_bars" / "date=2026-10-21" / "v=001"
    assert (ref.path / DATA_FILE).is_file() and (ref.path / MANIFEST_FILE).is_file()
    m = ref.manifest
    assert (m.rows, m.provider, m.fetched_at, m.license_url) == (2, "ibkr", T0, "https://x/terms")
    assert len(m.sha256) == 64
    assert not os.access(ref.path / DATA_FILE, os.W_OK)  # 只读


def test_same_content_is_idempotent_new_content_is_new_version(lake: DataLake) -> None:
    a = lake.write_partition("raw", "ibkr_bars", D1, raw_table(), provider="ibkr", fetched_at=T0)
    again = lake.write_partition(
        "raw", "ibkr_bars", D1, raw_table(), provider="ibkr", fetched_at=T0
    )
    assert again.path == a.path and again.manifest.version == 1
    b = lake.write_partition("raw", "ibkr_bars", D1, raw_table(3), provider="ibkr", fetched_at=T0)
    assert b.manifest.version == 2
    assert (a.path / DATA_FILE).is_file()  # 旧版本保留
    versions = lake.partitions("raw", "ibkr_bars")[D1]
    assert [r.manifest.version for r in versions] == [1, 2]


def test_existing_version_directory_is_never_overwritten(lake: DataLake) -> None:
    ref = lake.write_partition("raw", "ibkr_bars", D1, raw_table(), provider="ibkr", fetched_at=T0)
    blocker = ref.path.parent / "v=002"
    blocker.mkdir()
    (blocker / MANIFEST_FILE).write_text(ref.manifest.to_json(), encoding="utf-8")  # 损坏的占位
    with pytest.raises(LakeError):
        lake.write_partition("raw", "ibkr_bars", D1, raw_table(5), provider="ibkr", fetched_at=T0)
    assert not any(p.name.startswith(".staging-") for p in ref.path.parent.iterdir())


@pytest.mark.parametrize(
    ("layer", "dataset", "day", "match"),
    [
        ("features", "x", D1, "按日期分区"),
        ("raw", "Bad-Name", D1, "数据集名"),
        ("raw", "../escape", D1, "数据集名"),
        ("raw", "x", datetime(2026, 10, 21, tzinfo=UTC), "date"),
    ],
)
def test_partition_arguments_are_validated(
    lake: DataLake, layer: str, dataset: str, day: date, match: str
) -> None:
    with pytest.raises(LakeError, match=match):
        lake.write_partition(layer, dataset, day, raw_table(), provider="p", fetched_at=T0)


def test_naive_fetch_time_is_rejected(lake: DataLake) -> None:
    with pytest.raises(LakeError):
        naive = datetime(2026, 1, 1)  # noqa: DTZ001 - 故意构造无时区时间
        lake.write_partition("raw", "x", D1, raw_table(), provider="p", fetched_at=naive)


def test_verify_detects_tampering(lake: DataLake) -> None:
    ref = lake.write_partition("raw", "x", D1, raw_table(), provider="p", fetched_at=T0)
    lake.verify(ref)
    target = ref.path / DATA_FILE
    target.chmod(stat.S_IWRITE | stat.S_IREAD)
    target.write_bytes(target.read_bytes() + b"x")
    with pytest.raises(LakeError, match="哈希"):
        lake.verify(ref)


# ---------- 标准化 K 线：Decimal 与 UTC 往返、版本绑定 ----------


def test_bars_round_trip_with_decimal_and_utc(lake: DataLake) -> None:
    raw = lake.write_partition("raw", "ibkr_bars", D1, raw_table(), provider="ibkr", fetched_at=T0)
    bars = [
        bar(D1, "503.12", quality=frozenset({DataQuality.STALE})),
        bar(D1, "99.5", symbol="QQQ", delayed=True),
    ]
    ref = lake.write_bars(bars, D1, provider="ibkr", fetched_at=T0, lineage=[raw.key])
    assert ref.manifest.lineage == (raw.key,)
    (got,) = lake.read_bars("SPY", D1, D1)
    assert got == bars[0]
    assert isinstance(got.close, Decimal) and got.start.tzinfo is not None
    assert lake.read_bars("QQQ", D1, D1)[0].delayed is True


def test_read_uses_latest_or_pinned_versions(lake: DataLake) -> None:
    lake.write_bars([bar(D1, "500")], D1, provider="p", fetched_at=T0)
    lake.write_bars([bar(D2, "501")], D2, provider="p", fetched_at=T0)
    pinned = lake.snapshot("bars_1d", D1, D2)
    assert pinned == {D1: 1, D2: 1}
    lake.write_bars([bar(D1, "500.5")], D1, provider="p", fetched_at=T0)  # 供应商修订
    assert [b.close for b in lake.read_bars("SPY", D1, D2)] == [Decimal("500.5"), Decimal(501)]
    # 回测绑定旧版本：结果可复现
    assert [b.close for b in lake.read_bars("SPY", D1, D2, versions=pinned)] == [
        Decimal(500),
        Decimal(501),
    ]


def test_pinned_version_missing_fails_closed(lake: DataLake) -> None:
    lake.write_bars([bar(D1, "500")], D1, provider="p", fetched_at=T0)
    with pytest.raises(LakeError, match="没有绑定的版本"):
        lake.read_bars("SPY", D1, D1, versions={D1: 7})
    with pytest.raises(LakeError, match="没有绑定的版本"):
        lake.read_bars("SPY", D1, D2, versions={D1: 1, D2: 1})


def test_read_verifies_hash_before_use(lake: DataLake) -> None:
    ref = lake.write_bars([bar(D1, "500")], D1, provider="p", fetched_at=T0)
    target = ref.path / DATA_FILE
    target.chmod(stat.S_IWRITE | stat.S_IREAD)
    target.write_bytes(bars_table([bar(D1, "999")]).to_pylist().__repr__().encode())
    with pytest.raises(LakeError, match="哈希"):
        lake.read_bars("SPY", D1, D1)


def test_empty_range_returns_nothing(lake: DataLake) -> None:
    assert lake.read_bars("SPY", D1, D2) == []


def test_catalog_lists_all_manifests(lake: DataLake) -> None:
    lake.write_partition("raw", "x", D1, raw_table(), provider="p", fetched_at=T0)
    lake.write_bars([bar(D1, "500")], D1, provider="p", fetched_at=T0)
    assert sorted((m.layer, m.dataset) for m in lake.catalog()) == [
        ("normalized", "bars_1d"),
        ("raw", "x"),
    ]


def test_corrupt_manifest_fails_closed(lake: DataLake) -> None:
    ref = lake.write_partition("raw", "x", D1, raw_table(), provider="p", fetched_at=T0)
    target = ref.path / MANIFEST_FILE
    target.chmod(stat.S_IWRITE | stat.S_IREAD)
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(LakeError, match="manifest"):
        lake.partitions("raw", "x")


# ---------- 快照与保留 ----------


def test_snapshots_cannot_be_overwritten(lake: DataLake) -> None:
    lake.write_snapshot("features", ["snap-1"], raw_table(), provider="workerd", created_at=T0)
    with pytest.raises(LakeError, match="不可覆盖"):
        lake.write_snapshot("features", ["snap-1"], raw_table(), provider="workerd", created_at=T0)
    with pytest.raises(LakeError, match="3 段"):
        lake.write_snapshot("backtests", ["s1", "v1"], raw_table(), provider="w", created_at=T0)
    with pytest.raises(LakeError, match="快照路径"):
        lake.write_snapshot("features", [".."], raw_table(), provider="w", created_at=T0)


def test_retention_plan_respects_policy(lake: DataLake) -> None:
    old, recent = T0 - timedelta(days=100), T0 - timedelta(days=10)
    lake.write_snapshot("features", ["old"], raw_table(), provider="w", created_at=old)
    lake.write_snapshot("features", ["old-linked"], raw_table(), provider="w", created_at=old)
    lake.write_snapshot("features", ["recent"], raw_table(), provider="w", created_at=recent)
    lake.write_snapshot("quotes", ["date=2026-09-01"], raw_table(), provider="w", created_at=old)
    lake.write_snapshot("quotes", ["date=2026-10-20"], raw_table(), provider="w", created_at=recent)
    for i in range(4):
        lake.write_snapshot(
            "backtests",
            ["s1", f"v{i}", "exp"],
            raw_table(),
            provider="w",
            created_at=old + timedelta(days=i),
        )
    lake.write_partition("raw", "x", date(2020, 1, 2), raw_table(), provider="p", fetched_at=old)
    plan = lake.plan_retention(
        T0,
        RetentionPolicy(backtest_versions=2),
        keep_features={"old-linked"},
        published_backtests={("s1", "v0")},
    )
    rel = sorted(p.relative_to(lake.root).as_posix() for p in plan)
    assert rel == ["backtests/s1/v1", "features/old", "quotes/date=2026-09-01"]
    deleted = lake.apply_retention(
        plan, keep_features={"old-linked"}, published_backtests={("s1", "v0")}
    )
    assert len(deleted) == 3 and all(not p.exists() for p in plan)
    assert (lake.root / "raw" / "x").is_dir()  # 原始层永不删除


def test_apply_retention_refuses_paths_outside_deletable_layers(lake: DataLake) -> None:
    ref = lake.write_partition("raw", "x", D1, raw_table(), provider="p", fetched_at=T0)
    keep = lake.write_snapshot("features", ["f"], raw_table(), provider="w", created_at=T0)
    for bad in [
        ref.path,
        lake.root / "features",
        lake.root / "features" / ".." / "raw",
        lake.root.parent,
    ]:
        with pytest.raises(LakeError, match="不得删除"):
            lake.apply_retention([keep.path, bad], keep_features=(), published_backtests=())
        assert keep.path.exists()  # 整批不执行


# ---------- 第一轮评审补充 ----------


def test_pinned_read_fails_on_date_directory_without_versions(lake: DataLake) -> None:
    lake.write_bars([bar(D1, "500")], D1, provider="p", fetched_at=T0)
    (lake.root / "normalized" / "bars_1d" / "date=2026-10-22").mkdir()  # 版本目录丢失
    with pytest.raises(LakeError, match="没有绑定的版本"):
        lake.read_bars("SPY", D1, D2, versions={D1: 1, D2: 1})


def test_bars_dataset_requires_standard_schema(lake: DataLake) -> None:
    floats = pa.table({"symbol": ["SPY"], "close": [500.1]})
    with pytest.raises(LakeError, match="标准 schema"):
        lake.write_partition("normalized", "bars_1d", D1, floats, provider="p", fetched_at=T0)
    # 非 bars_ 数据集可以写，但按 K 线读取时 schema 不符即拒绝，绝不经 float 转 Decimal
    lake.write_partition("normalized", "prices", D1, floats, provider="p", fetched_at=T0)
    with pytest.raises(LakeError, match="bars_"):
        lake.read_bars("SPY", D1, D1, dataset="prices")
    with pytest.raises(LakeError, match="bars_"):
        lake.write_bars([bar(D1, "500")], D1, provider="p", fetched_at=T0, dataset="prices")


def test_bars_must_belong_to_partition_day_and_be_unique(lake: DataLake) -> None:
    with pytest.raises(LakeError, match="不在分区日期"):
        lake.write_bars([bar(D2, "500")], D1, provider="p", fetched_at=T0)  # 未来数据写进前一天
    with pytest.raises(LakeError, match="重复"):
        lake.write_bars([bar(D1, "500"), bar(D1, "501")], D1, provider="p", fetched_at=T0)


def test_read_filters_by_bar_time_as_well(lake: DataLake) -> None:
    lake.write_bars([bar(D1, "500")], D1, provider="p", fetched_at=T0)
    lake.write_bars([bar(D2, "501")], D2, provider="p", fetched_at=T0)
    assert [b.start.date() for b in lake.read_bars("SPY", D1, D1)] == [D1]


def test_idempotency_ignores_schema_metadata_and_bar_order(lake: DataLake) -> None:
    a = lake.write_partition("raw", "x", D1, raw_table(), provider="p", fetched_at=T0)
    tagged = raw_table().replace_schema_metadata({b"pandas": b"{}"})
    assert lake.write_partition("raw", "x", D1, tagged, provider="p", fetched_at=T0).path == a.path
    bars = [bar(D1, "500"), bar(D1, "99", symbol="QQQ")]
    first = lake.write_bars(bars, D1, provider="p", fetched_at=T0)
    again = lake.write_bars(list(reversed(bars)), D1, provider="p", fetched_at=T0)
    assert again.path == first.path


@pytest.mark.parametrize(
    ("close", "volume"),
    [("500.0000001", "1"), ("1e13", "1"), ("500", "1.00001")],
)
def test_values_outside_schema_precision_are_rejected(
    lake: DataLake, close: str, volume: str
) -> None:
    with pytest.raises(LakeError):
        lake.write_bars([bar(D1, close, volume=Decimal(volume))], D1, provider="p", fetched_at=T0)


def test_non_utc_input_round_trips_as_utc(lake: DataLake) -> None:
    new_york = ZoneInfo("America/New_York")
    start = datetime(2026, 10, 21, 9, 30, tzinfo=new_york)
    original = bar(D1, "500", start=start, end=start + timedelta(hours=6, minutes=30))
    lake.write_bars([original], D1, provider="p", fetched_at=T0)
    (got,) = lake.read_bars("SPY", D1, D1)
    assert got.start == datetime(2026, 10, 21, 13, 30, tzinfo=UTC) and got.start.tzinfo is not None


def test_manifest_must_match_its_directory(lake: DataLake) -> None:
    ref = lake.write_partition("raw", "x", D1, raw_table(), provider="p", fetched_at=T0)
    moved = lake.root / "raw" / "x" / "date=2026-10-22" / "v=001"
    shutil.copytree(ref.path, moved)  # 把别的日期的版本整个复制过来
    with pytest.raises(LakeError, match="不一致"):
        lake.partitions("raw", "x")


def test_manifest_hash_format_is_checked(lake: DataLake) -> None:
    ref = lake.write_partition("raw", "x", D1, raw_table(), provider="p", fetched_at=T0)
    text = (ref.path / MANIFEST_FILE).read_text(encoding="utf-8")
    with pytest.raises(LakeError, match="十六进制"):
        Manifest.from_json(text.replace(ref.manifest.sha256, "abc"))
    with pytest.raises(LakeError, match="lineage"):
        Manifest.from_json(text.replace('"lineage": []', '"lineage": "raw/x"'))


@pytest.mark.parametrize("bad", ["snap.", "CON", "nul.txt", "Upper", "a b", "-x"])
def test_snapshot_ids_avoid_ambiguous_names(lake: DataLake, bad: str) -> None:
    with pytest.raises(LakeError):
        lake.write_snapshot("features", [bad], raw_table(), provider="w", created_at=T0)


def test_quote_dates_are_real_dates(lake: DataLake) -> None:
    with pytest.raises(LakeError, match="日期不合法"):
        lake.write_snapshot("quotes", ["date=2026-13-45"], raw_table(), provider="w", created_at=T0)


def test_apply_retention_enforces_depth_and_protection(lake: DataLake) -> None:
    keep = lake.write_snapshot("features", ["linked"], raw_table(), provider="w", created_at=T0)
    bt = lake.write_snapshot(
        "backtests", ["s1", "v1", "e1"], raw_table(), provider="w", created_at=T0
    )
    for bad, kw in [
        (keep.path, {"keep_features": {"linked"}, "published_backtests": ()}),
        (bt.path.parent, {"keep_features": (), "published_backtests": {("s1", "v1")}}),
        (bt.path.parent.parent, {"keep_features": (), "published_backtests": ()}),  # 整个策略
        (bt.path, {"keep_features": (), "published_backtests": ()}),  # 层级不对
        (keep.path / DATA_FILE, {"keep_features": (), "published_backtests": ()}),  # 文件
    ]:
        with pytest.raises(LakeError, match="不得删除"):
            lake.apply_retention([bad], **kw)  # type: ignore[arg-type]
    assert keep.path.exists() and bt.path.exists()


def test_retention_skips_leftover_staging_directories(lake: DataLake) -> None:
    (lake.root / "features" / ".staging-abc").mkdir()
    assert lake.plan_retention(T0, keep_features=(), published_backtests=()) == []


# ---------- 第二轮评审补充 ----------


def test_idempotency_ignores_chunk_layout(lake: DataLake) -> None:
    a = lake.write_partition("raw", "x", D1, raw_table(4), provider="p", fetched_at=T0)
    chunked = pa.concat_tables([raw_table(1), raw_table(3)])
    assert lake.write_partition("raw", "x", D1, chunked, provider="p", fetched_at=T0).path == a.path


def test_stray_manifest_is_rejected_by_catalog(lake: DataLake) -> None:
    lake.write_partition("raw", "x", D1, raw_table(), provider="p", fetched_at=T0)
    (lake.root / "raw" / MANIFEST_FILE).write_text("{}", encoding="utf-8")
    with pytest.raises(LakeError):
        lake.catalog()
