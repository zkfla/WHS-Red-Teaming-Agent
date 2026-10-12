---
name: final-deliverable-not-demo
description: Project goal is a real final deliverable, not a demo — whole system must work end-to-end for real
metadata:
  type: project
---

**Goal = final deliverable, not a demo.** The user stressed (2026-10-02, "꼭 기억해") that work must move the whole system toward behaving as a real final product end-to-end, not a demo/vertical-slice. 

Implications: don't accept "demo-level" defaults or stubs as good enough; silent-fake defaults, unwired modules, and "works only with the right config" are not acceptable for the final. When reviewing/fixing, judge against "does the real end-to-end flow work for a real target," not "does the happy-path demo run." See [[control-scope-port-work]], and the cross-team collaboration doc (`docs/cross-team-final-collaboration.md`) listing remaining demo-level items (D2 ExampleMCPAdapter default, seed_endpoints, artifact_ref wiring).

User is currently working in the **Control** role (even after the Agent-team move), continuing Control-domain work. See [[team-move-control-to-agent]] (now partly superseded: actively Control again for this push).
