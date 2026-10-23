"""Request models for the generation, retrieval and verification operations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from v2e_rag.domain.taxonomy import (
    TRACK_ROLE_FAMILY,
    JobLevel,
    RoleFamily,
    Sector,
    TaskType,
    TrackCode,
)

Weekday = Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TaskRequest(_Strict):
    """Ask the AI Manager to issue one grounded work item to a candidate."""

    track: TrackCode | None = Field(
        default=None,
        description="Week one track code from the Work Brief: DA, SE, CY or DE.",
    )
    role_family: RoleFamily | None = Field(
        default=None, description="Labour market role family. Derived from the track when omitted."
    )
    job_level: JobLevel | None = Field(
        default=None, description="Seniority used to select market evidence."
    )
    sector: Sector | None = Field(
        default=None, description="Market sector used to select market evidence."
    )
    task_type: TaskType = TaskType.CORE_ACTIVITY
    activity_number: int | None = Field(
        default=None,
        ge=1,
        le=30,
        description="Activity number from the track brief (sequential activities).",
    )
    simulation_day: Weekday | None = None
    focus: str | None = Field(
        default=None, max_length=600, description="Optional emphasis for the task."
    )
    region: str = Field(default="UK", max_length=8)
    candidate_first_name: str | None = Field(default=None, max_length=60)
    manager_name: str | None = Field(
        default=None,
        max_length=80,
        description="Persona name supplied by the platform. Never invented.",
    )
    previous_task_titles: list[str] = Field(default_factory=list, max_length=50)
    include_market_evidence: bool = True

    @model_validator(mode="after")
    def _resolve(self) -> TaskRequest:
        if self.track is None and self.role_family is None:
            raise ValueError("Provide a track or a role_family.")
        if self.track is not None:
            expected = TRACK_ROLE_FAMILY[self.track]
            if self.role_family is None:
                self.role_family = expected
            elif self.role_family != expected:
                raise ValueError(
                    f"role_family {self.role_family.value} does not match track {self.track.value} "
                    f"({expected.value})."
                )
        if self.activity_number is not None and self.track is None:
            raise ValueError(
                "activity_number requires a track because activities are defined per track."
            )
        return self


class ConversationTurn(_Strict):
    role: Literal["candidate", "manager"]
    content: str = Field(max_length=8000)


class ManagerReplyRequest(_Strict):
    """Ask the AI Manager to reply in character to a candidate message."""

    track: TrackCode | None = None
    role_family: RoleFamily | None = None
    job_level: JobLevel | None = None
    sector: Sector | None = None
    candidate_message: str = Field(min_length=1, max_length=8000)
    conversation: list[ConversationTurn] = Field(default_factory=list, max_length=40)
    current_task_title: str | None = Field(default=None, max_length=300)
    current_task_summary: str | None = Field(default=None, max_length=3000)
    simulation_day: Weekday | None = None
    candidate_first_name: str | None = Field(default=None, max_length=60)
    manager_name: str | None = Field(default=None, max_length=80)
    region: str = Field(default="UK", max_length=8)

    @model_validator(mode="after")
    def _resolve(self) -> ManagerReplyRequest:
        if self.track is None and self.role_family is None:
            raise ValueError("Provide a track or a role_family.")
        if self.track is not None and self.role_family is None:
            self.role_family = TRACK_ROLE_FAMILY[self.track]
        return self


class RetrieveRequest(_Strict):
    """Retrieval only: returns an evidence pack and a grounding prompt for external use."""

    query: str = Field(min_length=2, max_length=2000)
    track: TrackCode | None = None
    role_family: RoleFamily | None = None
    job_level: JobLevel | None = None
    sector: Sector | None = None
    source_types: list[str] = Field(default_factory=list)
    facets: list[str] = Field(default_factory=list)
    limit: int = Field(default=8, ge=1, le=30)
    include_engineering_reference: bool = False

    @model_validator(mode="after")
    def _resolve(self) -> RetrieveRequest:
        if self.track is not None and self.role_family is None:
            self.role_family = TRACK_ROLE_FAMILY[self.track]
        return self


class VerifyRequest(_Strict):
    """Verify text produced elsewhere against knowledge base chunks."""

    text: str = Field(min_length=1, max_length=20000)
    chunk_ids: list[str] = Field(min_length=1, max_length=40)
    use_entailment: bool = True
