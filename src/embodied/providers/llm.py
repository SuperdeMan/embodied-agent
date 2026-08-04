# Ported from car-agent llm-gateway/providers.py @ f0b08f8, changes: LLM section only (lines 1-553;
# ASR/TTS/S2S providers dropped, out of scope for M1); removed unused `import re`. Logic unchanged.
"""LLM Provider 抽象与实现。**更换服务商优先改 env，无需改代码**。

- anthropic：Anthropic Claude API（独立 SDK）
- 其余（xiaomimimo/openai/deepseek/qwen/自建 vLLM…）：统一走 OpenAI 兼容 HTTP provider，
  端点 `LLM_BASE_URL`、鉴权 `LLM_AUTH_STYLE`、思考开关 `LLM_DISABLE_THINKING` 全经 env 注入。
- mock：无 `LLM_API_KEY` 时的回显兜底（PoC 可离线端到端）。

仅当需要一种全新的非 OpenAI 兼容协议时，才在此新增 Provider 类。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx

logger = logging.getLogger("llm.providers")

# ── 出站 HTTP 连接池 + 超时（复用连接，免去每调用新建 client 的 TLS 握手开销）──
_HTTP_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=16,
                            keepalive_expiry=30.0)
_HTTP_CONNECT_S = float(os.getenv("LLM_HTTP_CONNECT_S", "5") or 5)
_HTTP_READ_CAP_S = float(os.getenv("LLM_HTTP_READ_CAP_S", "75") or 75)   # complete 兜底上限
_STREAM_STALL_S = float(os.getenv("LLM_STREAM_STALL_S", "30") or 30)     # 流式 per-chunk 静默上限
_EMBED_READ_CAP_S = float(os.getenv("LLM_EMBED_READ_CAP_S", "25") or 25)


def _read_budget(budget_s, cap_s: float) -> float:
    """上游 read 超时：有调用方 deadline（gRPC context.time_remaining）时取其 90% 收进
    窗口内——网关先于调用方失败、返回干净错误，而非被调用方中途取消（"无响应"）；
    无 deadline 时用 cap 兜底。"""
    try:
        b = float(budget_s) if budget_s is not None else 0.0
    except (TypeError, ValueError):
        b = 0.0
    if b > 0:
        return max(1.0, min(cap_s, b * 0.9))
    return cap_s


def _http_timeout(budget_s, read_cap: float) -> httpx.Timeout:
    return httpx.Timeout(_read_budget(budget_s, read_cap),
                         connect=min(_HTTP_CONNECT_S, read_cap), pool=5.0)


def _strict_mock_gate(domain: str, why: str) -> None:
    """严格栈（REQUIRE_REAL_PROVIDERS=on，治理 P2）：mock 决议直接拒绝启动。
    豁免 REQUIRE_REAL_EXEMPT。"""
    if os.getenv("REQUIRE_REAL_PROVIDERS", "off").strip().lower() not in ("on", "true", "1", "yes"):
        return
    exempt = {d.strip() for d in
              os.getenv("REQUIRE_REAL_EXEMPT", "parking,knowledge").split(",") if d.strip()}
    if domain in exempt:
        return
    raise RuntimeError(
        f"REQUIRE_REAL_PROVIDERS=on：provider[{domain}] 将落 mock（{why}）——严格栈禁止；"
        f"补齐凭证或把 {domain} 加入 REQUIRE_REAL_EXEMPT")


class ProviderHTTPError(RuntimeError):
    """上游 HTTP 错误：状态码 + Retry-After 结构化（运行时硬化 D3，网关按语义分类映射——
    429→RESOURCE_EXHAUSTED、请求性 4xx→INVALID_ARGUMENT）；消息保持
    `provider HTTP <code>: <body片段>` 格式，日志/obs.llm error 可诊断口径不变。"""

    def __init__(self, status_code: int, snippet: str, retry_after: float | None = None):
        super().__init__(f"provider HTTP {status_code}: {snippet}")
        self.status_code = status_code
        self.retry_after = retry_after


def _retry_after_s(resp) -> float | None:
    """解析 Retry-After 秒数（仅数字形式；HTTP-date 形式少见，按无处理）。
    对无 headers 的测试桩防御（getattr）。"""
    headers = getattr(resp, "headers", None) or {}
    v = (headers.get("retry-after") or "").strip()
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        return None


def normalize_tool_calls(raw_calls) -> list[dict]:
    """OpenAI 形状 tool_calls → 网关统一形状 ``[{"id","name","arguments"(dict)}]``。

    M1a（submit_plan 结构化输出，RFC §3.1）：``function.arguments`` 是 JSON string，
    统一解析为 object 再下发——调用方（planning）不再管各家差异。畸形 arguments
    **丢弃该条**（warning 计数），刻意不做字符串抢救：tool-calling 的价值就是服务端
    约束，畸形=协议失败，诚实回退让调用方走 JSON 抢救/重试路径（RFC §8-3）。
    个别服务商直接给 object 的宽容接收。
    """
    out = []
    for tc in raw_calls or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if not name:
            continue
        args_raw = fn.get("arguments")
        if isinstance(args_raw, dict):
            args = args_raw
        else:
            try:
                args = json.loads(args_raw or "{}")
            except (TypeError, ValueError) as e:
                logger.warning("tool_call %s arguments 畸形，丢弃：%s", name, e)
                continue
        if not isinstance(args, dict):
            logger.warning("tool_call %s arguments 非 object，丢弃", name)
            continue
        out.append({"id": tc.get("id") or "", "name": name, "arguments": args})
    return out


class BaseProvider:
    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        """returns (content, model_used, finish_reason, (prompt_tokens, completion_tokens)).

        thinking: None=用服务商默认（env LLM_DISABLE_THINKING）；True=本次开思考；
        False=本次关思考。复杂任务（行程/调研）由编排层经 meta 动态传 True。
        """
        raise NotImplementedError

    async def complete_tools(self, messages, model, temperature, max_tokens,
                             tools=None, tool_choice=None, thinking=None, timeout_s=None):
        """带工具定义的补全（M1a submit_plan 结构化输出）。

        returns (content, model_used, finish_reason, usage, tool_calls)——tool_calls 为
        网关归一化形状 ``[{"id","name","arguments"(dict)}]``，无工具调用时 ``[]``。
        默认实现回落纯文本 ``complete`` + 空 tool_calls（Mock/未覆盖 provider fail-open：
        调用方按无工具调用处理，走既有 JSON 抢救/回退路径）。刻意不改 ``complete``
        四元组契约——仓内 fake/测试按其实现，独立方法存量零波及（RFC §8-1）。
        """
        content, used, finish, usage = await self.complete(
            messages, model, temperature, max_tokens, thinking=thinking, timeout_s=timeout_s)
        return content, used, finish, usage, []

    async def stream(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        raise NotImplementedError
        yield  # pragma: no cover

    async def embed(self, texts, model="", timeout_s=None):
        """returns list[list[float]]（与 texts 一一对应）。默认未实现，由子类提供。"""
        raise NotImplementedError


_EMBED_DIM = 384  # 与 memory.memory_item.embedding vector(384) 对齐


def _mock_embed_one(text: str) -> list[float]:
    """确定性伪向量（非语义，仅供无 key/降级时打通 pgvector 链路与测试）。"""
    import hashlib
    h = hashlib.sha256((text or "").encode()).digest()
    return [(h[i % len(h)] / 128.0) - 1.0 for i in range(_EMBED_DIM)]


class MockProvider(BaseProvider):
    """无 API key 时的兜底，保证 PoC 可离线端到端跑通。"""
    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        # T3.5 e2e_degrade.py 测试钩子：LLM_MOCK_DELAY_MS（默认 "0"，零行为变化）。调用时
        # （非构造时）读 env，供测试注入人为延迟，确定性触发 executor 层 step_timeout，
        # 刻画"LLM 超时"降级行为。stream() 内部调用本方法，无需重复加。
        delay_ms = int(os.getenv("LLM_MOCK_DELAY_MS", "0") or 0)
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        text = f"[mock] 我听到你说「{user}」。配置 LLM_API_KEY 后即可接入真实模型。"
        return text, "mock", "stop", (0, 0)

    async def stream(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        content, *_ = await self.complete(messages, model, temperature, max_tokens)
        for ch in content:
            yield ch

    async def embed(self, texts, model="", timeout_s=None):
        return [_mock_embed_one(t) for t in texts]


class AnthropicProvider(BaseProvider):
    def __init__(self, api_key: str):
        from anthropic import AsyncAnthropic
        self.client = AsyncAnthropic(api_key=api_key)

    @staticmethod
    def _split(messages):
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        msgs = [{"role": m["role"], "content": m["content"]}
                for m in messages if m["role"] in ("user", "assistant")]
        return system or None, msgs

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        # thinking 形参保持签名一致；Anthropic extended thinking 暂未接线（目标服务商是 MiMo）。
        system, msgs = self._split(messages)
        resp = await self.client.messages.create(
            model=model, system=system, messages=msgs,
            temperature=temperature, max_tokens=max_tokens or 512)
        text = "".join(b.text for b in resp.content if b.type == "text")
        return text, model, resp.stop_reason, (resp.usage.input_tokens, resp.usage.output_tokens)

    @staticmethod
    def _to_anthropic_tools(tools, tool_choice):
        """OpenAI 线格式 → Anthropic 专有形状（转换放最少数一侧，RFC §8-2）。
        tools: [{"type":"function","function":{name,description,parameters}}] →
        [{name, description, input_schema}]；tool_choice named→{"type":"tool"}、
        "required"→{"type":"any"}、"none"→{"type":"none"}、其余缺省 auto。"""
        a_tools = []
        for t in tools or []:
            fn = (t or {}).get("function") or {}
            if not fn.get("name"):
                continue
            a_tools.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object"},
            })
        a_choice = None
        if isinstance(tool_choice, dict):
            name = (tool_choice.get("function") or {}).get("name", "")
            a_choice = {"type": "tool", "name": name} if name else {"type": "auto"}
        elif tool_choice == "required":
            a_choice = {"type": "any"}
        elif tool_choice == "none":
            a_choice = {"type": "none"}
        return a_tools, a_choice

    async def complete_tools(self, messages, model, temperature, max_tokens,
                             tools=None, tool_choice=None, thinking=None, timeout_s=None):
        system, msgs = self._split(messages)
        kwargs = {}
        a_tools, a_choice = self._to_anthropic_tools(tools, tool_choice)
        if a_tools:
            kwargs["tools"] = a_tools
            if a_choice is not None:
                kwargs["tool_choice"] = a_choice
        resp = await self.client.messages.create(
            model=model, system=system, messages=msgs,
            temperature=temperature, max_tokens=max_tokens or 512, **kwargs)
        text = "".join(b.text for b in resp.content if b.type == "text")
        # tool_use block 的 input 天生 object（与 OpenAI 的 JSON string 不同），直取归一化
        tool_calls = [
            {"id": getattr(b, "id", "") or "", "name": getattr(b, "name", "") or "",
             "arguments": dict(b.input) if isinstance(getattr(b, "input", None), dict) else {}}
            for b in resp.content if b.type == "tool_use" and getattr(b, "name", "")
        ]
        return (text, model, resp.stop_reason,
                (resp.usage.input_tokens, resp.usage.output_tokens), tool_calls)

    async def stream(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        system, msgs = self._split(messages)
        async with self.client.messages.stream(
                model=model, system=system, messages=msgs,
                temperature=temperature, max_tokens=max_tokens or 512) as s:
            async for text in s.text_stream:
                yield text


# ── 推理模型 <think> 内联剥离 ────────────────────────────────────────────────
# MiniMax-M3 等推理模型**开思考**时把思考段内联在 content 头部（`<think>…</think>\n\n正文`），
# 而非独立 reasoning_content 字段（后者 stream 分支早已丢弃）。真栈探针（2026-07-12，四家
# × complete/stream × 开/关思考）：仅 MiniMax 开思考泄漏，mimo/deepseek/qwen 干净。
# 统一在 provider 出口剥——思考是内部推理，任何调用方（Planner/Agent/聚合）都不该收到。
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def strip_think_block(text: str) -> str:
    """剥离**头部** <think>…</think> 块。只看头部（推理模型先思考后作答），正文中间出现的
    字面 <think> 不动（防误伤转述场景）。未闭合（被 max_tokens 截断在思考里）→ 无正文可用，
    诚实返回空串（调用方按空响应既有兜底走重试/降级，绝不把半截思考当答案）。"""
    t = text or ""
    head = t.lstrip()
    if not head.startswith(_THINK_OPEN):
        return t
    end = head.find(_THINK_CLOSE)
    if end == -1:
        return ""
    return head[end + len(_THINK_CLOSE):].lstrip("\n").lstrip()


class ThinkStreamStripper:
    """流式头部 <think> 剥离状态机（与 strip_think_block 同语义，跨 chunk 安全）。

    probe：缓冲首若干字符判定是否 `<think>` 前缀（判定窗 ≤ len("<think>")+前导空白，
    普通回复只延迟一个包级别）；drop：吞到 `</think>` 后把余下正文放流；pass：透传。
    """

    def __init__(self):
        self._mode = "probe"        # probe | drop | pass
        self._buf = ""

    def feed(self, delta: str) -> str:
        if self._mode == "pass":
            return delta
        self._buf += delta
        if self._mode == "probe":
            probe = self._buf.lstrip()
            if not probe:
                return ""
            if probe.startswith(_THINK_OPEN):
                self._mode = "drop"
            elif _THINK_OPEN.startswith(probe[:len(_THINK_OPEN)]):
                return ""                       # 仍是 "<th" 类前缀，继续观望
            else:
                self._mode = "pass"
                out, self._buf = self._buf, ""
                return out
        if self._mode == "drop":
            end = self._buf.find(_THINK_CLOSE)
            if end == -1:
                return ""
            rest = self._buf[end + len(_THINK_CLOSE):].lstrip("\n").lstrip()
            self._mode = "pass"
            self._buf = ""
            return rest
        return ""

    def flush(self) -> str:
        """流结束收尾：probe 残留（极短回复恰似 "<th" 前缀）原样放出不丢字；
        drop 未闭合＝整段思考被截断，丢弃（与 strip_think_block 一致）。"""
        if self._mode == "probe":
            out, self._buf = self._buf, ""
            return out
        return ""


class OpenAICompatibleProvider(BaseProvider):
    """OpenAI 兼容 Chat Completions 提供商（MiMo / OpenAI / DeepSeek / Qwen / 本地 vLLM 等）。

    端点、鉴权、思考开关全部经配置注入——**更换 LLM 服务商只改 env、不动代码**：
    - LLM_BASE_URL：chat/completions 完整 URL（默认小米 MiMo）
    - LLM_AUTH_STYLE：``api-key``（默认，MiMo）| ``bearer``（多数 OpenAI 兼容服务）
    - LLM_DISABLE_THINKING：``true``（默认，MiMo 推理模型须关思考保结构化输出）| ``false``

    MiMo docs: https://platform.xiaomimimo.com/docs/zh-CN/quick-start/first-api-call
    """
    _DEFAULT_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1/chat/completions"

    def __init__(self, api_key: str, base_url: str = "",
                 auth_style: str = "api-key", disable_thinking: bool = True,
                 token_param: str = "max_completion_tokens", thinking_style: str = "mimo",
                 embed_url: str = "", embed_model: str = "", embed_api_key: str = "",
                 embed_auth_style: str = "bearer", embed_dimensions: int = 0):
        self.api_key = api_key
        self.base_url = base_url or self._DEFAULT_BASE_URL
        self.auth_style = (auth_style or "api-key").lower()
        self.disable_thinking = disable_thinking
        # per-provider 差异（多 LLM 源）：
        #   token_param   —— token 上限字段名：max_completion_tokens（MiMo/MiniMax）| max_tokens（DeepSeek/Qwen）
        #   thinking_style —— 关思考的方式：
        #     "mimo" → thinking:{type:disabled}（含 MiniMax，同款）；开思考不发键（原生 adaptive）
        #     "qwen" → enable_thinking:false/true（DashScope 兼容模式 qwen3）
        #     "none" → 不发任何思考键（DeepSeek 等默认非思考服务商）
        self.token_param = (token_param or "max_completion_tokens").strip()
        self.thinking_style = (thinking_style or "mimo").strip().lower()
        # 向量化（embedding）端点/鉴权/维度独立于 chat——embedding 常用另一服务商（如百炼）。
        # 默认从 chat 端点推导；embed_api_key 缺省回退 chat key；auth 默认 bearer（OpenAI 风格）。
        self.embed_url = embed_url or self.base_url.replace("/chat/completions", "/embeddings")
        self.embed_model = embed_model
        self.embed_api_key = embed_api_key or api_key
        self.embed_auth_style = (embed_auth_style or "bearer").lower()
        self.embed_dimensions = int(embed_dimensions or 0)
        self._client: httpx.AsyncClient | None = None  # 复用的出站连接池（懒建，绑定运行 loop）

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(limits=_HTTP_LIMITS)
        return self._client

    def _embed_headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.embed_auth_style == "api-key":
            h["api-key"] = self.embed_api_key
        else:
            h["Authorization"] = f"Bearer {self.embed_api_key}"
        return h

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.auth_style == "bearer":
            h["Authorization"] = f"Bearer {self.api_key}"
        else:  # 默认 MiMo 风格
            h["api-key"] = self.api_key
        return h

    def _resolve_thinking(self, thinking) -> bool:
        """本次调用是否关思考：thinking=None 用构造默认；True/False 覆盖本次。"""
        return self.disable_thinking if thinking is None else (not thinking)

    def _build_body(self, messages, model, temperature, max_tokens, thinking, stream: bool) -> dict:
        """按 per-provider 差异（token_param/thinking_style）构造 chat/completions 请求体。"""
        disable = self._resolve_thinking(thinking)
        # 开思考时给足 token：reasoning 占预算，content 容易被饿空/截断；下限抬到 2048。
        max_out = (max_tokens or 512) if disable else max((max_tokens or 512), 2048)
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            self.token_param: max_out,
            "stream": stream,
        }
        if self.thinking_style == "mimo":
            # MiMo/MiniMax 等推理模型：默认把 token 预算几乎全花在 reasoning_content 上，导致
            # 结构化任务（Planner JSON、聚合改写、接地合成）的 content 被饿成空/截断——关思考拿干净、
            # 确定、低延迟 content。开思考时不发本键（回原生思考态），reasoning_content 留服务端不下发。
            if disable:
                body["thinking"] = {"type": "disabled"}
        elif self.thinking_style == "qwen":
            # DashScope 兼容模式 qwen3：思考经 enable_thinking 显式控制（结构化任务须置 false）。
            body["enable_thinking"] = not disable
        # thinking_style == "none"（DeepSeek 等）：不发思考键，用服务商默认。
        return body

    async def _post_chat(self, body, timeout_s) -> dict:
        """非流式 chat/completions POST + 错误结构化（complete/complete_tools 共用）。
        4xx/5xx 的真实拒因在响应体里（如 MiniMax 422 只有 body 说得清是参数还是内容问题），
        raise_for_status 的异常文本不含 body——截断入异常，网关日志/obs.llm error 直接可诊断
        （badcase 6d29929e：422 秒拒两次，只留状态码，根因无从判定）。"""
        resp = await self._get_client().post(
            self.base_url, headers=self._headers(), json=body,
            timeout=_http_timeout(timeout_s, _HTTP_READ_CAP_S))
        if resp.status_code >= 400:
            snippet = (resp.text or "")[:300].replace("\n", " ")
            raise ProviderHTTPError(resp.status_code, snippet, _retry_after_s(resp))
        return resp.json()

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        body = self._build_body(messages, model, temperature, max_tokens, thinking, stream=False)
        data = await self._post_chat(body, timeout_s)

        content = strip_think_block(data["choices"][0]["message"]["content"] or "")
        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        return content, model, "stop", (prompt_tokens, completion_tokens)

    async def complete_tools(self, messages, model, temperature, max_tokens,
                             tools=None, tool_choice=None, thinking=None, timeout_s=None):
        """带 tools 的补全（M1a）。tools/tool_choice 为 OpenAI 线格式原样注入
        （四家 OpenAI 兼容直通，RFC §2/§3.1）；响应 message.tool_calls 经
        normalize_tool_calls 归一化（arguments string→object）。tool call 场景
        content 常为空/None，与 tool_calls 并行返回，取舍交调用方。

        finish_reason 只透传不判断：qwen（DashScope 兼容模式）出 tool_calls 时
        finish_reason 仍是 "stop" 而非 "tool_calls"（2026-07-24 真栈探针实测，
        其余三家标准）——是否工具调用一律按 tool_calls 置位判断，勿按 finish 分支。"""
        if not tools:
            return await super().complete_tools(
                messages, model, temperature, max_tokens,
                thinking=thinking, timeout_s=timeout_s)
        body = self._build_body(messages, model, temperature, max_tokens, thinking, stream=False)
        body["tools"] = list(tools)
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        data = await self._post_chat(body, timeout_s)

        choice = data["choices"][0]
        msg = choice.get("message") or {}
        content = strip_think_block(msg.get("content") or "")
        tool_calls = normalize_tool_calls(msg.get("tool_calls"))
        finish = choice.get("finish_reason") or "stop"
        usage = data.get("usage", {})
        return (content, model, finish,
                (usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
                tool_calls)

    async def stream(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        body = self._build_body(messages, model, temperature, max_tokens, thinking, stream=True)
        # 流式：read 超时作 per-chunk stall 检测（无新 chunk 超时即中止），不让上游卡死吊死整链。
        stall = _read_budget(timeout_s, _STREAM_STALL_S)
        stripper = ThinkStreamStripper()   # 头部 <think> 内联剥离（MiniMax 开思考泄漏，见下方注释）
        async with self._get_client().stream(
                "POST", self.base_url, headers=self._headers(), json=body,
                timeout=httpx.Timeout(stall, connect=_HTTP_CONNECT_S, pool=5.0)) as resp:
            if resp.status_code >= 400:   # 同 complete()：把响应体带进异常，拒因可诊断
                raw = await resp.aread()
                snippet = raw[:300].decode("utf-8", "replace").replace("\n", " ")
                raise ProviderHTTPError(resp.status_code, snippet, _retry_after_s(resp))
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                    delta = chunk["choices"][0].get("delta", {})
                    # 只取 content；reasoning_content（思考增量）刻意丢弃，不下发给用户。
                    text = delta.get("content", "")
                    if text:
                        out = stripper.feed(text)
                        if out:
                            yield out
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
        tail = stripper.flush()
        if tail:
            yield tail

    async def embed(self, texts, model="", timeout_s=None):
        """OpenAI 兼容 /embeddings（百炼 text-embedding-v4 等）。返回 list[list[float]]。"""
        body = {"model": model or self.embed_model or "text-embedding-v4",
                "input": list(texts)}
        if self.embed_dimensions:  # v3/v4 支持指定输出维度（须与 memory EMBED_DIM 一致）
            body["dimensions"] = self.embed_dimensions
            body["encoding_format"] = "float"
        resp = await self._get_client().post(
            self.embed_url, headers=self._embed_headers(), json=body,
            timeout=_http_timeout(timeout_s, _EMBED_READ_CAP_S))
        resp.raise_for_status()
        data = resp.json()
        items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        return [list(d["embedding"]) for d in items]


# 向后兼容别名（历史代码/测试可能引用 MiMoProvider）
MiMoProvider = OpenAICompatibleProvider


def build_provider() -> BaseProvider:
    """按 env 装配 LLM provider。换服务商只改 env：

    - LLM_PROVIDER：``anthropic`` 走 Claude SDK；其余（xiaomimimo/mimo/openai/deepseek/
      qwen/自建…）一律走 OpenAI 兼容 HTTP provider，端点/鉴权/思考开关见下。
    - LLM_API_KEY / LLM_BASE_URL / LLM_AUTH_STYLE / LLM_DISABLE_THINKING
    无 key → MockProvider（PoC 可离线跑通）。
    """
    provider = os.getenv("LLM_PROVIDER", "xiaomimimo").lower()
    api_key = os.getenv("LLM_API_KEY", "")

    if not api_key:
        print(f"[llm-gateway] provider={provider}, no API key -> MockProvider", flush=True)
        return MockProvider()

    if provider == "anthropic":
        return AnthropicProvider(api_key)

    # 其余一律 OpenAI 兼容：端点/鉴权/思考开关经 env 注入，新增服务商无需改代码
    return OpenAICompatibleProvider(
        api_key,
        base_url=os.getenv("LLM_BASE_URL", ""),
        auth_style=os.getenv("LLM_AUTH_STYLE", "api-key"),
        disable_thinking=os.getenv("LLM_DISABLE_THINKING", "true").lower() != "false",
        embed_url=os.getenv("LLM_EMBED_URL", ""),
        embed_model=os.getenv("LLM_EMBED_MODEL", ""),
        embed_api_key=os.getenv("LLM_EMBED_API_KEY", ""),
        embed_auth_style=os.getenv("LLM_EMBED_AUTH_STYLE", "bearer"),
        embed_dimensions=int(os.getenv("LLM_EMBED_DIMENSIONS", "0") or 0),
    )
