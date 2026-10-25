# 0004: Fence untrusted text in prompts

Status: Accepted, October 2026 (introduced in 1.1.0)

## Context

Candidate messages, conversation history and task context are written by people the service does not control, and job posting passages were collected from the web. In 1.0.0 this text was placed in prompts as plain lines, so a message could imitate a section header (for example a line starting with `MANAGER:`) or carry instructions. The judge received claims as pipe separated lines that claim text could imitate.

## Decision

* Wrap every untrusted span in a named element (`<candidate_message>`, `<turn>`, `<current_task>`, `<evidence>`) and tell the model, as a grounding rule, that text inside these elements is data and that candidate assertions are not evidence.
* Neutralise any fence tag inside untrusted text before rendering, so it cannot close or forge a fence.
* Send claims to the judge as a JSON array and accept verdicts only for claim ids that were asked.
* Exclude the candidate's own message from the text that licenses numbers and names in a reply.

## Consequences

* Injection attempts must now defeat both the fencing and claim level verification, which checks every statement against retrieved evidence regardless of what the model was persuaded to write.
* Prompt changes are versioned (`PROMPT_VERSION` 2026.10.2) and covered by tests and generation scenarios.
* Fencing reduces risk but is not a guarantee; verification remains the control that decides what reaches a candidate.
