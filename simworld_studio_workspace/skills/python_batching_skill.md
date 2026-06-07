---
id: python_batching
name: Python Script Batching
version: 1.0.0
author: simworld
tags: [python, batching, execute_python_script, scene-generation, safety]
dependencies: []
description: Use execute_python_script safely by building scenes in small verifiable batches instead of one giant script.
---

# Python Script Batching

Use this skill whenever you need `execute_python_script` for Unreal Engine scene construction or bulk editor operations.

## Core Rules

1. Prefer normal MCP tools for simple spawning, transforms, screenshots, verification, and single-actor edits.
2. Use `execute_python_script` only for UE API work that normal tools cannot express, or for carefully scoped bulk edits.
3. Keep each script to roughly 6-12 actors or operations. Use fewer when assets are heavy, logic is complex, or the scene already has many actors.
4. Never generate one giant Python script for an entire large scene or full task pipeline.
5. Do not call other UE tools while a Python job is running. Read `log_path` until it contains `[DONE]` or `[ERROR]`, inspect the result, then continue.

## Scene Construction Phases

Build large scenes as sequential batches:

1. clear/setup
2. major layout
3. buildings
4. props/furniture
5. vegetation/vehicles/agents
6. validation/save

Each phase can be split further if it would exceed 6-12 actors or operations.

## Script Completion Markers

Every script must print a terminal marker:

```python
try:
    # focused batch work here
    print("[DONE] buildings_batch_01 count=8")
except Exception as exc:
    print("[ERROR] buildings_batch_01: " + str(exc))
```

After the tool returns `{job_id, log_path}`, read the log until one of those markers appears before issuing the next UE command.

## Camera Rule

Do not move or set the viewport camera from Python. The user controls the camera through Pixel Streaming. Use `take_screenshot` after meaningful edits so the UI can show progress.
