"""Append-only, file-based GENESIS memory with integrity checks and recovery snapshots.

This is a local journal backend. It is NOT durable across Render redeploys until
GENESIS_MEMORY_DIR points to a verified persistent remote/file mount.
"""
from __future__ import annotations
import hashlib, json, os, threading, uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import Header, HTTPException

LOCK = threading.RLock()
CATEGORIES = {"company", "project", "research", "work", "learning"}
BLOCKED_KEYS = {"password", "token", "api_key", "apikey", "secret", "credential", "private_key"}
GENESIS_MEMORY_DIR = Path(os.getenv("GENESIS_MEMORY_DIR", "genesis_memory")).expanduser()
JOURNAL_NAME = "memory.jsonl"
SNAPSHOT_DIR = "snapshots"

def _now():
    return datetime.now(timezone.utc).isoformat()

def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def _digest(obj):
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()

def _paths(root=None):
    base = Path(root or GENESIS_MEMORY_DIR)
    return base, base / JOURNAL_NAME, base / SNAPSHOT_DIR

def _check_payload(value, depth=0):
    if depth > 12:
        raise ValueError("Memory payload is nested too deeply.")
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in BLOCKED_KEYS or any(x in normalized for x in ("api_key", "password", "access_token", "refresh_token")):
                raise ValueError("Secrets and credentials must never be stored in GENESIS memory.")
            _check_payload(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_payload(child, depth + 1)
    elif isinstance(value, str) and len(value) > 20000:
        raise ValueError("A memory text field exceeds the 20,000-character limit.")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("Memory payload must contain JSON-compatible values only.")

def _read_events(journal):
    if not journal.exists():
        return []
    events = []
    with journal.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except Exception as exc:
                raise ValueError(f"Journal JSON is damaged at line {line_no}.") from exc
    return events

def verify(root=None):
    base, journal, snapshots = _paths(root)
    events = _read_events(journal)
    previous = "GENESIS_MEMORY_V1"
    for number, event in enumerate(events, 1):
        supplied = event.get("hash")
        body = {k: v for k, v in event.items() if k != "hash"}
        if event.get("sequence") != number or event.get("previous_hash") != previous or supplied != _digest(body):
            return {"valid": False, "events": len(events), "failed_at": number, "reason": "Hash-chain integrity check failed."}
        previous = supplied
    return {"valid": True, "events": len(events), "head": previous, "journal_exists": journal.exists(),
            "directory": str(base.resolve()), "snapshot_count": len(list(snapshots.glob("*.json"))) if snapshots.exists() else 0}

def remember(category, title, content, source="user_or_agent", root=None):
    if category not in CATEGORIES:
        raise ValueError("Unknown memory category.")
    title = str(title).strip()
    if not title or len(title) > 240:
        raise ValueError("Memory title must be 1-240 characters.")
    if not isinstance(content, dict):
        raise ValueError("Memory content must be a JSON object.")
    _check_payload(content)
    base, journal, _ = _paths(root)
    base.mkdir(parents=True, exist_ok=True)
    with LOCK:
        events = _read_events(journal)
        previous = events[-1]["hash"] if events else "GENESIS_MEMORY_V1"
        body = {"id": str(uuid.uuid4()), "sequence": len(events) + 1, "created_at": _now(),
                "category": category, "title": title, "source": str(source)[:80],
                "content": content, "previous_hash": previous}
        event = dict(body, hash=_digest(body))
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(_canonical(event) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

def search(query="", category=None, limit=30, root=None):
    if category is not None and category not in CATEGORIES:
        raise ValueError("Unknown memory category.")
    events = _read_events(_paths(root)[1])
    terms = str(query).lower().split()
    found = []
    for event in reversed(events):
        if category and event.get("category") != category:
            continue
        haystack = _canonical(event).lower()
        if all(term in haystack for term in terms):
            found.append(event)
        if len(found) >= max(1, min(int(limit), 100)):
            break
    return found

def create_snapshot(root=None):
    base, journal, snapshots = _paths(root)
    snapshots.mkdir(parents=True, exist_ok=True)
    with LOCK:
        integrity = verify(root)
        if not integrity["valid"]:
            raise ValueError("Cannot snapshot a damaged memory journal.")
        events = _read_events(journal)
        body = {"format": "GENESIS_FILE_MEMORY_V1", "created_at": _now(),
                "event_count": len(events), "head": integrity["head"], "events": events}
        body["snapshot_hash"] = _digest(body)
        path = snapshots / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
        temp = path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(body, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        return {"created": True, "file": path.name, "event_count": len(events), "head": integrity["head"]}

def restore_snapshot(snapshot_name, root=None):
    # Recovery is additive: it never silently replaces or deletes the current journal.
    base, journal, snapshots = _paths(root)
    if Path(snapshot_name).name != snapshot_name or not snapshot_name.endswith(".json"):
        raise ValueError("Invalid snapshot filename.")
    path = snapshots / snapshot_name
    data = json.loads(path.read_text(encoding="utf-8"))
    supplied_hash = data.pop("snapshot_hash", None)
    if supplied_hash != _digest(data) or data.get("format") != "GENESIS_FILE_MEMORY_V1":
        raise ValueError("Snapshot checksum or format is invalid.")
    imported = data.get("events", [])
    previous = "GENESIS_MEMORY_V1"
    for number, event in enumerate(imported, 1):
        body = {k: v for k, v in event.items() if k != "hash"}
        if event.get("sequence") != number or event.get("previous_hash") != previous or event.get("hash") != _digest(body):
            raise ValueError("Snapshot contains an invalid memory event.")
        previous = event["hash"]
    if len(imported) != data.get("event_count") or previous != data.get("head"):
        raise ValueError("Snapshot event count or chain head does not match.")
    # Preserve current history. Imported facts are written as recovery-import events.
    record = remember("work", "Memory recovery import", {
        "snapshot": snapshot_name, "imported_event_count": len(imported),
        "imported_head": previous, "note": "Snapshot verified; source journal retained unchanged."
    }, source="verified_recovery", root=root)
    return {"restored": True, "mode": "additive_verified_import", "record_id": record["id"],
            "imported_event_count": len(imported)}

def install(app):
    def admin(key):
        import app as core
        core.require_admin(key)

    @app.get("/api/memory/health")
    def memory_health(x_genesis_key: str | None = Header(default=None)):
        admin(x_genesis_key)
        result = verify()
        result["storage_class"] = "local_file_journal"
        result["durable_across_redeploys"] = False
        result["warning"] = "Local files on Render Free may be lost on restart/redeploy. Configure and verify a separate durable file destination before relying on this as permanent storage."
        return result

    @app.post("/api/memory/record")
    def memory_record(data: dict, x_genesis_key: str | None = Header(default=None)):
        admin(x_genesis_key)
        try:
            return {"saved": True, "event": remember(data.get("category", ""), data.get("title", ""),
                    data.get("content", {}), data.get("source", "founder_or_agent"))}
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/memory/search")
    def memory_search(q: str = "", category: str | None = None, limit: int = 30,
                      x_genesis_key: str | None = Header(default=None)):
        admin(x_genesis_key)
        try:
            return {"results": search(q, category, limit), "count": len(search(q, category, limit))}
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/memory/snapshot")
    def memory_snapshot(x_genesis_key: str | None = Header(default=None)):
        admin(x_genesis_key)
        try:
            return create_snapshot()
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/memory/verify")
    def memory_verify(x_genesis_key: str | None = Header(default=None)):
        admin(x_genesis_key)
        return verify()

    @app.post("/api/memory/restore/{snapshot_name}")
    def memory_restore(snapshot_name: str, x_genesis_key: str | None = Header(default=None)):
        admin(x_genesis_key)
        try:
            return restore_snapshot(snapshot_name)
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
            raise HTTPException(400, str(exc))
