# Integration guide

This guide is for the platform developer connecting the Vet2Employ platform to the RAG Service. It assumes the platform keeps its existing OpenRouter integration for other work and uses this service whenever the AI Manager or a colleague persona speaks to a candidate.

## 1. Deploy and load

1. Provision PostgreSQL 15 or later with the pgvector extension (AlloyDB for PostgreSQL and Supabase both qualify).
2. Set the environment (see `.env.example`). At minimum: `OPENROUTER_API_KEY`, `V2E_DATABASE_URL`, `V2E_API_KEYS`, `V2E_ADMIN_API_KEYS`, `V2E_ENVIRONMENT=production`.
3. Deploy the container. On start it applies migrations and serves on port 8000.
4. Load the knowledge base once: `docker compose run --rm ingest`, or `POST /v1/admin/ingest` with an admin key, or `v2e ingest` from a job.
5. Wait for `GET /health/ready` to return `200`. Readiness requires the database, a loaded knowledge base, a configured OpenRouter key and an embedding model that matches the stored vectors.
6. Run `v2e doctor` in the deployed environment and `scripts/smoke_test.py --base-url <url> --api-key <key>` against the deployed API. Both must report no failures. The full checklist is in [handover.md](handover.md).

## 2. Authentication and headers

| Header | Required | Purpose |
| :--- | :--- | :--- |
| `X-API-Key` | Yes | A key from `V2E_API_KEYS` (admin endpoints need a key from `V2E_ADMIN_API_KEYS`) |
| `X-Request-ID` | Recommended | Correlates platform and service logs; echoed back on every response |
| `Content-Type: application/json` | For POST bodies | |

## 3. A small typed client

For full coverage of every field, generate a client from the committed schema `docs/openapi.json` (for example with `openapi-typescript`). The hand written client below shows the essentials.

```typescript
// v2eRag.ts: minimal client for the Vet2Employ RAG Service
export type Track = "DA" | "SE" | "CY" | "DE";
export type TaskType = "core_activity" | "colleague_request" | "stakeholder_update" | "complication";

export interface TaskRequest {
  track?: Track;
  role_family?: "cybersecurity" | "data_analyst" | "data_engineer" | "software_developer";
  job_level?: "beginner" | "mid_level" | "advanced" | "expert";
  sector?: "construction" | "consulting" | "education" | "healthcare" | "information_technology" | "general";
  task_type?: TaskType;
  activity_number?: number;
  simulation_day?: "Monday" | "Tuesday" | "Wednesday" | "Thursday" | "Friday";
  focus?: string;
  region?: string;
  candidate_first_name?: string;
  manager_name?: string;
  previous_task_titles?: string[];
}

export class RagError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details: Record<string, unknown>,
    public requestId: string | null,
  ) {
    super(message);
  }

  /** Worth retrying with backoff: the request may succeed later unchanged. */
  get retryable(): boolean {
    return [502, 503, 504].includes(this.status);
  }
}

export class RagClient {
  // Must exceed V2E_REQUEST_TIMEOUT_SECONDS (150 by default) so the service answers first.
  constructor(private baseUrl: string, private apiKey: string, private timeoutMs = 170_000) {}

  private async post<T>(path: string, body: unknown, requestId: string): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": this.apiKey, "X-Request-ID": requestId },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    const payload = await response.json();
    if (!response.ok) {
      // Every error from the service has this envelope: error, message, details, request_id.
      throw new RagError(response.status, payload.error, payload.message, payload.details ?? {}, payload.request_id ?? null);
    }
    return payload as T;
  }

  generateTask(request: TaskRequest, requestId: string) {
    return this.post<TaskResponse>("/v1/tasks/generate", request, requestId);
  }

  managerReply(request: Record<string, unknown>, requestId: string) {
    return this.post<ManagerReplyResponse>("/v1/manager/reply", request, requestId);
  }
}

export interface Grounded { text: string; evidence_ids: string[] }
export interface TaskResponse {
  task: {
    title: string;
    sender_role: string;
    priority: "P0" | "P1" | "P2";
    message: string;
    summary: Grounded;
    objectives: Grounded[];
    steps: Grounded[];
    deliverables: { reference: string | null; name: string; file_name: string | null; due: string | null }[];
    schedule: { assigned_on: string | null; due: string | null; estimated_effort_hours: number | null };
    open_questions: string[];
  };
  evidence: { evidence_id: string; chunk_id: string; citation: string; source_type: string }[];
  verification: { verdict: string; groundedness: number; pruned: string[] };
  meta: { audit_id: string; model: string; prompt_version: string };
}
export interface ManagerReplyResponse {
  reply: { reply: string; intent: string; needs_human_review: boolean; review_reason: string | null };
  verification: { verdict: "approved" | "approved_with_pruning" | "abstained" | "intercepted" };
  safeguarding: { flagged: boolean; categories: string[]; action: "none" | "flag" | "intercept" };
  meta: { audit_id: string };
}
```

