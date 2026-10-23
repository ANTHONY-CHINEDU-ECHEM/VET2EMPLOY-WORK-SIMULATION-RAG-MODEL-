"""Structured output contracts returned by the AI Manager.

Every factual item carries ``evidence_ids`` so that verification can check each claim
against the exact passages it relies on. These models are also the public response
schema of the API, so field names are stable and documented.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CompetencyName = Literal[
    "Technical Quality", "Problem Solving", "Communication", "Delivery", "Professionalism"
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class GroundedItem(_Model):
    text: str = Field(min_length=1, max_length=1200, description="One self contained statement.")
    evidence_ids: list[str] = Field(
        default_factory=list, description="Evidence entries that state this item."
    )


class TaskDeliverable(_Model):
    reference: str | None = Field(
        default=None, description="Deliverable reference from the brief, such as D2."
    )
    name: str = Field(min_length=1, max_length=200)
    format: str | None = Field(default=None, max_length=200)
    file_name: str | None = Field(
        default=None, max_length=200, description="Exactly as the brief specifies."
    )
    due: str | None = Field(default=None, max_length=100)
    acceptance_criteria: list[GroundedItem] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class CompetencyFocus(_Model):
    competency: CompetencyName
    weight_percent: int | None = Field(default=None, ge=0, le=100)
    why: str = Field(min_length=1, max_length=600)
    evidence_ids: list[str] = Field(default_factory=list)


class TaskSchedule(_Model):
    assigned_on: str | None = Field(default=None, max_length=60)
    due: str | None = Field(default=None, max_length=100)
    estimated_effort_hours: float | None = Field(default=None, ge=0, le=80)
    evidence_ids: list[str] = Field(default_factory=list)


class GeneratedTask(_Model):
    title: str = Field(min_length=3, max_length=160)
    sender_role: str = Field(
        min_length=2, max_length=80, description="Role of the persona sending the message."
    )
    priority: Literal["P0", "P1", "P2"]
    message: str = Field(
        min_length=20,
        max_length=2500,
        description="The in character message the candidate receives.",
    )
    summary: GroundedItem
    context: list[GroundedItem] = Field(default_factory=list)
    objectives: list[GroundedItem] = Field(min_length=1)
    steps: list[GroundedItem] = Field(min_length=1)
    deliverables: list[TaskDeliverable] = Field(default_factory=list)
    tools_and_technologies: list[GroundedItem] = Field(default_factory=list)
    collaboration: list[GroundedItem] = Field(default_factory=list)
    complications: list[GroundedItem] = Field(default_factory=list)
    competencies: list[CompetencyFocus] = Field(default_factory=list)
    schedule: TaskSchedule
    open_questions: list[str] = Field(default_factory=list)


ReplyIntent = Literal["answer", "clarify", "redirect", "feedback", "acknowledge", "escalate"]


class ManagerReply(_Model):
    reply: str = Field(min_length=1, max_length=2500)
    intent: ReplyIntent
    claims: list[GroundedItem] = Field(default_factory=list)
    needs_human_review: bool = False
    review_reason: str | None = Field(default=None, max_length=500)


JudgeVerdictLabel = Literal["SUPPORTED", "PARTIAL", "UNSUPPORTED", "NOT_A_CLAIM"]


class JudgeVerdict(_Model):
    claim_id: str
    verdict: JudgeVerdictLabel
    unsupported_details: list[str] = Field(default_factory=list)
    rationale: str = ""


class JudgeOutput(_Model):
    verdicts: list[JudgeVerdict]
