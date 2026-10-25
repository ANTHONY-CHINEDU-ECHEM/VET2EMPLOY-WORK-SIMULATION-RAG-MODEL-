# 0005: A deterministic safeguarding screen runs before any model

Status: Accepted, October 2026 (introduced in 1.1.0)

## Context

Candidates include veterans under pressure. In 1.0.0, recognising a wellbeing or crisis disclosure depended entirely on the generation model setting `needs_human_review`. A model can miss a disclosure, and a reply to a person in crisis should not depend on retrieval or verification succeeding.

## Decision

Screen every candidate message with rules in `config/safeguarding.yaml`, owned by the Programme Office, before retrieval or any model call. Crisis matches return a fixed, human written support message and are audited as `intercepted`. Other categories force `needs_human_review` on the normal reply. The screen only adds caution and never removes a review the model requested.

## Consequences

* A crisis response cannot fail because OpenRouter is down or the knowledge base lacks evidence.
* The intercept message steps outside the simulation. Safety is placed above immersion deliberately.
* Pattern rules produce some false positives, which cost a short human review, and cannot catch every expression of distress. Human oversight remains essential (see safeguarding.md).
* The Programme Office must review the rules and support contacts before launch.