## 4. Issuing work from the Task Engine

The Task Engine already schedules work with BullMQ. Generate the task inside the job, persist it, then notify the candidate.

```typescript
import { Worker } from "bullmq";
import { RagClient, RagError } from "./v2eRag";

const rag = new RagClient(process.env.V2E_RAG_URL!, process.env.V2E_RAG_API_KEY!);

new Worker("simulation-activities", async (job) => {
  const { candidateId, track, activityNumber, simulationDay, firstName, cohortWeekStart } = job.data;
  try {
    const result = await rag.generateTask(
      { track, activity_number: activityNumber, simulation_day: simulationDay, candidate_first_name: firstName },
      `task-${candidateId}-${track}-${activityNumber}`,
    );
    await saveTask({
      candidateId,
      title: result.task.title,
      priority: result.task.priority,
      message: result.task.message,
      deadlineAt: resolveDue(result.task.schedule.due, cohortWeekStart),
      body: result.task,
      citations: result.evidence,
      auditId: result.meta.audit_id,
    });
  } catch (error) {
    if (!(error instanceof RagError)) throw error;
    if (error.code === "insufficient_evidence" || error.code === "invalid_request") {
      // The knowledge base or catalogue cannot support this request. Never improvise with a raw model call.
      await flagForContentTeam(candidateId, error.code, error.details, error.requestId);
      return;
    }
    if (error.code === "grounding_failed" && job.attemptsMade === 0) {
      throw error; // one retry; a second failure is alerted below
    }
    if (error.retryable) {
      throw error; // let BullMQ retry with backoff (502, 503, 504)
    }
    await alertEngineering(error.code, error.details.audit_id, error.requestId);
  }
});
```

### Turning a due point into a timestamp

Due points are copied from the brief (for example `Tue 17:00`) and are relative to the cohort week. Resolve them on the platform side in the UK time zone:

```typescript
const DAYS: Record<string, number> = { mon: 0, tue: 1, wed: 2, thu: 3, fri: 4 };

export function resolveDue(due: string | null, weekStartMonday: Date): Date | null {
  if (!due) return null;
  const match = due.toLowerCase().match(/\b(mon|tue|wed|thu|fri)[a-z]*\b.*?(\d{1,2}):(\d{2})/);
  if (!match) return null;
  const date = new Date(weekStartMonday);
  date.setDate(date.getDate() + DAYS[match[1]]);
  date.setHours(Number(match[2]), Number(match[3]), 0, 0);
  return date; // interpret in Europe/London when storing
}
```

### Mapping to the knowledge graph Task node

| Graph property (design deck) | Source |
| :--- | :--- |
| `task_id` | Generated by the platform |
| `title` | `task.title` |
| `assigned_at` | Time the platform delivers the message |
| `deadline_at` | `resolveDue(task.schedule.due, cohortWeekStart)` |
| `priority` | `task.priority` (`P0`, `P1` or `P2`, the same enumeration as the design deck) |
| `ASSIGNED_BY` AIPersona | The persona the platform used; pass its name as `manager_name` |
| Evidence trail | `evidence[].chunk_id` and `meta.audit_id`, stored as properties or edges |

## 5. Replying to candidates

```typescript
const result = await rag.managerReply(
  {
    track: "DA",
    candidate_message: message.text,
    conversation: lastTurns.map((t) => ({ role: t.fromCandidate ? "candidate" : "manager", content: t.text })),
    current_task_title: task.title,
    current_task_summary: task.body.summary.text,
    simulation_day: "Tuesday",
    candidate_first_name: candidate.firstName,
  },
  `reply-${message.id}`,
);

await postManagerMessage(result.reply.reply);
if (result.safeguarding.action === "intercept") {
  // A crisis message. The reply is a fixed support message written by people.
  await pageProgrammeOffice(candidate.id, result.reply.review_reason, result.meta.audit_id);
  await pauseAutomatedTasks(candidate.id);
} else if (result.reply.needs_human_review) {
  await notifyProgrammeOffice(candidate.id, result.reply.review_reason, result.meta.audit_id);
}
```

