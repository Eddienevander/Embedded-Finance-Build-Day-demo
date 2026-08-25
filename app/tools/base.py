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


def build_registry(db: Database, mock: bool, real_integrations: bool = False) -> ToolRegistry:
    """`mock` picks the overall tool set (scripted-demo vs "every adapter real").
    `real_integrations` is a narrower, independent switch: swap in whichever
    adapters are genuinely wired to a live API today — currently just Zwapgrid
    for `payment_history` — regardless of `mock`, without needing every other
    *_real.py stub finished. Add Open Payments here the same way once it's
    actually implemented."""
    from app.tools.account_registry import MockAccountRegistryTool
    from app.tools.bankgirot import MockBankgirotTool
    from app.tools.bolagsverket import BolagsverketRealTool, MockBolagsverketTool
    from app.tools.invoice_archive import InvoiceArchiveTool
    from app.tools.payment_history import MockPaymentHistoryTool
    from app.tools.web_intel import MockWebIntelTool, WebIntelRealTool
    from app.tools.zwapgrid_real import ZwapgridPaymentHistoryTool

    payment_history = (
        ZwapgridPaymentHistoryTool() if real_integrations else MockPaymentHistoryTool(db)
    )

    if mock:
        return ToolRegistry([
            MockBolagsverketTool(),
            payment_history,
            MockAccountRegistryTool(),  # MOCK ONLY — this API doesn't exist (that's the pitch)
            MockWebIntelTool(),
            InvoiceArchiveTool(db),  # always real: queries our own SQLite
            MockBankgirotTool(),  # mod-10 validation is real; owner lookup mocked
        ])
    return ToolRegistry([
        BolagsverketRealTool(),
        payment_history,
        MockAccountRegistryTool(),  # no real implementation exists — see README
        WebIntelRealTool(),
        InvoiceArchiveTool(db),
        MockBankgirotTool(),  # real adapter = parse the public search (see bankgirot.py)
    ])
