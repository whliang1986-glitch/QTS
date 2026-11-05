"""时区数据（B-03b）：tzdata 被依赖约束过一次，这里用固定日期确认美东时区的夏令时切换仍然正确。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

NEW_YORK = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("moment", "offset_hours"),
    [
        (datetime(2026, 7, 1, 15, 0, tzinfo=UTC), -4),  # 夏令时
        (datetime(2026, 11, 5, 15, 0, tzinfo=UTC), -5),  # 11 月第一个周日之后：标准时间
        (datetime(2026, 3, 8, 6, 59, tzinfo=UTC), -5),  # 3 月第二个周日 2:00 切换之前
        (datetime(2026, 3, 8, 7, 0, tzinfo=UTC), -4),  # 切换之后
    ],
)
def test_us_eastern_offsets(moment: datetime, offset_hours: int) -> None:
    assert moment.astimezone(NEW_YORK).utcoffset() == timedelta(hours=offset_hours)
