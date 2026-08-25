"""Shared Anthropic client, tool-use loop and heartbeat hooks for all agents."""

import json
import re
from typing import TypeVar

from anthropic import AsyncAnthropic
from pydantic import BaseModel

from app import config
from app.bus import bus
from app.models import AgentName, AgentState, Evidence
from app.schemas import strict_schema
from app.tools.base import ToolRegistry

M = TypeVar("M", bound=BaseModel)

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


_JSON_NOISE = re.compile(
    r'[{}\[\]"]|\b(points|strongest_point|decision|confidence|key_evidence|reasoning|'
    r'recommended_action|evidence|tool|query|finding|supports)\b\s*:'
)


def humanize(text: str) -> str:
    """Structured output streams as JSON; strip the syntax so the heartbeat
    ticker reads as prose on the projector."""
    return " ".join(_JSON_NOISE.sub(" ", text).split())


def output_format(model: type[BaseModel]) -> dict:
    return {"format": {"type": "json_schema", "schema": strict_schema(model)}}


# Sampling params were removed on the 4.7+/5 generation: sending `temperature`
# there is a hard 400 ("deprecated for this model"). Stance divergence between
# the debaters comes from their system prompts; temperature is a bonus where the
# model still takes it (e.g. claude-sonnet-4-6).
_NO_TEMPERATURE = ("opus-4-7", "opus-4-8", "opus-5", "sonnet-5", "fable-5", "mythos-5")


def sampling_body(temperature: float) -> dict:
    if any(tag in config.MODEL for tag in _NO_TEMPERATURE):
        return {}
    return {"temperature": temperature}


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
    if tool == "bankgirot":
        if not raw.get("is_bankgiro"):
            return "Not a bankgiro number — Bankgirot cannot verify it"
        if not raw.get("check_digit_valid"):
            return "INVALID check digit — never issued by Bankgirot"
        owner = raw.get("owner_name")
        return (f"Valid bankgiro; owner: {owner} ({raw.get('owner_orgnr')})" if owner
                else "Valid check digit; no owner found in the public search")
    if tool == "web_intel":
        return raw.get("summary", "web intel result")
    if tool == "invoice_archive":
        return (f"{raw.get('invoice_count_on_file', 0)} settled invoices on file; returned last "
                f"{len(raw.get('last_5_invoices', []))} for comparison")
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
        output_model: type[M],
        max_iterations: int = 6,
    ) -> M:
        """Anthropic tool-use protocol: send -> execute requested tools ->
        append tool_result blocks -> repeat, emitting heartbeats at every step.
        The final turn is schema-constrained, so it parses without repair."""
        client = get_client()
        for _ in range(max_iterations):
            await self.beat("thinking", "Reasoning over the case…")
            response = await client.messages.create(
                model=config.MODEL,
                max_tokens=config.LLM_MAX_TOKENS,
                system=system,
                messages=messages,
                tools=registry.to_anthropic_schema(),
                output_config=output_format(output_model),
                extra_body=sampling_body(self.temperature),
            )

            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if b.type == "text")
                return output_model.model_validate_json(text)

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

    async def stream_structured(
        self,
        system: str,
        messages: list[dict],
        output_model: type[M],
        state: AgentState = "streaming",
    ) -> M:
        """Streamed, schema-constrained completion; emits a heartbeat every ~15
        accumulated deltas with the tail of the text, so the audience sees the
        argument being written."""
        client = get_client()
        parts: list[str] = []
        chars_since_beat = 0
        async with client.messages.stream(
            model=config.MODEL,
            max_tokens=config.LLM_MAX_TOKENS,
            system=system,
            messages=messages,
            output_config=output_format(output_model),
            extra_body=sampling_body(self.temperature),
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    parts.append(event.delta.text)
                    chars_since_beat += len(event.delta.text)
                    # Count characters, not deltas: chunk size varies wildly by
                    # model, and the debater cards must visibly tick either way.
                    if chars_since_beat >= 60:
                        chars_since_beat = 0
                        readable = humanize("".join(parts))
                        await self.beat("streaming", readable[-60:],
                                        payload={"text": readable[-1200:], "stream_state": state})
            final = await stream.get_final_message()
        text = "".join(b.text for b in final.content if b.type == "text") or "".join(parts)
        return output_model.model_validate_json(text)
