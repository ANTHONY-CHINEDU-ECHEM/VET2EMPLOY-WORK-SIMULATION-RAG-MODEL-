# 0002: Verification fails closed at every step

Status: Accepted, October 2026. Strengthened in 1.1.0.

## Context

Verification is only as strong as its weakest path. Review before handover found three places where uncertainty was treated as success: a claim the judge did not return a verdict for was a minor problem, a claim id the pruner did not recognise was skipped, and a required retrieval step could pass the abstention gate while contributing nothing to the evidence pack.

## Decision

Whenever the service cannot prove that content is supported, it treats the content as unsupported:

* the judge is asked again about skipped claims, and claims still unjudged are major violations;
* a judge error while verification is required returns `503 verification_unavailable`;
* the pruner refuses any claim it cannot locate, and refuses unsupported core content (title, summary, objectives, steps);
* the best passage of every required retrieval step is reserved in the evidence pack before the token budget applies;
* production refuses to start with entailment verification disabled.

## Consequences

* Some outputs that a looser system would have returned are rejected or abstained. The platform must handle `422` and `503` without falling back to unverified generation.
* A model outage becomes visible as errors rather than as silently unverified content.
* Every new verification or pruning path must state how it fails, and tests must cover the failure.
