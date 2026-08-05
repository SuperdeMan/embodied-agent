# Ported from car-agent llm-gateway/tests/test_md_tts.py @ f0b08f8, changes: imports adapted to
# embodied.providers.audio (sys.path hack dropped); added _find_sentence_end unit test (helper was
# untested upstream).
"""TTS 入口 markdown 清理单测（与 car-agent grounding.strip_markdown_speech 同口径的最小实现）。"""
from __future__ import annotations

import asyncio

from embodied.providers.audio import _find_sentence_end, _sentence_segments, _strip_md_tts


def test_strip_md_tts_basic():
    assert _strip_md_tts("**加粗**和`代码`") == "加粗和代码"
    assert _strip_md_tts("# 标题\n- 列表项\n> 引用") == "标题\n列表项\n引用"
    assert _strip_md_tts("详见[公告](https://x.com)") == "详见公告"
    assert _strip_md_tts("A | B") == "A ， B"          # 竖线转停顿，不念符号
    assert _strip_md_tts("纯文本不动。") == "纯文本不动。"


def test_sentence_segments_strip_md_after_assembly():
    """句子组装完成后剥（跨增量 ** 对已合并，剥不漏），TTS 永不合成星号。"""
    async def deltas():
        for d in ("**固态电", "池**能量密度更高。", "第二`句`也干净。"):
            yield d

    async def collect():
        return [seg async for seg in _sentence_segments(deltas())]

    segs = asyncio.run(collect())
    joined = "".join(segs)
    assert "*" not in joined and "`" not in joined
    assert segs[0] == "固态电池能量密度更高。"


def test_find_sentence_end_first_hit_and_miss():
    assert _find_sentence_end("你好。世界！") == 2      # 首个句末标点
    assert _find_sentence_end("hi! ok?") == 2
    assert _find_sentence_end("换行\n也算") == 2
    assert _find_sentence_end("没有标点") == -1
    assert _find_sentence_end("") == -1
