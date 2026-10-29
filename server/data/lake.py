"""Parquet + DuckDB 数据湖（D-06，V4.1 5.2–5.4）。

目录（<DATA_ROOT>/lake/）：
    raw/<dataset>/date=YYYY-MM-DD/v=NNN/{data.parquet, manifest.json}        原始层，不可变
    normalized/<dataset>/date=YYYY-MM-DD/v=NNN/{data.parquet, manifest.json} 标准化层，不可变
    features/<snapshot_id>/{data.parquet, manifest.json}                     特征与决策输入快照
    backtests/<strategy_id>/<strategy_version>/<experiment_id>/{...}          回测明细
    quotes/date=YYYY-MM-DD/{data.parquet, manifest.json}                      报价抓取快照

不变量：
- 分区一经写入不再修改：逻辑内容相同则幂等返回已有版本，不同则写成新版本（v+1），旧版本保留，
  回测按 `snapshot()` 绑定具体版本。写入先落临时目录再原子改名，
  文件设为只读（POSIX 上版本目录也只读）。
- manifest 记录来源、拉取时间（UTC）、行数、schema、两个哈希与授权条款链接：
  `content_sha256` 是去掉 schema 元数据后的 Arrow IPC 序列化哈希，用于幂等判定（与 pyarrow 版本、
  压缩无关）；`sha256` 是 Parquet 文件字节哈希，用于防篡改。manifest 与所在目录不一致即视为损坏。
- K 线（normalized 下的 bars_* 数据集）必须符合固定 schema：价格 decimal(18,6)、数量 decimal(24,4)、
  UTC 微秒时间戳；每根 K 线的开始时间（UTC 日期）必须落在分区日期内，(symbol, start) 唯一。
  读出时还原为 Decimal（小数位按列的 scale，如 500.100000，数值相等）与带时区的 datetime。
- 读取：一次读入文件字节，先校验哈希与 schema 再解析，绑定版本缺失即报错（失败即关闭）。
- 保留策略只会删除 features、quotes、backtests 下的过期内容；raw 与 normalized 永不删除；
  与决策关联的特征快照与已发布回测必须显式传入（不给默认值，漏传即无法调用）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from server.domain.enums import DataQuality
from server.domain.market import MarketBar
from server.domain.values import to_utc
from server.platforms import files as platform_files

_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
# 快照路径段：小写，不以点结尾（Windows 会去掉结尾的点），不用设备保留名
_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9_-])?")
_WINDOWS_RESERVED = re.compile(r"(con|prn|aux|nul|com\d|lpt\d)(\..*)?")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_VERSION_DIR = re.compile(r"v=(\d{3,})")
_DATE_DIR = re.compile(r"date=(\d{4}-\d{2}-\d{2})")
DATA_FILE = "data.parquet"
MANIFEST_FILE = "manifest.json"
STAGING_PREFIX = ".staging-"
IMMUTABLE_LAYERS = ("raw", "normalized")
SNAPSHOT_LAYERS = {"features": 1, "quotes": 1, "backtests": 3}  # 路径段数
RETENTION_DEPTH = {"features": 1, "quotes": 1, "backtests": 2}  # 保留任务删除的层级

PRICE = pa.decimal128(18, 6)
VOLUME = pa.decimal128(24, 4)
UTC_US = pa.timestamp("us", tz="UTC")
_BAR_FIELDS: list[pa.Field[Any]] = [
    pa.field("symbol", pa.string()),
    pa.field("start", UTC_US),
    pa.field("end", UTC_US),
    pa.field("open", PRICE),
    pa.field("high", PRICE),
    pa.field("low", PRICE),
    pa.field("close", PRICE),
    pa.field("volume", VOLUME),
    pa.field("source", pa.string()),
    pa.field("delayed", pa.bool_()),
    pa.field("quality", pa.list_(pa.string())),
    pa.field("adjustment_version", pa.string()),
]
BARS_SCHEMA = pa.schema(_BAR_FIELDS)


class LakeError(Exception):
    """数据湖不可用或内容不可信：调用方必须拒绝使用这批数据（失败即关闭）。"""


@dataclass(frozen=True)
class Manifest:
    layer: str
    dataset: str
    partition: str  # date=YYYY-MM-DD 或快照路径
    version: int
    sha256: str  # Parquet 文件字节哈希（防篡改）
    content_sha256: str  # 逻辑内容哈希（幂等判定）
    rows: int
    schema: str
    provider: str
    fetched_at: datetime
    license_url: str | None = None
    lineage: tuple[str, ...] = ()  # 来源分区（标准化层记录它用到的原始分区）
    extra: Mapping[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        data = {
            "layer": self.layer,
            "dataset": self.dataset,
            "partition": self.partition,
            "version": self.version,
            "sha256": self.sha256,
            "content_sha256": self.content_sha256,
            "rows": self.rows,
            "schema": self.schema,
            "provider": self.provider,
            "fetched_at": self.fetched_at.isoformat(),
            "license_url": self.license_url,
            "lineage": list(self.lineage),
            "extra": dict(self.extra),
        }
        return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        try:
            data = json.loads(text)
            lineage = data.get("lineage", [])
            extra = data.get("extra", {})
            if not isinstance(lineage, list) or not all(isinstance(x, str) for x in lineage):
                raise ValueError("lineage 必须是字符串数组")
            if not isinstance(extra, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in extra.items()
            ):
                raise ValueError("extra 必须是字符串映射")
            manifest = cls(
                layer=str(data["layer"]),
                dataset=str(data["dataset"]),
                partition=str(data["partition"]),
                version=int(data["version"]),
                sha256=str(data["sha256"]),
                content_sha256=str(data["content_sha256"]),
                rows=int(data["rows"]),
                schema=str(data["schema"]),
                provider=str(data["provider"]),
                fetched_at=to_utc(datetime.fromisoformat(data["fetched_at"])),
                license_url=data.get("license_url"),
                lineage=tuple(lineage),
                extra=extra,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LakeError(f"manifest 损坏：{exc}") from exc
        if not (_HEX64.fullmatch(manifest.sha256) and _HEX64.fullmatch(manifest.content_sha256)):
            raise LakeError("manifest 损坏：哈希必须是 64 位小写十六进制")
        return manifest


@dataclass(frozen=True)
class PartitionRef:
    path: Path
    manifest: Manifest

    @property
    def key(self) -> str:
        m = self.manifest
        if m.layer in IMMUTABLE_LAYERS:
            return f"{m.layer}/{m.dataset}/{m.partition}/v={m.version:03d}"
        return f"{m.layer}/{m.partition}"


@dataclass(frozen=True)
class RetentionPolicy:
    """V4.1 5.4 的默认保留期（可配置）。"""

    feature_days: int = 90
    quote_days: int = 30
    backtest_versions: int = 10


def _check(pattern: re.Pattern[str], value: str, what: str) -> str:
    if not pattern.fullmatch(value):
        raise LakeError(f"{what} 不合法：{value!r}")
    return value


def _snapshot_segment(value: str) -> str:
    _check(_ID, value, "快照路径")
    if _WINDOWS_RESERVED.fullmatch(value):
        raise LakeError(f"快照路径不得使用设备保留名：{value!r}")
    return value


def _quote_segment(value: str) -> str:
    match = _check(_DATE_DIR, value, "报价快照路径")
    try:
        date.fromisoformat(match[5:])
    except ValueError as exc:
        raise LakeError(f"报价快照日期不合法：{value!r}") from exc
    return value


def _canonical(table: pa.Table) -> pa.Table:
    """去掉 schema 与字段元数据（pandas index、来源信息等），只留逻辑内容。"""
    fields = [f.remove_metadata() for f in table.schema]
    # 合并分块：逻辑内容相同、分块布局不同（concat、切片、from_pandas）的表得到相同的内容哈希
    return table.cast(pa.schema(fields)).replace_schema_metadata(None).combine_chunks()


def _content_hash(table: pa.Table) -> str:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return hashlib.sha256(sink.getvalue().to_pybytes()).hexdigest()


def _parquet_bytes(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="zstd")
    return bytes(sink.getvalue().to_pybytes())


def _make_read_only(directory: Path, *, lock_directory: bool) -> None:
    for item in directory.iterdir():
        item.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    if lock_directory:
        platform_files.lock_directory(directory)  # Windows 上不生效（目录只读属性不阻止删除）


def _rmtree(path: Path) -> None:
    def unlock(func: Any, target: str, _exc: object) -> None:
        Path(target).chmod(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        func(target)

    shutil.rmtree(path, onexc=unlock)


class DataLake:
    def __init__(self, root: Path) -> None:
        self.root = root
        for layer in (*IMMUTABLE_LAYERS, *SNAPSHOT_LAYERS):
            (root / layer).mkdir(parents=True, exist_ok=True)

    # ---------- 写入 ----------

    def write_partition(  # noqa: PLR0913 - 分区元数据全部显式给出
        self,
        layer: str,
        dataset: str,
        day: date,
        table: pa.Table,
        *,
        provider: str,
        fetched_at: datetime,
        license_url: str | None = None,
        lineage: Sequence[str] = (),
    ) -> PartitionRef:
        """写一个日期分区（原始层或标准化层）。逻辑内容与最新版本相同则幂等返回，不同则写新版本。"""
        if layer not in IMMUTABLE_LAYERS:
            raise LakeError(f"按日期分区只用于 {IMMUTABLE_LAYERS}")
        if type(day) is not date:
            raise LakeError("分区日期必须是 date")
        _check(_NAME, dataset, "数据集名")
        if not all(isinstance(x, str) for x in lineage):
            raise LakeError("lineage 必须是字符串")
        try:
            fetched = to_utc(fetched_at)
            canonical = _canonical(table)
            if layer == "normalized" and dataset.startswith("bars_"):
                _check_bars(canonical, day)
            content = _content_hash(canonical)
            data = _parquet_bytes(canonical)
        except (pa.ArrowException, ValueError, TypeError) as exc:
            raise LakeError(f"{layer}/{dataset} {day} 的数据不合法：{exc}") from exc
        base = self.root / layer / dataset / f"date={day.isoformat()}"
        existing = self._versions(base)
        if existing and existing[-1].manifest.content_sha256 == content:
            return existing[-1]
        version = existing[-1].manifest.version + 1 if existing else 1
        manifest = Manifest(
            layer=layer,
            dataset=dataset,
            partition=f"date={day.isoformat()}",
            version=version,
            sha256=hashlib.sha256(data).hexdigest(),
            content_sha256=content,
            rows=canonical.num_rows,
            schema=str(canonical.schema),
            provider=provider,
            fetched_at=fetched,
            license_url=license_url,
            lineage=tuple(lineage),
        )
        try:
            return self._commit(base / f"v={version:03d}", data, manifest, lock_directory=True)
        except LakeError:
            # 并发写入同一分区：别人先写了同样的内容就幂等返回，否则失败即关闭
            latest = self._versions(base)
            if latest and latest[-1].manifest.content_sha256 == content:
                return latest[-1]
            raise

    def write_bars(  # noqa: PLR0913
        self,
        bars: Sequence[MarketBar],
        day: date,
        *,
        provider: str,
        fetched_at: datetime,
        lineage: Sequence[str] = (),
        dataset: str = "bars_1d",
    ) -> PartitionRef:
        """把标准化 K 线写进 normalized/<dataset>（按 symbol、start 排序）。

        lineage 记录它用到的原始分区。
        """
        if not dataset.startswith("bars_"):
            raise LakeError("K 线数据集名必须以 bars_ 开头")
        ordered = sorted(bars, key=lambda b: (b.symbol, b.start))
        return self.write_partition(
            "normalized",
            dataset,
            day,
            bars_table(ordered),
            provider=provider,
            fetched_at=fetched_at,
            lineage=lineage,
        )

    def write_snapshot(  # noqa: PLR0913
        self,
        layer: str,
        key: Sequence[str],
        table: pa.Table,
        *,
        provider: str,
        created_at: datetime,
        extra: Mapping[str, str] | None = None,
    ) -> PartitionRef:
        """写特征快照（features/<id>）、回测明细（backtests/<策略>/<版本>/<实验>）或报价快照（quotes/date=…）。

        这三类不可覆盖：同一路径已存在即报错。
        """
        if layer not in SNAPSHOT_LAYERS:
            raise LakeError(f"未知的快照层：{layer}")
        if len(key) != SNAPSHOT_LAYERS[layer]:
            raise LakeError(f"{layer} 的路径必须有 {SNAPSHOT_LAYERS[layer]} 段")
        parts = [_quote_segment(k) if layer == "quotes" else _snapshot_segment(k) for k in key]
        target = self.root / layer / Path(*parts)
        if target.exists():
            raise LakeError(f"{target} 已存在，快照不可覆盖")
        try:
            created = to_utc(created_at)
            canonical = _canonical(table)
            content = _content_hash(canonical)
            data = _parquet_bytes(canonical)
        except (pa.ArrowException, ValueError, TypeError) as exc:
            raise LakeError(f"{layer} 快照数据不合法：{exc}") from exc
        manifest = Manifest(
            layer=layer,
            dataset=layer,
            partition="/".join(parts),
            version=1,
            sha256=hashlib.sha256(data).hexdigest(),
            content_sha256=content,
            rows=canonical.num_rows,
            schema=str(canonical.schema),
            provider=provider,
            fetched_at=created,
            extra=dict(extra or {}),
        )
        return self._commit(target, data, manifest, lock_directory=False)

    def _commit(
        self, target: Path, data: bytes, manifest: Manifest, *, lock_directory: bool
    ) -> PartitionRef:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=target.parent))
        try:
            (staging / DATA_FILE).write_bytes(data)
            (staging / MANIFEST_FILE).write_text(manifest.to_json(), encoding="utf-8")
            _make_read_only(staging, lock_directory=False)
            if target.exists():  # POSIX 的 rename 会替换空目录：先查，绝不覆盖
                raise FileExistsError(target)
            os.rename(staging, target)
        except OSError as exc:
            _rmtree(staging)
            raise LakeError(f"写入 {target} 失败：{exc}") from exc
        if lock_directory:
            _make_read_only(target, lock_directory=True)
        return PartitionRef(target, manifest)

    # ---------- 目录 ----------

    def _versions(self, base: Path) -> list[PartitionRef]:
        if not base.is_dir():
            return []
        refs: list[PartitionRef] = []
        for child in base.iterdir():
            match = _VERSION_DIR.fullmatch(child.name)
            if match and child.is_dir():
                ref = self._load(child)
                if ref.manifest.version != int(match.group(1)):
                    raise LakeError(f"{child} 的 manifest 版本号与目录名不一致")
                refs.append(ref)
        return sorted(refs, key=lambda r: r.manifest.version)

    def _load(self, path: Path) -> PartitionRef:
        try:
            manifest = Manifest.from_json((path / MANIFEST_FILE).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise LakeError(f"读不到 {path} 的 manifest：{exc}") from exc
        rel = path.relative_to(self.root).parts
        depth = {"raw": 4, "normalized": 4, "features": 2, "quotes": 2, "backtests": 4}
        layer = rel[0] if rel else ""
        if depth.get(layer) != len(rel):
            raise LakeError(f"{path} 不在合法的分区层级上")
        if layer in IMMUTABLE_LAYERS:
            version = _VERSION_DIR.fullmatch(rel[3])
            if version is None or int(version.group(1)) != manifest.version:
                raise LakeError(f"{path} 的 manifest 版本号与目录名不一致")
        if manifest.layer != layer:
            raise LakeError(f"{path} 的 manifest 层与目录不一致")
        if layer in IMMUTABLE_LAYERS:
            consistent = (manifest.dataset, manifest.partition) == (rel[1], rel[2])
        else:
            consistent = manifest.partition == "/".join(rel[1:])
        if not consistent:
            raise LakeError(f"{path} 的 manifest 与所在目录不一致")
        return PartitionRef(path, manifest)

    def partitions(self, layer: str, dataset: str) -> dict[date, list[PartitionRef]]:
        """某数据集的全部日期分区及其版本（从旧到新）；没有任何版本的日期目录也列出（空列表）。"""
        base = self.root / layer / _check(_NAME, dataset, "数据集名")
        out: dict[date, list[PartitionRef]] = {}
        if not base.is_dir():
            return out
        for child in sorted(base.iterdir()):
            match = _DATE_DIR.fullmatch(child.name)
            if match:
                try:
                    day = date.fromisoformat(match.group(1))
                except ValueError as exc:
                    raise LakeError(f"分区目录日期不合法：{child}") from exc
                out[day] = self._versions(child)
        return out

    def catalog(self) -> list[Manifest]:
        """全部 manifest（目录），跳过写入中途留下的临时目录。"""
        return [
            self._load(p.parent).manifest
            for p in sorted(self.root.rglob(MANIFEST_FILE))
            if not any(part.startswith(STAGING_PREFIX) for part in p.parts)
        ]

    def snapshot(
        self, dataset: str, start: date, end: date, *, layer: str = "normalized"
    ) -> dict[date, int]:
        """[start, end] 内每个日期分区的最新版本号，供回测绑定数据版本（可复现）。"""
        return {
            day: refs[-1].manifest.version
            for day, refs in self.partitions(layer, dataset).items()
            if start <= day <= end and refs
        }

    # ---------- 读取 ----------

    def _read_verified(self, ref: PartitionRef) -> pa.Table:
        """一次读入字节：校验文件哈希后从同一份字节解析（没有校验与读取之间的时间窗）。"""
        try:
            data = (ref.path / DATA_FILE).read_bytes()
        except OSError as exc:
            raise LakeError(f"读不到 {ref.key}：{exc}") from exc
        if hashlib.sha256(data).hexdigest() != ref.manifest.sha256:
            raise LakeError(f"{ref.key} 的内容哈希与 manifest 不符")
        try:
            return pq.read_table(pa.BufferReader(data))
        except pa.ArrowException as exc:
            raise LakeError(f"{ref.key} 无法解析：{exc}") from exc

    def verify(self, ref: PartitionRef) -> None:
        """重新计算内容哈希，与 manifest 不符即报错（数据被改动）。"""
        self._read_verified(ref)

    def read_partition(self, ref: PartitionRef) -> pa.Table:
        """读整个分区（校验哈希后解析）：入湖合并同日分区时用（D-01a）。"""
        return self._read_verified(ref)

    def latest(self, layer: str, dataset: str, day: date) -> PartitionRef | None:
        """某日期分区的最新版本；没有分区或没有版本时返回 None。"""
        if layer not in IMMUTABLE_LAYERS:
            raise LakeError(f"按日期分区只用于 {IMMUTABLE_LAYERS}")
        _check(_NAME, dataset, "数据集名")
        refs = self._versions(self.root / layer / dataset / f"date={day.isoformat()}")
        return refs[-1] if refs else None

    def read_bars(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        dataset: str = "bars_1d",
        versions: Mapping[date, int] | None = None,
    ) -> list[MarketBar]:
        """读 [start, end] 内某标的的标准化 K 线。

        给定 versions 时只按绑定的版本读取：绑定日期没有分区、没有版本或没有该版本，一律报错。
        """
        if not dataset.startswith("bars_"):
            raise LakeError("只能按 K 线读取 bars_ 开头的数据集")
        partitions = self.partitions("normalized", dataset)
        chosen: list[PartitionRef] = []
        if versions is not None:
            for day in sorted(d for d in versions if start <= d <= end):
                refs = partitions.get(day, [])
                match = [r for r in refs if r.manifest.version == versions[day]]
                if not match:
                    raise LakeError(f"{dataset} {day} 没有绑定的版本 v={versions[day]:03d}")
                chosen.append(match[0])
        else:
            chosen = [refs[-1] for day, refs in partitions.items() if start <= day <= end and refs]
        tables = []
        for ref in chosen:
            table = self._read_verified(ref)
            if not table.schema.equals(BARS_SCHEMA):
                raise LakeError(f"{ref.key} 的 schema 不是标准 K 线 schema")
            tables.append(table)
        if not tables:
            return []
        lo = datetime.combine(start, time(), tzinfo=UTC)
        hi = datetime.combine(end + timedelta(days=1), time(), tzinfo=UTC)
        combined = pa.concat_tables(tables)
        query = (
            'select symbol, epoch_us(start), epoch_us("end"), open, high, low, close, volume,'
            " source, delayed, quality, adjustment_version from bars"
            " where symbol = ? and start >= ? and start < ? order by start"
        )
        with duckdb.connect() as con:
            con.register("bars", combined)
            rows = con.execute(query, [symbol, lo, hi]).fetchall()
        return [_bar_from_row(row) for row in rows]

    # ---------- 保留 ----------

    def plan_retention(
        self,
        now: datetime,
        policy: RetentionPolicy | None = None,
        *,
        keep_features: Collection[str],
        published_backtests: Collection[tuple[str, str]],
    ) -> list[Path]:
        """列出可以删除的目录（只预演，不删除）。保护集合必须显式传入，哪怕为空。

        - features：超过保留期且不在 keep_features（与交易决策关联的快照，永久保留）中的；
        - quotes：超过保留期的；
        - backtests：每个策略保留最近 N 个版本（按创建时间、同时间按名称）与全部已发布版本。
        raw、normalized 永不进入计划；写入中途留下的临时目录跳过。
        """
        policy = policy or RetentionPolicy()
        now = to_utc(now)
        keep = set(keep_features)
        published = set(published_backtests)
        plan: list[Path] = []
        for path in self._children(self.root / "features"):
            if path.name not in keep:
                age = now - self._load(path).manifest.fetched_at
                if age > timedelta(days=policy.feature_days):
                    plan.append(path)
        for path in self._children(self.root / "quotes"):
            day = date.fromisoformat(_quote_segment(path.name)[5:])
            if (now.date() - day).days > policy.quote_days:
                plan.append(path)
        for strategy in self._children(self.root / "backtests"):
            versions = sorted(
                self._children(strategy),
                key=lambda v: (
                    max(
                        (self._load(e).manifest.fetched_at for e in self._children(v)),
                        default=datetime.min.replace(tzinfo=UTC),
                    ),
                    v.name,
                ),
            )
            for version in versions[: max(len(versions) - policy.backtest_versions, 0)]:
                if (strategy.name, version.name) not in published:
                    plan.append(version)
        return plan

    @staticmethod
    def _children(path: Path) -> list[Path]:
        return sorted(
            p for p in path.iterdir() if p.is_dir() and not p.name.startswith(STAGING_PREFIX)
        )

    def apply_retention(
        self,
        plan: Iterable[Path],
        *,
        keep_features: Collection[str],
        published_backtests: Collection[tuple[str, str]],
    ) -> list[Path]:
        """删除计划中的目录。先全部校验、再删除：有一条不合规即整批拒绝。

        只允许 features/<id>、quotes/<date>、backtests/<策略>/<版本> 这一层的目录；
        再次排除受保护的快照与已发布回测；
        目标不得落在 raw、normalized 之下（防符号链接、junction）。
        """
        root = self.root.resolve()
        immutable = [(self.root / layer).resolve() for layer in IMMUTABLE_LAYERS]
        keep = set(keep_features)
        published = set(published_backtests)
        targets: list[Path] = []
        for item in plan:
            target = Path(item).resolve()
            try:
                rel = target.relative_to(root).parts
            except ValueError:
                raise LakeError(f"保留任务不得删除 {target}") from None
            layer = rel[0] if rel else ""
            ok = (
                layer in RETENTION_DEPTH
                and len(rel) == RETENTION_DEPTH[layer] + 1
                and target.is_dir()
                and not any(target.is_relative_to(i) for i in immutable)
                and not (layer == "features" and rel[1] in keep)
                and not (layer == "backtests" and (rel[1], rel[2]) in published)
            )
            if not ok:
                raise LakeError(f"保留任务不得删除 {target}")
            if target not in targets:
                targets.append(target)
        for target in targets:
            _rmtree(target)
        return targets


def check_bars(table: pa.Table, day: date) -> None:
    """写入前预检标准化 K 线分区（与 write_partition 的检查相同），不合法抛 LakeError。"""
    try:
        _check_bars(_canonical(table), day)
    except (pa.ArrowException, ValueError, TypeError) as exc:
        raise LakeError(f"{day} 的 K 线分区不合法：{exc}") from exc


def _check_bars(table: pa.Table, day: date) -> None:
    if not table.schema.equals(BARS_SCHEMA):
        raise ValueError(
            "K 线必须符合标准 schema（价格 decimal(18,6)、数量 decimal(24,4)、UTC 时间）"
        )
    lo = datetime.combine(day, time(), tzinfo=UTC)
    hi = lo + timedelta(days=1)
    seen: set[tuple[str, datetime]] = set()
    symbols = table.column("symbol").to_pylist()
    starts = table.column("start").to_pylist()
    for symbol, start in zip(symbols, starts, strict=True):
        if not isinstance(symbol, str) or not isinstance(start, datetime):
            raise ValueError("K 线的 symbol 与 start 不能为空")
        if not (lo <= start < hi):
            raise ValueError(f"{symbol} 的 K 线开始时间 {start.isoformat()} 不在分区日期 {day} 内")
        if (symbol, start) in seen:
            raise ValueError(f"{symbol} 在 {start.isoformat()} 有重复 K 线")
        seen.add((symbol, start))


def bars_table(bars: Sequence[MarketBar]) -> pa.Table:
    try:
        return pa.table(
            {
                "symbol": [b.symbol for b in bars],
                "start": [b.start for b in bars],
                "end": [b.end for b in bars],
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
                "source": [b.source for b in bars],
                "delayed": [b.delayed for b in bars],
                "quality": [sorted(q.value for q in b.quality) for b in bars],
                "adjustment_version": [b.adjustment_version for b in bars],
            },
            schema=BARS_SCHEMA,
        )
    except (pa.ArrowException, ValueError, TypeError) as exc:  # 精度或范围超出 schema：拒绝，不截断
        raise LakeError(f"K 线无法按标准 schema 存储：{exc}") from exc


def _from_us(micros: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=micros)


def _decimal(value: object, what: str) -> Decimal:
    if not isinstance(value, Decimal):  # 只接受 Decimal，绝不经 float
        raise LakeError(f"{what} 读回的类型是 {type(value).__name__}，不是 Decimal")
    return value


def _bar_from_row(row: tuple[Any, ...]) -> MarketBar:
    try:
        return _bar_from_row_unchecked(row)
    except LakeError:
        raise
    except (ValueError, TypeError) as exc:  # 含 pydantic ValidationError：存储的数据违反领域约束
        raise LakeError(f"读回的 K 线不合法：{exc}") from exc


def _bar_from_row_unchecked(row: tuple[Any, ...]) -> MarketBar:
    symbol, start_us, end_us, o, h, low, c, v, source, delayed, quality, adj = row
    return MarketBar(
        symbol=symbol,
        start=_from_us(start_us),
        end=_from_us(end_us),
        open=_decimal(o, "open"),
        high=_decimal(h, "high"),
        low=_decimal(low, "low"),
        close=_decimal(c, "close"),
        volume=_decimal(v, "volume"),
        source=source,
        delayed=delayed,
        quality=frozenset(DataQuality(q) for q in quality),
        adjustment_version=adj,
    )
