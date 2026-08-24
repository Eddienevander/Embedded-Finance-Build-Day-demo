"""Shared Anthropic client, tool-use loop and heartbeat hooks for all agents."""

import json
from typing import Any

from anthropic import AsyncAnthropic

from app import config
from app.bus import bus
from app.models import AgentName, AgentState, Evidence
from app.tools.base import ToolRegistry

_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        # Hard timeout + one retry: a hung LLM call must never freeze the demo.
        _client = AsyncAnthropic(timeout=config.LLM_TIMEOUT_SECONDS, max_retries=1)
    return _client


def truncate(text: str, n: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def extract_json(text: str) -> Any:
    """Parse the first JSON value found in a model response, stripping code
    fences defensively."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    decoder = json.JSONDecoder()
    for i, ch in enumerate(t):
        if ch in "[{":
            try:
                obj, _ = decoder.raw_decode(t[i:])
                return obj
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON found in model output: {truncate(text, 200)}")


def summarize_finding(tool: str, raw: dict) -> str:
    """One human line per tool result, for the tool_result heartbeat."""
    if tool == "bolagsverket":
        if raw.get("found"):
            return f"{raw.get('name')}: {raw.get('status')}, registered {raw.get('registration_date')}"
        return f"orgnr {raw.get('orgnr')}: NOT FOUND at Bolagsverket"
    if tool == "payment_history":
        accounts = ", ".join(f"{a['account']}: {a['payments']} payments"
                             for a in raw.get("accounts", [])[:3])
        return f"{raw.get('payment_count', 0)} payments on file. {accounts}"
    if tool == "account_registry":
        if not raw.get("found"):
            return "Account not present in ownership registry"
        flags = ", ".join(raw.get("flags", [])) or "no flags"
        return (f"Owner: {raw.get('owner_name')} ({raw.get('owner_type')}), "
                f"age {raw.get('account_age_days')} days, {flags}")
    if tool == "web_intel":
        return raw.get("summary", "web intel result")
    if tool == "invoice_archive":
        return f"{raw.get('invoice_count_on_file', 0)} invoices on file; returned last {len(raw.get('last_5_invoices', []))} for comparison"
    return truncate(json.dumps(raw), 110)


class BaseAgent:
    agent_name: AgentName = "system"
    temperature: float = 0.2

    def __init__(self, case_id: str) -> None:
        self.case_id = case_id

    async def beat(self, state: AgentState, detail: str, payload: dict | None = None) -> None:
        await bus.emit(self.case_id, self.agent_name, state, detail, payload)

    async def announce(self) -> None:
        await self.beat("spawned", f"{self.agent_name} spawned for {self.case_id}")

    async def run_tool_loop(
        self,
        system: str,
        messages: list[dict],
        registry: ToolRegistry,
        max_iterations: int = 6,
    ) -> str:
        """Anthropic tool-use protocol: send -> execute requested tools ->
        append tool_result blocks -> repeat, emitting heartbeats at every step."""
        client = get_client()
        for _ in range(max_iterations):
            await self.beat("thinking", "Reasoning over the case…")
            response = await client.messages.create(
                model=config.MODEL,
                max_tokens=config.LLM_MAX_TOKENS,
                system=system,
                messages=messages,
                tools=registry.to_anthropic_schema(),
                # anthropic>=1.0 dropped the typed temperature kwarg (removed on
                # 4.7+/5 models); claude-sonnet-4-6 still accepts it on the wire.
                extra_body={"temperature": self.temperature},
            )

            if response.stop_reason != "tool_use":
                return "".join(b.text for b in response.content if b.type == "text")

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                args = dict(block.input)
                await self.beat(
                    "tool_call",
                    f"Calling {block.name}({truncate(json.dumps(args, ensure_ascii=False), 90)})",
                )
                try:
                    raw = await registry.execute(block.name, args)
                    finding = summarize_finding(block.name, raw)
                    provisional = Evidence(
                        tool=block.name,
                        query=truncate(json.dumps(args, ensure_ascii=False), 110),
                        finding=finding, supports="neutral", confidence=0.5, raw=None,
                    )
                    await self.beat("tool_result", f"{block.name} → {finding}",
                                    payload={"evidence": provisional.model_dump(mode="json"),
                                             "provisional": True})
                    content = json.dumps(raw, ensure_ascii=False)
                    is_error = False
                except Exception as e:  # tool failure feeds back to the model, not a crash
                    content = f"Tool error: {type(e).__name__}: {e}"
                    await self.beat("tool_result", f"{block.name} → ERROR: {truncate(str(e), 90)}")
                    is_error = True
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": content, "is_error": is_error,
                })
            messages.append({"role": "user", "content": tool_results})

        raise RuntimeError(f"{self.agent_name}: tool loop exceeded {max_iterations} iterations")

    async def stream_text(
        self,
        system: str,
        messages: list[dict],
        state: AgentState = "streaming",
    ) -> str:
        """Streamed completion; emits a heartbeat every ~15 accumulated tokens
        with the tail of the text, so the audience sees it being written."""
        client = get_client()
        parts: list[str] = []
        deltas_since_beat = 0
        async with client.messages.stream(
            model=config.MODEL,
            max_tokens=config.LLM_MAX_TOKENS,
            system=system,
            messages=messages,
            extra_body={"temperature": self.temperature},
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    parts.append(event.delta.text)
                    deltas_since_beat += 1
                    if deltas_since_beat >= 15:
                        deltas_since_beat = 0
                        text = "".join(parts)
                        await self.beat("streaming", text[-60:],
                                        payload={"text": text[-1200:], "stream_state": state})
            final = await stream.get_final_message()
        text = "".join(b.text for b in final.content if b.type == "text") or "".join(parts)
        return text
