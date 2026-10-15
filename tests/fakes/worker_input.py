"""Explicit model-double view of already delivered messages, never runtime state.

Older doubles selected actions from a fresh context snapshot every turn. The
new consumer sends the initial instruction once and originals as tool replies.
This helper lets those scripted doubles inspect that conversation faithfully.
"""

import json


def worker_input(messages):
    initial = next(json.loads(message["content"]) for message in messages
                   if message.get("role") == "user" and "active_task" in json.loads(
                       message.get("content") or "{}"))
    originals = list(initial.get("probe", {}).get("original_evidence_ids", []))
    executed = 0
    for message in messages:
        if message.get("role") != "tool":
            continue
        body = json.loads(message["content"])
        if body.get("executed"):
            executed += 1
            originals.extend(body.get("evidence_ids", []))
    if "probe" in initial:
        initial["probe"]["original_evidence_ids"] = list(dict.fromkeys(originals))
        initial["probe"]["usage"]["actions"] += executed
    return initial
