# 0001: Planned and verified hybrid retrieval augmented generation

Status: Accepted, October 2026

## Context

The AI Manager must behave like a real manager in each discipline without inventing deadlines, colleagues, tools or files, because invented details break the simulation and contaminate the evidence behind a Verified Profile. Five architectures were compared against weighted requirements (hallucination control, auditability, precision on exact identifiers, completeness of each task, predictable cost and fit with the platform). The full comparison is in the README.

## Decision

Use a deterministic retrieval planner that issues one purpose specific hybrid search per kind of evidence a task needs, fuse vector and full text results with reciprocal rank fusion, assemble a cited and budgeted evidence pack, generate against a strict schema in which every item cites its evidence, and verify every claim before release.

## Consequences

* Every candidate receives equally grounded work for the same request, which keeps Verified Profiles comparable.
* The service can detect missing evidence before calling a model and abstain.
* Each task costs two to four model calls instead of one; the judge uses a smaller model to contain cost.
* Graph based retrieval remains possible later: chunk and record identifiers are stable and can be referenced from the knowledge graph.
