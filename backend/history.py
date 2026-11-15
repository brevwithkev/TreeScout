"""Optional Atlas history. Snapshots are scoped to a browser's random bearer token."""

from datetime import datetime, timezone
import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
from threading import Lock
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from bson.errors import InvalidDocument

MAX_PLAN_BYTES = 2_000_000
MAX_BODY_BYTES = MAX_PLAN_BYTES + 10_000


class BoundedBodyRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def bounded(request: Request):
            if request.method == "POST":
                chunks, size = [], 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_BODY_BYTES:
                        raise HTTPException(413, "This plan is too large to save.")
                    chunks.append(chunk)
                request._body = b"".join(chunks)
            return await handler(request)
        return bounded


router = APIRouter(prefix="/history", tags=["Hike history"], route_class=BoundedBodyRoute)
logger = logging.getLogger(__name__)
_client = None
_lock = Lock()
_indexed = set()


def collection():
    global _client
    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri:
        raise HTTPException(503, "Hike history is not configured. Set MONGODB_URI on the server.")
    try:
        with _lock:
            if _client is None:
                _client = MongoClient(uri, serverSelectionTimeoutMS=2000, connectTimeoutMS=2000,
                                     socketTimeoutMS=2000, timeoutMS=3000, appname="TrekChek")
        database = os.getenv("MONGODB_DB", "").strip() or "trekchek"
        result = _client[database]["hike_history"]
        with _lock:
            if database not in _indexed:
                result.create_index([("owner", 1), ("saved_at", -1), ("_id", -1)])
                _indexed.add(database)
        return result
    except (PyMongoError, ValueError):
        raise HTTPException(503, "Hike history could not connect to Atlas.") from None


def owner(x_history_token: str | None = Header(default=None)):
    # The token is an anonymous credential, not a username. Never put it in URLs or logs.
    if not x_history_token or not re.fullmatch(r"[0-9a-f]{64}", x_history_token):
        raise HTTPException(422, "A valid browser history credential is required.")
    return hashlib.sha256(x_history_token.encode()).hexdigest()


def unavailable():
    # Driver exceptions can contain hostnames/configuration; do not expose or log their text.
    logger.warning("Atlas history operation failed")
    return HTTPException(503, "Hike history is unavailable. Your current plan is still usable.")


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: dict


@router.post("", status_code=201)
def save(body: Snapshot, owner_id: str = Depends(owner)):
    plan = body.plan
    trail = plan.get("trail")
    if not isinstance(trail, dict) or not isinstance(trail.get("coords"), list) or len(trail["coords"]) < 2:
        raise HTTPException(422, "Generate a hike with a mapped trail before saving.")
    if not isinstance(plan.get("request"), str) or not plan["request"].strip():
        raise HTTPException(422, "The plan must include its hike request.")
    for coord in trail["coords"]:
        if (not isinstance(coord, list) or len(coord) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or (isinstance(v, float) and not math.isfinite(v)) for v in coord)
                or not -90 <= coord[0] <= 90 or not -180 <= coord[1] <= 180):
            raise HTTPException(422, "Trail coordinates must be valid latitude/longitude pairs.")
    if "warnings" in plan and (not isinstance(plan["warnings"], list)
                               or any(not isinstance(w, str) for w in plan["warnings"])):
        raise HTTPException(422, "Plan warnings must be a list of text values.")
    try:
        encoded = json.dumps(plan, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise HTTPException(422, "The plan must contain valid finite JSON values.") from None
    if len(encoded) > MAX_PLAN_BYTES:
        raise HTTPException(413, "This plan is too large to save.")
    saved_at = datetime.now(timezone.utc).isoformat()
    identifier = uuid4().hex
    try:
        collection().insert_one({"_id": identifier, "owner": owner_id, "saved_at": saved_at,
                                 "request": plan["request"], "trail_name": str(trail.get("name") or "Trail"),
                                 "plan": plan})
    except (InvalidDocument, OverflowError):
        raise HTTPException(422, "The plan contains unsupported snapshot values.") from None
    except PyMongoError:
        raise unavailable() from None
    return {"id": identifier, "saved_at": saved_at}


@router.get("")
def listing(limit: int = Query(default=20, ge=1, le=50),
            before: str | None = Query(default=None, max_length=256), owner_id: str = Depends(owner)):
    query = {"owner": owner_id}
    if before:
        stamp, identifier = decode_cursor(before)
        query["$or"] = [{"saved_at": {"$lt": stamp}}, {"saved_at": stamp, "_id": {"$lt": identifier}}]
    try:
        rows = list(collection().find(query, {"plan": 0, "owner": 0})
                    .sort([("saved_at", -1), ("_id", -1)]).limit(limit + 1))
    except PyMongoError:
        raise unavailable() from None
    items = [{"id": row["_id"], "request": row["request"], "trail_name": row["trail_name"],
              "saved_at": row["saved_at"]} for row in rows[:limit]]
    return {"items": items, "next_before": encode_cursor(items[-1]) if len(rows) > limit else None}


def encode_cursor(item):
    return base64.urlsafe_b64encode(json.dumps([item["saved_at"], item["id"]]).encode()).decode().rstrip("=")


def decode_cursor(cursor):
    try:
        stamp, identifier = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0 or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError
        return stamp, identifier
    except (ValueError, TypeError, KeyError, OverflowError):
        raise HTTPException(422, "Invalid history page cursor.") from None


@router.get("/{identifier}")
def retrieve(identifier: str, owner_id: str = Depends(owner)):
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise HTTPException(404, "Saved hike not found.")
    try:
        row = collection().find_one({"_id": identifier, "owner": owner_id}, {"plan": 1, "saved_at": 1})
    except PyMongoError:
        raise unavailable() from None
    if row is None:
        raise HTTPException(404, "Saved hike not found.")
    return {"plan": row["plan"], "saved_at": row["saved_at"]}


def setup_indexes():
    """Run once after configuring Atlas: python -m backend.history."""
    collection().create_index([("owner", 1), ("saved_at", -1), ("_id", -1)])
    print("Atlas connected; hike history index is ready.")


def verify_round_trip():
    """Real Atlas write/list/read with a unique owner; delete only our inserted ID."""
    token = secrets.token_hex(32)
    owner_id = hashlib.sha256(token.encode()).hexdigest()
    plan = {"request": "ATLAS INTEGRATION CHECK (temporary)",
            "trail": {"name": "Integration check only", "coords": [[42, -83], [42.01, -83]]},
            "warnings": ["Integration test only; -15F. Not a hiking recommendation."],
            "hike_window": {"min_temp_f": -15}}
    coll, identifier = collection(), None
    try:
        result = save(Snapshot(plan=plan), owner_id)
        identifier = result["id"]
        assert retrieve(identifier, owner_id)["plan"] == plan
        assert any(row["id"] == identifier for row in listing(20, None, owner_id)["items"])
    finally:
        # Even an ambiguous write timeout can only have created a record under
        # this fresh random owner. Never delete by a shared request label.
        query = {"owner": owner_id}
        if identifier:
            query["_id"] = identifier
        coll.delete_many(query)
    print("Atlas save/list/retrieve verified; temporary test records removed.")


if __name__ == "__main__":
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    try:
        setup_indexes()
        verify_round_trip()
    except (HTTPException, PyMongoError, ValueError, AssertionError):
        raise SystemExit("Atlas check failed. Check MONGODB_URI, database user, and network access list.")
