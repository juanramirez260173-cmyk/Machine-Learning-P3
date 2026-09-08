from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from ..config import Settings
from ..llm import LLMProvider, load_prompt
from ..models import AgentRun, Case, Evidence
from ..store import Store


@dataclass
class AgentContext:
    case: Case
    evidence: List[Evidence]
    settings: Settings
    extra: Dict[str, Any]


class BaseAgent:
    name = "base"
    prompt_name: Optional[str] = None

    def __init__(self, store: Store, settings: Settings, provider: LLMProvider):
        self.store = store
        self.settings = settings
        self.provider = provider

    # ---------------------------------------------------------------- prompts
    def system_prompt(self) -> str:
        return load_prompt("system_base")

    def task_prompt(self) -> str:
        return load_prompt(self.prompt_name) if self.prompt_name else ""

    @staticmethod
    def evidence_register_text(evidence: Sequence[Evidence]) -> str:
        lines = []
        for e in evidence:
            lines.append(json.dumps({
                "evidence_id": e.evidence_id, "type": e.evidence_type.value, "title": e.title,
                "date": e.created_date.isoformat(), "owner": e.owner, "revision": e.revision,
                "validation_state": e.validation_state.value, "excerpt": e.excerpt, "structured": e.structured,
            }, ensure_ascii=False, default=str))
        return "\n".join(lines)

    @staticmethod
    def case_text(case: Case) -> str:
        return case.model_dump_json(indent=None, exclude={"created_at", "updated_at"})

    def user_prompt(self, ctx: AgentContext, extra_sections: Optional[Dict[str, str]] = None) -> str:
        parts = [self.task_prompt(), "\n## Case\n" + self.case_text(ctx.case),
                 "\n## Evidence register (cite by evidence_id)\n" + self.evidence_register_text(ctx.evidence)]
        for title, body in (extra_sections or {}).items():
            parts.append(f"\n## {title}\n{body}")
        return "\n".join(parts)

    # ------------------------------------------------------------------ trace
    def record_run(self, ctx: AgentContext, output_ids: List[str], tools: Optional[List[str]] = None) -> AgentRun:
        run = self.provider.last_run or AgentRun(case_id=ctx.case.case_id, agent=self.name, model="deterministic",
                                                 prompt_version=self.provider.prompt_version)
        run.agent = self.name
        run.case_id = ctx.case.case_id
        run.tools = tools or []
        run.source_evidence_ids = [e.evidence_id for e in ctx.evidence]
        run.output_object_ids = output_ids
        self.store.put(run, actor=self.name, action="agent_run")
        self.provider.last_run = None
        return run
