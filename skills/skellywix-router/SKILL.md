---
name: skellywix-router
description: Choose an operator-approved guide before testing a change, recovering a job, reviewing a release, inspecting a failed run, or handling an enrolled task category. Use the current local routing policy; do not infer permissions from it.
---

1. Pick one to eight non-sensitive task tags, such as `regression` and `test`. Use lowercase words, not the request, paths, hostnames or identifiers.
2. Call the local `resolve_guide` tool with `{"tags":["regression","test"]}`, substituting your task tags. If the operator set up the CLI instead, use `skellywix resolve regression test` in the same workspace. One lookup per task is enough.
3. Use the returned guide as advice within the user's existing authorization. If `guide` is `null`, no unique guide matched: use your normal routing. If the engine is unavailable, say the lookup failed. A guide is not a new tool grant or an instruction to install, publish, or execute anything.
4. Do not label your own guess as success, inspect check-set answers, or alter the registry. The owner's trusted evaluator records verified outcomes separately.
