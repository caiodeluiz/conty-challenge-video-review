"""SQLite-backed video-review workflow for the local Conty challenge demo."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


PIECES = ("roteiro", "video", "capa", "legenda")


class APIError(Exception):
    def __init__(self, status: int, message: str, **details: object) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, object]:
        return {"error": self.message, **self.details}


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    required_pieces_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS piece_versions (
    version_id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id),
    piece_type TEXT NOT NULL CHECK (piece_type IN ('roteiro', 'video', 'capa', 'legenda')),
    sequence_no INTEGER NOT NULL,
    asset_ref TEXT NOT NULL,
    duration_seconds REAL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved')),
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    UNIQUE (delivery_id, piece_type, sequence_no)
);

CREATE INDEX IF NOT EXISTS piece_versions_delivery_piece_sequence
    ON piece_versions (delivery_id, piece_type, sequence_no DESC);

CREATE TABLE IF NOT EXISTS version_reviews (
    review_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES piece_versions(version_id),
    decision TEXT NOT NULL CHECK (decision = 'approved'),
    reviewer_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (version_id, decision)
);

CREATE TABLE IF NOT EXISTS video_comments (
    comment_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES piece_versions(version_id),
    reviewer_id TEXT NOT NULL,
    seconds REAL NOT NULL CHECK (seconds >= 0),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS video_comments_version_time
    ON video_comments (version_id, created_at, comment_id);

CREATE TABLE IF NOT EXISTS delivery_events (
    event_no INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('approved', 'approval_invalidated')),
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise APIError(400, f"{field} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise APIError(400, f"{field} must be valid UTF-8 text") from exc
    return value.strip()


def _identifier(value: object, field: str) -> str:
    normalized = _text(value, field)
    if "/" in normalized:
        raise APIError(400, f"{field} must not contain '/'")
    return normalized


def _piece(value: object) -> str:
    piece = _identifier(value, "piece_type")
    if piece not in PIECES:
        raise APIError(400, f"piece_type must be one of: {', '.join(PIECES)}")
    return piece


def _seconds(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise APIError(400, f"{field} must be a finite non-negative number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise APIError(400, f"{field} must be a finite non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise APIError(400, f"{field} must be a finite non-negative number")
    return number


def _duration(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise APIError(400, "duration_seconds must be a finite non-negative number")
    try:
        duration = float(value)
    except OverflowError as exc:
        raise APIError(400, "duration_seconds must be a finite non-negative number") from exc
    if not math.isfinite(duration) or duration < 0:
        raise APIError(400, "duration_seconds must be a finite non-negative number")
    return duration


class ReviewService:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path == ":memory:":
            raise ValueError("a persistent database path is required")
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.executescript(SCHEMA)
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path, timeout=10.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        started = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            started = True
            yield connection
            connection.commit()
        except Exception:
            if started:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _read_connection(self) -> sqlite3.Connection:
        return self._connect()

    @staticmethod
    def _campaign(connection: sqlite3.Connection, campaign_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()
        if row is None:
            raise APIError(404, "campaign not found", campaign_id=campaign_id)
        return row

    @staticmethod
    def _delivery(connection: sqlite3.Connection, delivery_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        if row is None:
            raise APIError(404, "delivery not found", delivery_id=delivery_id)
        return row

    @staticmethod
    def _version(
        connection: sqlite3.Connection,
        delivery_id: str,
        piece_type: str,
        version_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT * FROM piece_versions
               WHERE delivery_id = ? AND piece_type = ? AND version_id = ?""",
            (delivery_id, piece_type, version_id),
        ).fetchone()
        if row is None:
            raise APIError(
                404,
                "piece version not found",
                delivery_id=delivery_id,
                piece_type=piece_type,
                version_id=version_id,
            )
        return row

    @staticmethod
    def _required_pieces(
        connection: sqlite3.Connection, delivery_id: str
    ) -> list[str]:
        delivery = ReviewService._delivery(connection, delivery_id)
        campaign = ReviewService._campaign(connection, delivery["campaign_id"])
        return json.loads(campaign["required_pieces_json"])

    @staticmethod
    def _current_versions(
        connection: sqlite3.Connection, delivery_id: str
    ) -> tuple[dict[str, sqlite3.Row], list[sqlite3.Row]]:
        rows = connection.execute(
            """SELECT * FROM piece_versions
               WHERE delivery_id = ?
               ORDER BY piece_type, sequence_no DESC""",
            (delivery_id,),
        ).fetchall()
        current: dict[str, sqlite3.Row] = {}
        for row in rows:
            current.setdefault(row["piece_type"], row)
        return current, rows

    @staticmethod
    def _missing_required(
        required_pieces: list[str], current: dict[str, sqlite3.Row]
    ) -> list[str]:
        return [
            piece
            for piece in required_pieces
            if piece not in current or current[piece]["status"] != "approved"
        ]

    @staticmethod
    def _last_delivery_event(
        connection: sqlite3.Connection, delivery_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT * FROM delivery_events
               WHERE delivery_id = ? ORDER BY event_no DESC LIMIT 1""",
            (delivery_id,),
        ).fetchone()

    @staticmethod
    def _is_approved(
        connection: sqlite3.Connection,
        delivery_id: str,
        required_pieces: list[str],
        current: dict[str, sqlite3.Row],
    ) -> bool:
        last_event = ReviewService._last_delivery_event(connection, delivery_id)
        return (
            last_event is not None
            and last_event["event_type"] == "approved"
            and not ReviewService._missing_required(required_pieces, current)
        )

    @staticmethod
    def _has_approved_event(
        connection: sqlite3.Connection, delivery_id: str
    ) -> bool:
        return (
            connection.execute(
                """SELECT 1 FROM delivery_events
                   WHERE delivery_id = ? AND event_type = 'approved' LIMIT 1""",
                (delivery_id,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _pending_revisions(
        connection: sqlite3.Connection,
        delivery_id: str,
        current: dict[str, sqlite3.Row] | None = None,
    ) -> list[dict[str, str]]:
        latest_approval = connection.execute(
            """SELECT COALESCE(MAX(event_no), 0) AS event_no FROM delivery_events
               WHERE delivery_id = ? AND event_type = 'approved'""",
            (delivery_id,),
        ).fetchone()["event_no"]
        rows = connection.execute(
            """SELECT details_json FROM delivery_events
               WHERE delivery_id = ? AND event_no > ?
                 AND event_type = 'approval_invalidated'
               ORDER BY event_no""",
            (delivery_id, latest_approval),
        ).fetchall()
        targets: dict[str, str] = {}
        for row in rows:
            details = json.loads(row["details_json"])
            piece = details.get("piece_type")
            version_id = details.get("version_id")
            if piece in PIECES and isinstance(version_id, str):
                targets[piece] = version_id

        if current is None:
            current, _ = ReviewService._current_versions(connection, delivery_id)
        pending: list[dict[str, str]] = []
        for piece in sorted(targets):
            version_id = targets[piece]
            version = current.get(piece)
            if (
                version is not None
                and version["version_id"] == version_id
                and version["status"] != "approved"
            ):
                pending.append({"piece_type": piece, "version_id": version_id})
        return pending

    @staticmethod
    def _record_delivery_event(
        connection: sqlite3.Connection,
        delivery_id: str,
        event_type: str,
        actor_id: str,
        reason: str,
        details: dict[str, object],
    ) -> None:
        connection.execute(
            """INSERT INTO delivery_events
               (event_id, delivery_id, event_type, actor_id, reason, details_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                delivery_id,
                event_type,
                actor_id,
                reason,
                json.dumps(details, sort_keys=True, separators=(",", ":")),
                utc_now(),
            ),
        )

    def create_campaign(
        self, campaign_id: object, name: object, required_pieces: object
    ) -> dict[str, object]:
        campaign_key = _identifier(campaign_id, "campaign_id")
        campaign_name = _text(name, "name")
        if not isinstance(required_pieces, list):
            raise APIError(400, "required_pieces must be a list")
        pieces = [_piece(value) for value in required_pieces]
        if len(pieces) != len(set(pieces)):
            raise APIError(400, "required_pieces must not contain duplicates")
        try:
            with self._transaction() as connection:
                connection.execute(
                    """INSERT INTO campaigns
                       (campaign_id, name, required_pieces_json, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        campaign_key,
                        campaign_name,
                        json.dumps(pieces, separators=(",", ":")),
                        utc_now(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise APIError(409, "campaign_id already exists", campaign_id=campaign_key) from exc
        return self.get_campaign(campaign_key)

    def get_campaign(self, campaign_id: object) -> dict[str, object]:
        campaign_key = _identifier(campaign_id, "campaign_id")
        connection = self._read_connection()
        try:
            row = self._campaign(connection, campaign_key)
            return {
                "campaign_id": row["campaign_id"],
                "name": row["name"],
                "required_pieces": json.loads(row["required_pieces_json"]),
                "created_at": row["created_at"],
            }
        finally:
            connection.close()

    def create_delivery(
        self, campaign_id: object, delivery_id: object
    ) -> dict[str, object]:
        campaign_key = _identifier(campaign_id, "campaign_id")
        delivery_key = _identifier(delivery_id, "delivery_id")
        try:
            with self._transaction() as connection:
                self._campaign(connection, campaign_key)
                connection.execute(
                    "INSERT INTO deliveries (delivery_id, campaign_id, created_at) VALUES (?, ?, ?)",
                    (delivery_key, campaign_key, utc_now()),
                )
        except sqlite3.IntegrityError as exc:
            raise APIError(409, "delivery_id already exists", delivery_id=delivery_key) from exc
        return self.get_delivery(delivery_key)

    def create_version(
        self,
        delivery_id: object,
        piece_type: object,
        version_id: object,
        asset_ref: object,
        submitted_by: object,
        duration_seconds: object = None,
    ) -> dict[str, object]:
        delivery_key = _identifier(delivery_id, "delivery_id")
        piece = _piece(piece_type)
        version_key = _identifier(version_id, "version_id")
        asset = _text(asset_ref, "asset_ref")
        submitter = _identifier(submitted_by, "submitted_by")
        duration = _duration(duration_seconds)
        try:
            with self._transaction() as connection:
                self._delivery(connection, delivery_key)
                has_approval_history = self._has_approved_event(
                    connection, delivery_key
                )
                row = connection.execute(
                    """SELECT COALESCE(MAX(sequence_no), 0) AS n FROM piece_versions
                       WHERE delivery_id = ? AND piece_type = ?""",
                    (delivery_key, piece),
                ).fetchone()
                sequence_no = int(row["n"]) + 1
                submitted_at = utc_now()
                connection.execute(
                    """INSERT INTO piece_versions
                       (version_id, delivery_id, piece_type, sequence_no, asset_ref,
                        duration_seconds, status, submitted_by, submitted_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    (
                        version_key,
                        delivery_key,
                        piece,
                        sequence_no,
                        asset,
                        duration,
                        submitter,
                        submitted_at,
                    ),
                )
                # By the user's brand-protection decision, any version added
                # after first approval opens a revision, including first
                # optional inclusion and additions while already suspended.
                # An absent optional piece still cannot block initial approval.
                if has_approval_history:
                    self._record_delivery_event(
                        connection,
                        delivery_key,
                        "approval_invalidated",
                        submitter,
                        "piece_version_added_after_approval",
                        {"piece_type": piece, "version_id": version_key},
                    )
        except sqlite3.IntegrityError as exc:
            raise APIError(409, "version_id already exists", version_id=version_key) from exc
        return self.get_version(delivery_key, piece, version_key)

    def get_version(
        self, delivery_id: object, piece_type: object, version_id: object
    ) -> dict[str, object]:
        delivery_key = _identifier(delivery_id, "delivery_id")
        piece = _piece(piece_type)
        version_key = _identifier(version_id, "version_id")
        connection = self._read_connection()
        try:
            row = self._version(connection, delivery_key, piece, version_key)
            return self._version_dict(row)
        finally:
            connection.close()

    @staticmethod
    def _version_dict(row: sqlite3.Row) -> dict[str, object]:
        return {
            "version_id": row["version_id"],
            "piece_type": row["piece_type"],
            "sequence": row["sequence_no"],
            "asset_ref": row["asset_ref"],
            "duration_seconds": row["duration_seconds"],
            "status": row["status"],
            "submitted_by": row["submitted_by"],
            "submitted_at": row["submitted_at"],
            "approved_by": row["approved_by"],
            "approved_at": row["approved_at"],
        }

    def approve_version(
        self,
        delivery_id: object,
        piece_type: object,
        version_id: object,
        reviewer_id: object,
    ) -> dict[str, object]:
        delivery_key = _identifier(delivery_id, "delivery_id")
        piece = _piece(piece_type)
        version_key = _identifier(version_id, "version_id")
        reviewer = _identifier(reviewer_id, "reviewer_id")
        with self._transaction() as connection:
            self._version(connection, delivery_key, piece, version_key)
            current, _ = self._current_versions(connection, delivery_key)
            current_version = current[piece]
            if current_version["version_id"] != version_key:
                raise APIError(
                    409,
                    "cannot approve a stale version",
                    current_version_id=current_version["version_id"],
                )
            connection.execute(
                """INSERT OR IGNORE INTO version_reviews
                   (review_id, version_id, decision, reviewer_id, created_at)
                   VALUES (?, ?, 'approved', ?, ?)""",
                (str(uuid.uuid4()), version_key, reviewer, utc_now()),
            )
            review = connection.execute(
                "SELECT reviewer_id, created_at FROM version_reviews WHERE version_id = ?",
                (version_key,),
            ).fetchone()
            connection.execute(
                """UPDATE piece_versions SET status = 'approved', approved_by = ?, approved_at = ?
                   WHERE version_id = ?""",
                (review["reviewer_id"], review["created_at"], version_key),
            )

            required = self._required_pieces(connection, delivery_key)
            current, _ = self._current_versions(connection, delivery_key)
            last_event = self._last_delivery_event(connection, delivery_key)
            if (
                last_event is not None
                and last_event["event_type"] == "approval_invalidated"
                and not self._missing_required(required, current)
                and not self._pending_revisions(connection, delivery_key, current)
            ):
                self._record_delivery_event(
                    connection,
                    delivery_key,
                    "approved",
                    reviewer,
                    "required_current_versions_approved",
                    {"trigger_version_id": version_key},
                )
        return {
            "version": self.get_version(delivery_key, piece, version_key),
            "delivery": self.get_delivery(delivery_key),
        }

    def approve_delivery(
        self, delivery_id: object, reviewer_id: object
    ) -> dict[str, object]:
        delivery_key = _identifier(delivery_id, "delivery_id")
        reviewer = _identifier(reviewer_id, "reviewer_id")
        with self._transaction() as connection:
            self._delivery(connection, delivery_key)
            required = self._required_pieces(connection, delivery_key)
            current, _ = self._current_versions(connection, delivery_key)
            missing = self._missing_required(required, current)
            pending_revisions = self._pending_revisions(
                connection, delivery_key, current
            )
            if missing:
                raise APIError(
                    409,
                    "required pieces need an approved current version",
                    pending_pieces=missing,
                )
            if pending_revisions:
                raise APIError(
                    409,
                    "revised pieces need an approved current version",
                    pending_revisions=pending_revisions,
                )
            if not self._is_approved(connection, delivery_key, required, current):
                self._record_delivery_event(
                    connection,
                    delivery_key,
                    "approved",
                    reviewer,
                    "all_required_current_versions_approved",
                    {
                        "versions": {
                            piece: current[piece]["version_id"]
                            for piece in required
                        }
                    },
                )
        return self.get_delivery(delivery_key)

    def add_comment(
        self,
        delivery_id: object,
        version_id: object,
        comment_id: object,
        reviewer_id: object,
        seconds: object,
        body: object,
    ) -> dict[str, object]:
        delivery_key = _identifier(delivery_id, "delivery_id")
        version_key = _identifier(version_id, "version_id")
        comment_key = _identifier(comment_id, "comment_id")
        reviewer = _identifier(reviewer_id, "reviewer_id")
        timestamp = _seconds(seconds, "seconds")
        _text(body, "body")
        try:
            with self._transaction() as connection:
                version = self._version(connection, delivery_key, "video", version_key)
                duration = version["duration_seconds"]
                if duration is not None and timestamp > duration:
                    raise APIError(
                        400,
                        "seconds must not exceed duration_seconds",
                        duration_seconds=duration,
                    )
                connection.execute(
                    """INSERT INTO video_comments
                       (comment_id, version_id, reviewer_id, seconds, body, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (comment_key, version_key, reviewer, timestamp, body, utc_now()),
                )
        except sqlite3.IntegrityError as exc:
            raise APIError(409, "comment_id already exists", comment_id=comment_key) from exc
        return self.get_comment(version_key, comment_key)

    def get_comment(self, version_id: object, comment_id: object) -> dict[str, object]:
        version_key = _identifier(version_id, "version_id")
        comment_key = _identifier(comment_id, "comment_id")
        connection = self._read_connection()
        try:
            row = connection.execute(
                """SELECT * FROM video_comments
                   WHERE version_id = ? AND comment_id = ?""",
                (version_key, comment_key),
            ).fetchone()
            if row is None:
                raise APIError(404, "comment not found", comment_id=comment_key)
            return self._comment_dict(row)
        finally:
            connection.close()

    @staticmethod
    def _comment_dict(row: sqlite3.Row) -> dict[str, object]:
        return {
            "comment_id": row["comment_id"],
            "version_id": row["version_id"],
            "reviewer_id": row["reviewer_id"],
            "seconds": row["seconds"],
            "body": row["body"],
            "created_at": row["created_at"],
        }

    def list_comments(self, delivery_id: object, version_id: object) -> list[dict[str, object]]:
        delivery_key = _identifier(delivery_id, "delivery_id")
        version_key = _identifier(version_id, "version_id")
        connection = self._read_connection()
        try:
            self._version(connection, delivery_key, "video", version_key)
            rows = connection.execute(
                """SELECT * FROM video_comments WHERE version_id = ?
                   ORDER BY created_at, comment_id""",
                (version_key,),
            ).fetchall()
            return [self._comment_dict(row) for row in rows]
        finally:
            connection.close()

    def get_delivery(self, delivery_id: object) -> dict[str, object]:
        delivery_key = _identifier(delivery_id, "delivery_id")
        connection = self._read_connection()
        try:
            connection.execute("BEGIN")
            delivery = self._delivery(connection, delivery_key)
            campaign = self._campaign(connection, delivery["campaign_id"])
            required = json.loads(campaign["required_pieces_json"])
            current, version_rows = self._current_versions(connection, delivery_key)
            missing = self._missing_required(required, current)
            pending_revisions = self._pending_revisions(
                connection, delivery_key, current
            )
            approved = self._is_approved(connection, delivery_key, required, current)
            events = connection.execute(
                """SELECT * FROM delivery_events WHERE delivery_id = ?
                   ORDER BY event_no""",
                (delivery_key,),
            ).fetchall()
            versions = [self._version_dict(row) for row in version_rows]
            versions.sort(
                key=lambda version: (version["submitted_at"], version["version_id"])
            )
            event_data = [
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "actor_id": row["actor_id"],
                    "reason": row["reason"],
                    "details": json.loads(row["details_json"]),
                    "created_at": row["created_at"],
                }
                for row in events
            ]
            result = {
                "delivery_id": delivery_key,
                "campaign_id": delivery["campaign_id"],
                "required_pieces": required,
                "approved": approved,
                "pending_pieces": missing,
                "pending_revisions": pending_revisions,
                "current_versions": {
                    piece: self._version_dict(row) for piece, row in current.items()
                },
                "versions": versions,
                "events": event_data,
                "created_at": delivery["created_at"],
                "status": (
                    "approved"
                    if approved
                    else "awaiting_required_versions"
                    if missing
                    else "awaiting_piece_revisions"
                    if pending_revisions
                    else "ready_for_approval"
                ),
            }
            connection.commit()
            return result
        finally:
            connection.close()