Every candidate message passes a safeguarding screen before any model call (see [safeguarding.md](safeguarding.md)). Flagged categories always set `needs_human_review`; crisis messages are intercepted. Make sure intercepts reach a person quickly.

Send at most the last 12 turns; older turns are ignored. Personal data in the message and history is redacted before it reaches any model, and the counts are returned in `meta.diagnostics.pii_redactions`.

## 6. Grounding flows that stay on direct OpenRouter calls

For a flow that must keep its own OpenRouter call (for example a bespoke colleague persona), ground and verify it through the service:

```typescript
// 1. Retrieve evidence and a ready made grounding block
const grounding = await fetch(`${base}/v1/retrieve`, {
  method: "POST",
  headers,
  body: JSON.stringify({ query: candidateQuestion, track: "CY", limit: 6 }),
}).then((r) => r.json());

// 2. Call OpenRouter as today, with the grounding block as the system prompt
const completion = await openrouter.chat.completions.create({
  model: "anthropic/claude-haiku-4.5",
  messages: [
    { role: "system", content: grounding.grounding_prompt + "\n\n" + personaInstructions },
    { role: "user", content: candidateQuestion },
  ],
});
const text = completion.choices[0].message.content;

// 3. Verify before showing it to the candidate
const check = await fetch(`${base}/v1/verify`, {
  method: "POST",
  headers,
  body: JSON.stringify({ text, chunk_ids: grounding.evidence.map((e: { chunk_id: string }) => e.chunk_id) }),
}).then((r) => r.json());

if (check.verification.verdict === "rejected") {
  // regenerate or fall back to /v1/manager/reply
}
```

## 7. Recommended week flow

The Work Brief defines a fixed weekly rhythm. A faithful simulation issues work in the same order.

| Day | Typical calls |
| :--- | :--- |
| Monday | `core_activity` for activities 1 and 2 (induction, plan); `stakeholder_update` to open the client question window |
| Tuesday | `core_activity` for the next activities; `complication` once the candidate has started analysis |
| Wednesday | `core_activity`; `stakeholder_update` before the midweek review |
| Thursday | `colleague_request` for the peer review exchange; `core_activity` |
| Friday | `core_activity` for the final deliverables and reflection |

Pass `previous_task_titles` so the AI Manager does not repeat itself, and pass `simulation_day` so the schedule wording matches the week.

## 8. Error handling contract

Every error body is `{"error", "message", "details", "request_id"}`. Log `request_id` with the platform's own request so both sides of an incident can be joined.

| Status | `error` | Meaning | What the platform should do |
| :--- | :--- | :--- | :--- |
| 401, 403 | `unauthorized`, `forbidden` | Missing or wrong API key | Fix configuration |
| 409 | `conflict`, `ingestion_in_progress` | Another ingestion run is active | Wait for it to finish |
| 413 | `payload_too_large` | Body above the limit | Send fewer conversation turns or shorter text |
| 422 | `invalid_request` | Request fields are invalid or contradict the catalogue (for example an activity number above `activity_count`) | Fix the request; do not retry unchanged |
| 422 | `insufficient_evidence` | Required evidence is not in the knowledge base | Do not improvise; choose another activity or add material |
| 422 | `grounding_failed` | Output could not be verified as grounded | Retry once; if repeated, alert and quote `audit_id` |
| 422 | `unknown_chunks` | `/v1/verify` received chunk ids that do not exist | Use ids from a recent `/v1/retrieve` response |
| 500 | `internal_error` | Unexpected failure | Alert with `request_id` |
| 502 | `upstream_model_error`, `invalid_model_output` | OpenRouter or model failure | Retry with backoff |
| 503 | `verification_unavailable`, `service_unavailable`, `store_unavailable` | Judge or dependency unavailable | Retry with backoff; never show unverified output |
| 504 | `generation_timeout` | The service deadline passed | Retry with backoff |

## 9. Performance expectations

A task generation performs one embedding call per distinct planned query, one generation call, one judge call and, only when needed, one repair and one further judge call. Judge batches run in parallel, so verification adds roughly one judge call of latency however many claims a task has. Expect latency to be dominated by the generation model. The service stops itself at `V2E_REQUEST_TIMEOUT_SECONDS` (150 by default), so set client timeouts slightly above that, and generate tasks ahead of time in the Task Engine rather than on the request path of the candidate app. Retrieval itself completes in tens of milliseconds on the supplied knowledge base.
