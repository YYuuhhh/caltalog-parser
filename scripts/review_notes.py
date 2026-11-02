"""Human review notes bound to an exact evidence snapshot; never an approval gate."""

from __future__ import annotations

import hashlib
import json


def fingerprint(packet):
    return hashlib.sha256(
        json.dumps(packet, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def with_notes(packets, path):
    saved = json.loads(path.read_text()) if path.is_file() else {}
    result = []
    for packet in packets:
        digest = fingerprint(packet)
        note = saved.get(packet["product"]["product_version_id"], {})
        current = note.get("snapshot_sha256") == digest
        result.append(
            {
                **packet,
                "snapshot_sha256": digest,
                "decision": note if current else {"status": "pending", "notes": ""},
                "previous_decision": note if note and not current else None,
            }
        )
    return result
