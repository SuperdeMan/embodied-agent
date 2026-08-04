# Ported from car-agent llm-gateway/tests/test_embed.py @ f0b08f8, changes: imports adapted to
# embodied.providers.llm; gRPC servicer tests (2) omitted (server.py + proto not in scope); added
# an HTTP-level embed test via httpx.MockTransport (request body / auth / index-sort untested
# upstream at provider level).
"""Provider embed 单测：mock 伪向量 + OpenAI 兼容 /embeddings 配置与请求。"""
from __future__ import annotations

import asyncio
import json

import httpx

from embodied.providers.llm import _EMBED_DIM, MockProvider, OpenAICompatibleProvider


def test_mock_embed_dim_and_deterministic():
    p = MockProvider()
    v1 = asyncio.run(p.embed(["你好", "世界"]))
    assert len(v1) == 2 and all(len(v) == _EMBED_DIM for v in v1)
    v2 = asyncio.run(p.embed(["你好"]))
    assert v2[0] == v1[0]  # 确定性


def test_openai_embed_url_derivation():
    p = OpenAICompatibleProvider("k", base_url="https://x.test/v1/chat/completions")
    assert p.embed_url == "https://x.test/v1/embeddings"
    p2 = OpenAICompatibleProvider("k", base_url="https://x/v1/chat/completions",
                                  embed_url="https://y/custom/embed")
    assert p2.embed_url == "https://y/custom/embed"  # 显式覆盖


def test_embed_config_separate_key_and_auth():
    """embedding 用独立 key + bearer + 维度（百炼场景：chat=MiMo, embed=百炼）。"""
    p = OpenAICompatibleProvider(
        "mimo-key", base_url="https://mimo/v1/chat/completions",
        embed_url="https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
        embed_model="text-embedding-v4", embed_api_key="bailian-key",
        embed_auth_style="bearer", embed_dimensions=1024)
    assert p.embed_api_key == "bailian-key"      # 独立于 chat key
    assert p._embed_headers()["Authorization"] == "Bearer bailian-key"
    assert p.embed_dimensions == 1024
    # 缺省 embed key 回退 chat key
    p2 = OpenAICompatibleProvider("only-chat", base_url="https://x/v1/chat/completions")
    assert p2.embed_api_key == "only-chat"


def test_embed_posts_body_and_sorts_by_index():
    """新增：embed 请求体（model/input/dimensions/encoding_format）+ bearer 头 + index 排序。"""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0.2, 0.2]},
            {"index": 0, "embedding": [0.1, 0.1]},
        ]})

    p = OpenAICompatibleProvider(
        "chat-key", base_url="http://test.local/v1/chat/completions",
        embed_url="http://test.local/v1/embeddings", embed_model="text-embedding-v4",
        embed_api_key="ek", embed_auth_style="bearer", embed_dimensions=2)
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    vecs = asyncio.run(p.embed(["a", "b"]))
    assert vecs == [[0.1, 0.1], [0.2, 0.2]]      # 按 index 归位
    assert seen["url"].endswith("/v1/embeddings")
    assert seen["auth"] == "Bearer ek"
    assert seen["body"] == {"model": "text-embedding-v4", "input": ["a", "b"],
                            "dimensions": 2, "encoding_format": "float"}
