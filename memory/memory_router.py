"""
回复意愿路由器 — 时间感知与回复研判

实现宪章 §7.1 的消息聚合感知与回复意愿研判:
- 时间窗口聚合（多条消息合并分析）
- 延迟惩罚（stale message → 折扣）
- 回复意愿判定
- 按需组装搜索 Query
"""

import math
import time
from dataclasses import dataclass, field
from typing import Optional

from core.logging_manager import get_logger

logger = get_logger("memory_router", "green")


@dataclass
class AggregatedContext:
    """聚合后的上下文窗口"""

    messages: list = field(default_factory=list)  # [{"role": ..., "content": ..., "timestamp": ...}]
    oldest_timestamp: float = 0.0
    newest_timestamp: float = 0.0
    mentioned: bool = False  # 是否被 @ 提及
    query_keywords: list = field(default_factory=list)  # 提取的搜索关键词
    reply_score: float = 0.0  # 回复意愿分数 0~1


class MemoryRouter:
    """轻量级回复意愿路由器

    在触发 LLM 生成和记忆检索之前，先快速判断：
    1. 是否值得回复（reply intent）
    2. 应该检索什么（query keywords）
    """

    def __init__(self):
        # 时间窗口参数
        self.window_max_messages = 5    # 窗口内最大消息数
        self.window_timeout_sec = 5.0   # 窗口超时（秒）

        # 延迟惩罚参数
        self.delay_half_life = 120.0    # 延迟半衰期（秒），超过 2 分钟大幅折扣

        # 回复意愿阈值
        self.reply_threshold = 0.3      # 低于此分数不回复

        # 会话缓冲区: session_id -> [messages]
        self._buffers: dict[str, list] = {}
        self._buffer_timestamps: dict[str, float] = {}

    def buffer_message(
        self,
        session_id: str,
        role: str,
        content: str,
        user_id: str = "",
        mentioned: bool = False,
    ):
        """将消息放入时间窗口缓冲区"""
        now = time.time()

        if session_id not in self._buffers:
            self._buffers[session_id] = []
            self._buffer_timestamps[session_id] = now

        self._buffers[session_id].append({
            "role": role,
            "content": content,
            "user_id": user_id,
            "timestamp": now,
            "mentioned": mentioned,
        })

    def should_flush(self, session_id: str) -> bool:
        """检查缓冲区是否该刷新（达到消息数或超时）"""
        if session_id not in self._buffers:
            return False

        buf = self._buffers[session_id]
        if not buf:
            return False

        # 达到消息数上限
        if len(buf) >= self.window_max_messages:
            return True

        # 超时
        first_ts = self._buffer_timestamps.get(session_id, 0)
        if time.time() - first_ts >= self.window_timeout_sec:
            return True

        return False

    def flush_and_evaluate(self, session_id: str) -> Optional[AggregatedContext]:
        """刷新缓冲区，聚合分析，返回上下文

        Returns:
            AggregatedContext 或 None（无消息时）
        """
        buf = self._buffers.pop(session_id, [])
        self._buffer_timestamps.pop(session_id, None)

        if not buf:
            return None

        ctx = AggregatedContext()
        ctx.messages = buf
        ctx.oldest_timestamp = buf[0]["timestamp"]
        ctx.newest_timestamp = buf[-1]["timestamp"]
        ctx.mentioned = any(m.get("mentioned", False) for m in buf)

        # 计算回复意愿
        ctx.reply_score = self._calculate_reply_score(ctx)

        # 提取搜索关键词（简单提取：拼接所有用户消息）
        user_texts = [m["content"] for m in buf if m["role"] == "user"]
        ctx.query_keywords = user_texts  # 后续可以做更精细的 keyword extraction

        return ctx

    def _calculate_reply_score(self, ctx: AggregatedContext) -> float:
        """计算回复意愿分数

        因素:
        1. 是否被 @ 提及 → 强制高分
        2. 消息时效性（延迟惩罚）
        3. 用户消息数量占比
        """
        now = time.time()

        # 基础分
        score = 0.5

        # 被 @ 提及 → 强制回复
        if ctx.mentioned:
            score = 1.0
            return score

        # 延迟惩罚: 最旧消息的年龄
        message_age = now - ctx.oldest_timestamp
        delay_penalty = math.exp(-message_age / self.delay_half_life)
        score *= delay_penalty

        # 用户消息占比加成
        user_msg_count = sum(1 for m in ctx.messages if m["role"] == "user")
        total_count = len(ctx.messages)
        if total_count > 0:
            user_ratio = user_msg_count / total_count
            score *= (0.5 + user_ratio * 0.5)

        # 消息内容长度加成（长消息更可能需要回复）
        total_length = sum(len(m["content"]) for m in ctx.messages if m["role"] == "user")
        if total_length > 100:
            score *= 1.2
        elif total_length < 10:
            score *= 0.7

        return min(1.0, max(0.0, score))

    def should_reply(self, ctx: AggregatedContext) -> bool:
        """基于聚合上下文判断是否应该回复"""
        return ctx.reply_score >= self.reply_threshold

    def get_search_query(self, ctx: AggregatedContext) -> str:
        """从聚合上下文中组装记忆检索 Query"""
        # 合并所有用户消息文本
        user_texts = [m["content"] for m in ctx.messages if m["role"] == "user"]
        return " ".join(user_texts)

    @staticmethod
    def apply_delay_penalty(original_score: float, message_age_seconds: float) -> float:
        """对单个分数施加延迟惩罚

        Args:
            original_score: 原始分数
            message_age_seconds: 消息年龄（秒）

        Returns:
            惩罚后的分数
        """
        half_life = 120.0  # 2 分钟半衰期
        penalty = math.exp(-message_age_seconds / half_life)
        return original_score * penalty
