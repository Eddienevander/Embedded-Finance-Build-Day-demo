"""EvidenceTool ABC + registry. Every external data source sits behind this
interface; MOCK_MODE picks mock vs real implementations at startup."""

from abc import ABC, abstractmethod

from pydantic import BaseModel

from app.db import Database


class EvidenceTool(ABC):
    name: str
    description: str
    input_model: type[BaseModel]

    @abstractmethod
    async def lookup(self, **kwargs) -> dict: ...

    def to_anthropic_tool(self) -> dict:
        schema = self.input_model.model_json_schema()
        schema["additionalProperties"] = False
        return {"name": self.name, "description": self.description, "input_schema": schema}


class ToolRegistry:
    def __init__(self, tools: list[EvidenceTool]) -> None:
        self._tools = {t.name: t for t in tools}

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def to_anthropic_schema(self) -> list[dict]:
        return [t.to_anthropic_tool() for t in self._tools.values()]

    async def execute(self, name: str, raw_input: dict) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"unknown tool: {name}")
        validated = tool.input_model.model_validate(raw_input)
        return await tool.lookup(**validated.model_dump())


def build_registry(db: Database, mock: bool) -> ToolRegistry:
    from app.tools.account_registry import MockAccountRegistryTool
    from app.tools.bolagsverket import BolagsverketRealTool, MockBolagsverketTool
    from app.tools.invoice_archive import InvoiceArchiveTool
    from app.tools.payment_history import MockPaymentHistoryTool, OpenPaymentsRealTool
    from app.tools.web_intel import MockWebIntelTool, WebIntelRealTool

    if mock:
        return ToolRegistry([
            MockBolagsverketTool(),
            MockPaymentHistoryTool(db),
            MockAccountRegistryTool(),  # MOCK ONLY — this API doesn't exist (that's the pitch)
            MockWebIntelTool(),
            InvoiceArchiveTool(db),  # always real: queries our own SQLite
        ])
    return ToolRegistry([
        BolagsverketRealTool(),
        OpenPaymentsRealTool(),
        MockAccountRegistryTool(),  # no real implementation exists — see README
        WebIntelRealTool(),
        InvoiceArchiveTool(db),
    ])
