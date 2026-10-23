"""Retrieval planning.

A single similarity search cannot reliably gather everything a realistic task needs.
The planner decomposes each request into purpose specific steps, one per kind of evidence
(the activity definition, the deliverable and its due point, the client context, the
supervisor's voice, real employer responsibilities, tools, collaboration patterns and brand
voice). Each step has its own query, metadata filters and, for market evidence, a ladder of
progressively relaxed filters so that a narrow request (for example an expert role in the
education sector) degrades gracefully instead of returning nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from v2e_rag.domain.catalog import Catalog
from v2e_rag.domain.taxonomy import Facet, SourceType, TaskType
from v2e_rag.generation.requests import ManagerReplyRequest, RetrieveRequest, TaskRequest
from v2e_rag.store.base import SearchFilters

_TASK_TYPE_HINTS: dict[TaskType, str] = {
    TaskType.CORE_ACTIVITY: "objective ordered steps completion criteria deliverable",
    TaskType.COLLEAGUE_REQUEST: "peer reviewer colleague asks for input feedback collaboration",
    TaskType.STAKEHOLDER_UPDATE: "client persona stakeholder update clarification questions priorities",
    TaskType.COMPLICATION: "deliberate complication inconsistent data ambiguous requirement stakeholder changes mind",
}


@dataclass
class RetrievalStep:
    name: str
    purpose: str
    query: str
    filters: SearchFilters
    limit: int
    required: bool = False
    min_hits: int = 1
    relaxations: list[SearchFilters] = field(default_factory=list)
    apply_similarity_threshold: bool = False


@dataclass
class RetrievalPlan:
    operation: str
    steps: list[RetrievalStep]
    notes: list[str] = field(default_factory=list)


def _brief(track: str, facets: list[Facet], activity: int | None = None) -> SearchFilters:
    return SearchFilters(
        source_types=(SourceType.PROGRAMME_BRIEF.value,),
        tracks=(track,),
        facets=tuple(f.value for f in facets),
        activity_numbers=(activity,) if activity else (),
    )


def _market_ladder(
    role_family: str,
    facets: list[Facet],
    level: str | None,
    sector: str | None,
    source: SourceType,
) -> tuple[SearchFilters, list[SearchFilters]]:
    def filters(with_level: bool, with_sector: bool) -> SearchFilters:
        return SearchFilters(
            source_types=(source.value,),
            role_families=(role_family,),
            facets=tuple(f.value for f in facets),
            job_levels=(level,) if level and with_level else (),
            sectors=(sector,) if sector and with_sector else (),
        )

    strict = filters(with_level=True, with_sector=True)
    ladder: list[SearchFilters] = []
    if sector:
        ladder.append(filters(with_level=True, with_sector=False))
    if level:
        ladder.append(filters(with_level=False, with_sector=False))
    return strict, ladder


class RetrievalPlanner:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def plan_task(self, request: TaskRequest) -> RetrievalPlan:
        steps: list[RetrievalStep] = []
        notes: list[str] = []
        hint = _TASK_TYPE_HINTS[request.task_type]
        focus = request.focus or ""
        role_family = request.role_family.value if request.role_family else ""
        level = request.job_level.value if request.job_level else None
        sector = request.sector.value if request.sector else None

        if request.track is not None:
            track = self.catalog.track(request.track)
            code = track.code.value
            keywords = " ".join(track.retrieval_keywords)
            activity = f"Activity {request.activity_number}" if request.activity_number else ""
            topic = " ".join(p for p in (activity, focus) if p) or keywords
            steps += [
                RetrievalStep(
                    name="activity_definition",
                    purpose="What the candidate must do: objective, ordered steps and completion criteria",
                    query=f"{activity} {track.name} {focus} {hint}".strip(),
                    filters=_brief(code, [Facet.TRACK_ACTIVITY], request.activity_number),
                    limit=3 if request.activity_number else 4,
                    required=True,
                ),
                RetrievalStep(
                    name="deliverables",
                    purpose="Deliverable reference, file name, format, due point and acceptance criteria",
                    query=f"required deliverables file name due acceptance criteria {topic}",
                    filters=_brief(code, [Facet.TRACK_DELIVERABLES]),
                    limit=2,
                    required=request.task_type == TaskType.CORE_ACTIVITY,
                ),
                RetrievalStep(
                    name="client_context",
                    purpose="Fictitious client, stakeholders, the problem and known data or system issues",
                    query=f"client stakeholders problem constraints working materials known issues {topic}",
                    filters=_brief(code, [Facet.TRACK_CLIENT, Facet.TRACK_MATERIALS]),
                    limit=4,
                    required=True,
                ),
                RetrievalStep(
                    name="schedule",
                    purpose="When the activity happens and how much effort is planned",
                    query=f"week at a glance {activity} effort when",
                    filters=_brief(code, [Facet.TRACK_SCHEDULE]),
                    limit=1,
                ),
                RetrievalStep(
                    name="supervisor_voice",
                    purpose="How the track supervisor speaks and what they value",
                    query="welcome note from your track supervisor what I will be looking for",
                    filters=_brief(code, [Facet.SUPERVISOR_VOICE]),
                    limit=1,
                ),
                RetrievalStep(
                    name="rubric",
                    purpose="What good looks like for assessment",
                    query=f"assessment rubric criteria weight {topic}",
                    filters=_brief(code, [Facet.TRACK_RUBRIC]),
                    limit=1,
                ),
            ]
            if request.task_type == TaskType.COMPLICATION:
                steps.append(
                    RetrievalStep(
                        name="planted_issues",
                        purpose="Known weaknesses and deliberate complications the brief already describes",
                        query=f"known or suspected issues weakness supervisor hint inconsistent {topic}",
                        filters=_brief(
                            code, [Facet.TRACK_MATERIALS, Facet.TRACK_CLIENT, Facet.TRACK_ACTIVITY]
                        ),
                        limit=3,
                        required=True,
                    )
                )
        else:
            notes.append(
                "No Work Brief track was supplied, so the task is grounded in programme rules and labour "
                "market evidence only."
            )

        steps.append(
            RetrievalStep(
                name="programme_norms",
                purpose="Programme wide rules: weekly rhythm, communication norms, naming and submission standards",
                query=f"weekly rhythm daily update communication norms escalate blockers naming convention submission {hint}",
                filters=SearchFilters(
                    source_types=(SourceType.PROGRAMME_BRIEF.value,),
                    facets=(Facet.PROGRAMME_RULES.value, Facet.TEMPLATES.value),
                    only_untracked=True,
                ),
                limit=3,
                required=request.track is None,
            )
        )

        if request.include_market_evidence and role_family:
            market_query = f"{focus} {hint}".strip() or role_family.replace("_", " ")
            for name, purpose, facets, source, limit, required in (
                (
                    "market_responsibilities",
                    "Day to day responsibilities real UK employers describe for this role",
                    [Facet.RESPONSIBILITIES],
                    SourceType.JOB_MARKET,
                    4,
                    request.track is None,
                ),
                (
                    "market_tools",
                    "Tools, platforms and frameworks real employers list for this role",
                    [Facet.TECH_STACK],
                    SourceType.JOB_MARKET,
                    3,
                    False,
                ),
                (
                    "market_collaboration",
                    "How real teams for this role are structured and collaborate",
                    [Facet.TEAM_COLLABORATION, Facet.WORK_CULTURE],
                    SourceType.JOB_MARKET,
                    3,
                    False,
                ),
                (
                    "culture_signals",
                    "Employee sentiment and culture evidence for employers hiring this role",
                    [Facet.EMPLOYEE_REVIEW, Facet.EMPLOYEE_PERSPECTIVE, Facet.CULTURE_RATINGS],
                    SourceType.EMPLOYER_REVIEW,
                    2,
                    False,
                ),
            ):
                strict, ladder = _market_ladder(role_family, facets, level, sector, source)
                steps.append(
                    RetrievalStep(
                        name=name,
                        purpose=purpose,
                        query=market_query,
                        filters=strict,
                        limit=limit,
                        required=required,
                        min_hits=min(2, limit),
                        relaxations=ladder,
                    )
                )

        steps.append(
            RetrievalStep(
                name="brand_voice",
                purpose="Vocabulary and tone rules for every candidate facing message",
                query="AI Manager tone feedback language guidelines preferred language avoid",
                filters=SearchFilters(
                    source_types=(SourceType.BRAND_GUIDELINE.value,),
                    facets=(Facet.BRAND_VOICE.value,),
                ),
                limit=2,
            )
        )
        return RetrievalPlan(operation="task_generation", steps=steps, notes=notes)

    def plan_manager_reply(self, request: ManagerReplyRequest) -> RetrievalPlan:
        context = " ".join(p for p in (request.current_task_title, request.candidate_message) if p)
        steps: list[RetrievalStep] = []
        if request.track is not None:
            code = request.track.value
            steps.append(
                RetrievalStep(
                    name="brief_answer",
                    purpose="Brief content that answers or relates to the candidate message",
                    query=context,
                    filters=SearchFilters(
                        source_types=(SourceType.PROGRAMME_BRIEF.value,),
                        tracks=(code,),
                        include_untracked=True,
                    ),
                    limit=5,
                    apply_similarity_threshold=True,
                )
            )
            steps.append(
                RetrievalStep(
                    name="supervisor_voice",
                    purpose="How the track supervisor speaks",
                    query="welcome note from your track supervisor",
                    filters=_brief(code, [Facet.SUPERVISOR_VOICE]),
                    limit=1,
                )
            )
        else:
            steps.append(
                RetrievalStep(
                    name="programme_answer",
                    purpose="Programme rules that relate to the candidate message",
                    query=context,
                    filters=SearchFilters(
                        source_types=(SourceType.PROGRAMME_BRIEF.value,), only_untracked=True
                    ),
                    limit=4,
                    apply_similarity_threshold=True,
                )
            )
        if request.role_family is not None:
            level = request.job_level.value if request.job_level else None
            sector = request.sector.value if request.sector else None
            strict, ladder = _market_ladder(
                request.role_family.value,
                [Facet.RESPONSIBILITIES, Facet.TECH_STACK, Facet.TEAM_COLLABORATION],
                level,
                sector,
                SourceType.JOB_MARKET,
            )
            steps.append(
                RetrievalStep(
                    name="market_context",
                    purpose="Real employer practice related to the candidate message",
                    query=context,
                    filters=strict,
                    limit=3,
                    relaxations=ladder,
                    apply_similarity_threshold=True,
                )
            )
        steps.append(
            RetrievalStep(
                name="brand_voice",
                purpose="Tone rules for feedback and candidate messages",
                query="AI Manager feedback tone respectful direct",
                filters=SearchFilters(
                    source_types=(SourceType.BRAND_GUIDELINE.value,),
                    facets=(Facet.BRAND_VOICE.value,),
                ),
                limit=1,
            )
        )
        return RetrievalPlan(operation="manager_reply", steps=steps)

    def plan_retrieve(self, request: RetrieveRequest) -> RetrievalPlan:
        """Source balanced free text retrieval.

        Without explicit source types the plan runs two steps: programme material (brief,
        brand, product) and market evidence (job postings, employee reviews). Evidence is
        then interleaved, so programme rules are never crowded out by the much larger body
        of job postings, and level and sector filters apply only where they have meaning.
        """
        exclude = (
            ()
            if request.include_engineering_reference
            else (SourceType.ENGINEERING_REFERENCE.value,)
        )
        family = (request.role_family.value,) if request.role_family else ()
        track = (request.track.value,) if request.track else ()
        if request.source_types:
            filters = SearchFilters(
                source_types=tuple(request.source_types),
                facets=tuple(request.facets),
                role_families=family,
                include_untagged_role_family=True,
                job_levels=(request.job_level.value,) if request.job_level else (),
                sectors=(request.sector.value,) if request.sector else (),
                tracks=track,
                include_untracked=True,
                exclude_source_types=exclude,
            )
            steps = [self._free_text_step("query", request.query, filters, request.limit)]
            return RetrievalPlan(operation="retrieve", steps=steps)

        programme_sources = tuple(
            s.value
            for s in (
                SourceType.PROGRAMME_BRIEF,
                SourceType.BRAND_GUIDELINE,
                SourceType.PRODUCT_OVERVIEW,
                SourceType.METHODOLOGY,
                SourceType.ENGINEERING_REFERENCE,
            )
            if s.value not in exclude
        )
        market_sources = (SourceType.JOB_MARKET.value, SourceType.EMPLOYER_REVIEW.value)
        programme_limit = max(1, round(request.limit * 0.6))
        programme = SearchFilters(
            source_types=programme_sources,
            facets=tuple(request.facets),
            role_families=family,
            include_untagged_role_family=True,
            tracks=track,
            include_untracked=True,
        )
        market = SearchFilters(
            source_types=market_sources,
            facets=tuple(request.facets),
            role_families=family,
            job_levels=(request.job_level.value,) if request.job_level else (),
            sectors=(request.sector.value,) if request.sector else (),
        )
        steps = [self._free_text_step("programme", request.query, programme, programme_limit)]
        if request.limit - programme_limit > 0:
            steps.append(
                self._free_text_step(
                    "market", request.query, market, request.limit - programme_limit
                )
            )
        return RetrievalPlan(operation="retrieve", steps=steps)

    @staticmethod
    def _free_text_step(name: str, query: str, filters: SearchFilters, limit: int) -> RetrievalStep:
        return RetrievalStep(
            name=name,
            purpose="Free text retrieval",
            query=query,
            filters=filters,
            limit=limit,
            apply_similarity_threshold=True,
        )
