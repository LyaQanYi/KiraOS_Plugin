"""
KiraOS_Plugin — 通用工具函数（v3.0 精简版）

v2.x 的 UserMemoryDB / user_profiles / event_logs 已被 memory/ 子包中的
双脑记忆系统完全取代（详见 memory/memory_manager.py）。

本模块仅保留若干跨模块共用的工具函数：
- `_mask_id`     — 把 user_id / key 脱敏后用于日志
- `_parse_ttl`   — 解析 '30d' / '12h' 风格的过期时间字符串
"""

import hashlib
import re
from datetime import datetime, timedelta
from typing import Optional


def _mask_id(value: object) -> str:
    """Return a masked identifier safe for log files.

    user_id 与 memory key 不应该原样落进日志（日志可能被归档、共享或贴进 bug
    报告）。这里统一成 `<3-char prefix>***(<8-char sha256>)` 的形式。
    """
    s = "" if value is None else str(value)
    if not s:
        return "<empty>"
    digest = hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:8]
    prefix = s[:3] if len(s) >= 3 else s
    return f"{prefix}***({digest})"


def _parse_ttl(ttl: str) -> Optional[datetime]:
    """Parse a TTL string like '30d', '7d', '12h', '30m' into a datetime."""
    if not ttl:
        return None
    m = re.fullmatch(r"(\d+)\s*([dhm])", ttl.strip().lower())
    if not m:
        return None
    amount, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        return datetime.now() + timedelta(days=amount)
    if unit == "h":
        return datetime.now() + timedelta(hours=amount)
    if unit == "m":
        return datetime.now() + timedelta(minutes=amount)
    return None
