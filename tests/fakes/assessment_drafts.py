"""Storage-only assessment input; no target, vulnerability oracle or model answer."""


def draft(*, target="host:unit.local", evidence_id=None, **changes):
    payload = {
        "draft_id": "draft-a", "contract_version": 1,
        "name": "Retained response observation", "vulnclass": "Unclassified",
        "statement": "The retained observation needs meaning review.",
        "target_node_id": target, "judgment": "inconclusive",
        "reason": "Store the observation without a candidate verdict.",
        "expectation": {"statement": "The intended boundary is not established.",
                        "source": "assumption"},
        "limitations": ["No independent security judgment is asserted."],
    }
    if evidence_id is not None:
        payload["evidence_refs"] = [evidence_id]
        payload["observations"] = [{
            "statement": "A stored status field is present.",
            "sources": [{"evidence_id": evidence_id, "raw_path": ["status"]}],
        }]
    return {**payload, **changes}
