"""GENESIS Phase 5: action centre and explicit, per-item approval queue.

This module prepares external actions but never executes them. An approved item
is only marked ready for a separately implemented, owner-authorised adapter.
"""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field
import app as core


ALLOWED_KINDS = {"email_draft", "social_post", "support_reply", "purchase_request"}
ALLOWED_DECISIONS = {"approve", "reject"}


def canonical_payload(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_digest(payload):
    return hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()


def init():
    with core.db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS action_queue (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending_approval',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            decided_at TEXT NOT NULL DEFAULT '',
            decision_note TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_action_queue_status_created
            ON action_queue(status, created_at DESC);
        CREATE TABLE IF NOT EXISTS action_audit (
            id TEXT PRIMARY KEY,
            action_id TEXT NOT NULL,
            event TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(action_id) REFERENCES action_queue(id)
        );
        """)


class ActionIn(BaseModel):
    kind: str
    title: str = Field(min_length=3, max_length=180)
    payload: dict = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=120)


class DecisionIn(BaseModel):
    decision: str
    note: str = Field(default="", max_length=500)


def audit(action_id, event, detail=""):
    with core.db() as c:
        c.execute(
            "INSERT INTO action_audit VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), action_id, event, detail[:500], core.now())
        )


def expire_pending():
    stamp = core.now()
    with core.db() as c:
        rows = c.execute(
            "SELECT id FROM action_queue WHERE status='pending_approval' AND expires_at < ?",
            (stamp,)
        ).fetchall()
        for row in rows:
            c.execute(
                "UPDATE action_queue SET status='expired' WHERE id=? AND status='pending_approval'",
                (row["id"],)
            )
            c.execute(
                "INSERT INTO action_audit VALUES(?,?,?,?,?)",
                (str(uuid.uuid4()), row["id"], "expired", "Approval window elapsed.", stamp)
            )


def public_action(row):
    payload = json.loads(row["payload_json"])
    actual_hash = payload_digest(payload)
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "payload": payload,
        "payload_sha256": row["payload_sha256"],
        "integrity_ok": actual_hash == row["payload_sha256"],
        "status": row["status"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "decided_at": row["decided_at"],
        "decision_note": row["decision_note"],
    }


def install(app):
    @app.on_event("startup")
    def startup():
        init()

    @app.get("/api/internal/actions/overview")
    def overview(x_genesis_key: str | None = Header(default=None)):
        core.require_admin(x_genesis_key)
        init()
        expire_pending()
        with core.db() as c:
            rows = c.execute(
                "SELECT * FROM action_queue ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
            audit_rows = c.execute(
                "SELECT * FROM action_audit ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        actions = [public_action(row) for row in rows]
        return {
            "actions": actions,
            "audit": [dict(row) for row in audit_rows],
            "metrics": {
                "pending": sum(x["status"] == "pending_approval" for x in actions),
                "approved_not_executed": sum(x["status"] == "approved_not_executed" for x in actions),
                "rejected": sum(x["status"] == "rejected" for x in actions),
                "expired": sum(x["status"] == "expired" for x in actions),
            },
            "policy": [
                "Each action requires its own explicit human decision; no blanket approvals.",
                "The approved payload is immutable and protected by a SHA-256 integrity check.",
                "Approval means approved_not_executed: no email, message, post, purchase or payment is performed.",
                "Purchases are requests for an authorised adult to review; GENESIS cannot move money.",
                "Use only organisation-level contact details and opt-in customer conversations. No spam or private-data scraping.",
            ],
        }

    @app.post("/api/internal/actions")
    def create_action(data: ActionIn, x_genesis_key: str | None = Header(default=None)):
        core.require_admin(x_genesis_key)
        init()
        if data.kind not in ALLOWED_KINDS:
            raise HTTPException(400, "Unsupported action type.")
        payload_text = canonical_payload(data.payload)
        if len(payload_text) > 16000:
            raise HTTPException(413, "Action payload is too large.")
        if data.kind == "purchase_request":
            # Request only; never treat approval as authority to transact.
            payload = dict(data.payload)
            payload["execution_policy"] = "adult_review_required_no_automatic_purchase"
            payload_text = canonical_payload(payload)
        digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        action_id = str(uuid.uuid4())
        created = datetime.now(timezone.utc)
        created_at = created.isoformat()
        expires_at = (created + timedelta(days=7)).isoformat()
        with core.db() as c:
            existing = c.execute(
                "SELECT * FROM action_queue WHERE idempotency_key=?",
                (data.idempotency_key,)
            ).fetchone()
            if existing:
                return {"action": public_action(existing), "duplicate": True}
            c.execute(
                """INSERT INTO action_queue
                (id,idempotency_key,kind,title,payload_json,payload_sha256,status,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (action_id, data.idempotency_key, data.kind, data.title,
                 payload_text, digest, "pending_approval", created_at, expires_at)
            )
        audit(action_id, "created", "Queued for one-time human review.")
        with core.db() as c:
            row = c.execute("SELECT * FROM action_queue WHERE id=?", (action_id,)).fetchone()
        return {"action": public_action(row), "duplicate": False,
                "note": "Queued for review. Nothing was sent, published or purchased."}

    @app.post("/api/internal/actions/{action_id}/decision")
    def decide_action(action_id: str, data: DecisionIn,
                      x_genesis_key: str | None = Header(default=None)):
        core.require_admin(x_genesis_key)
        init()
        if data.decision not in ALLOWED_DECISIONS:
            raise HTTPException(400, "Decision must be approve or reject.")
        expire_pending()
        with core.db() as c:
            row = c.execute("SELECT * FROM action_queue WHERE id=?", (action_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Action not found.")
            if row["status"] != "pending_approval":
                raise HTTPException(409, "This action is no longer awaiting approval.")
            # Refuse decisions if stored content no longer matches its recorded digest.
            payload = json.loads(row["payload_json"])
            if payload_digest(payload) != row["payload_sha256"]:
                c.execute("UPDATE action_queue SET status='integrity_error' WHERE id=?", (action_id,))
                c.execute(
                    "INSERT INTO action_audit VALUES(?,?,?,?,?)",
                    (str(uuid.uuid4()), action_id, "integrity_error",
                     "Payload hash mismatch; action blocked.", core.now())
                )
                integrity_failed = True
                status = "integrity_error"
            else:
                integrity_failed = False
                status = "approved_not_executed" if data.decision == "approve" else "rejected"
                stamp = core.now()
                c.execute(
                    "UPDATE action_queue SET status=?,decided_at=?,decision_note=? WHERE id=?",
                    (status, stamp, data.note, action_id)
                )
                c.execute(
                    "INSERT INTO action_audit VALUES(?,?,?,?,?)",
                    (str(uuid.uuid4()), action_id, data.decision, data.note[:500], stamp)
                )
        if integrity_failed:
            raise HTTPException(409, "Payload integrity check failed; action blocked.")
        return {
            "id": action_id,
            "status": status,
            "note": "Decision recorded only. No external action was executed."
        }
