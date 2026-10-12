from __future__ import annotations

import http.client
import json
import math
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from server import ReviewAPI, make_handler
from service import APIError, ReviewService


class ReviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Path(self.temp_dir.name) / "review.sqlite3"
        self.service = ReviewService(database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def make_delivery(
        self, required: list[str] | None = None, delivery_id: str = "delivery-1"
    ) -> str:
        self.service.create_campaign(
            "campaign-1", "Spring campaign", ["video"] if required is None else required
        )
        self.service.create_delivery("campaign-1", delivery_id)
        return delivery_id

    def add_version(
        self,
        delivery_id: str,
        piece: str,
        version_id: str,
        duration: float | None = None,
    ) -> dict[str, object]:
        return self.service.create_version(
            delivery_id,
            piece,
            version_id,
            f"fake://assets/{version_id}",
            "creator-1",
            duration,
        )

    def approve_piece(self, delivery_id: str, piece: str, version_id: str) -> None:
        self.service.approve_version(delivery_id, piece, version_id, "reviewer-1")

    def test_video_only_campaign_does_not_require_script(self) -> None:
        self.make_delivery(["video"])
        self.add_version("delivery-1", "video", "video-v1")
        self.approve_piece("delivery-1", "video", "video-v1")

        result = self.service.approve_delivery("delivery-1", "brand-1")

        self.assertTrue(result["approved"])
        self.assertEqual(result["required_pieces"], ["video"])
        self.assertNotIn("roteiro", result["current_versions"])
        self.assertNotIn("capa", result["current_versions"])

    def test_approval_fails_with_clear_list_when_required_piece_is_missing(self) -> None:
        self.make_delivery(["roteiro", "video"])
        self.add_version("delivery-1", "video", "video-v1")
        self.approve_piece("delivery-1", "video", "video-v1")

        with self.assertRaises(APIError) as raised:
            self.service.approve_delivery("delivery-1", "brand-1")

        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.details["pending_pieces"], ["roteiro"])
        self.assertFalse(self.service.get_delivery("delivery-1")["approved"])

    def test_latest_version_replaces_old_for_the_current_piece_gate(self) -> None:
        self.make_delivery(["video"])
        self.add_version("delivery-1", "video", "video-v1")
        self.approve_piece("delivery-1", "video", "video-v1")
        self.add_version("delivery-1", "video", "video-v2")

        current = self.service.get_delivery("delivery-1")
        self.assertEqual(current["current_versions"]["video"]["version_id"], "video-v2")
        self.assertEqual(current["current_versions"]["video"]["status"], "pending")
        self.assertEqual(current["versions"][0]["version_id"], "video-v1")
        self.assertEqual(current["versions"][0]["status"], "approved")
        with self.assertRaises(APIError) as raised:
            self.service.approve_delivery("delivery-1", "brand-1")
        self.assertEqual(raised.exception.details["pending_pieces"], ["video"])

    def test_new_required_version_invalidates_and_then_restores_delivery_approval(self) -> None:
        self.make_delivery(["video"])
        self.add_version("delivery-1", "video", "video-v1")
        self.approve_piece("delivery-1", "video", "video-v1")
        self.service.approve_delivery("delivery-1", "brand-1")

        self.add_version("delivery-1", "video", "video-v2")
        invalidated = self.service.get_delivery("delivery-1")
        self.assertFalse(invalidated["approved"])
        self.assertEqual(invalidated["events"][-1]["event_type"], "approval_invalidated")

        self.approve_piece("delivery-1", "video", "video-v2")
        restored = self.service.get_delivery("delivery-1")
        self.assertTrue(restored["approved"])
        self.assertEqual(restored["events"][-1]["event_type"], "approved")

    def test_approving_a_stale_version_never_approves_the_current_version(self) -> None:
        self.make_delivery(["video"])
        self.add_version("delivery-1", "video", "video-v1")
        self.add_version("delivery-1", "video", "video-v2")

        with self.assertRaises(APIError) as raised:
            self.approve_piece("delivery-1", "video", "video-v1")

        self.assertEqual(raised.exception.status, 409)
        result = self.service.get_delivery("delivery-1")
        self.assertEqual(result["current_versions"]["video"]["version_id"], "video-v2")
        self.assertEqual(result["current_versions"]["video"]["status"], "pending")
        self.assertEqual(
            next(v for v in result["versions"] if v["version_id"] == "video-v1")["status"],
            "pending",
        )
        self.assertFalse(result["approved"])

    def test_delivery_view_uses_one_snapshot_during_concurrent_version_write(self) -> None:
        self.make_delivery(["video"])
        self.add_version("delivery-1", "video", "video-v1")
        journal = sqlite3.connect(self.service.database_path)
        try:
            journal.execute("PRAGMA journal_mode = WAL")
        finally:
            journal.close()

        original_current_versions = self.service._current_versions
        version_inserted = False

        def insert_during_read(connection: sqlite3.Connection, delivery_id: str):
            nonlocal version_inserted
            if not version_inserted:
                version_inserted = True
                self.add_version("delivery-1", "video", "video-v2")
            return original_current_versions(connection, delivery_id)

        self.service._current_versions = insert_during_read
        try:
            snapshot = self.service.get_delivery("delivery-1")
        finally:
            del self.service._current_versions

        self.assertEqual(snapshot["current_versions"]["video"]["version_id"], "video-v1")
        self.assertEqual(
            self.service.get_delivery("delivery-1")["current_versions"]["video"]["version_id"],
            "video-v2",
        )

    def test_video_and_included_cover_can_be_approved_together(self) -> None:
        self.make_delivery(["video"])
        self.add_version("delivery-1", "video", "video-v1")
        self.add_version("delivery-1", "capa", "cover-v1")
        self.approve_piece("delivery-1", "video", "video-v1")
        self.approve_piece("delivery-1", "capa", "cover-v1")

        approved = self.service.approve_delivery("delivery-1", "brand-1")
        self.assertTrue(approved["approved"])
        self.assertEqual(approved["required_pieces"], ["video"])
        self.assertEqual(approved["current_versions"]["capa"]["version_id"], "cover-v1")

    def test_first_optional_piece_inclusion_after_approval_requires_review(self) -> None:
        self.make_delivery(["video"])
        self.add_version("delivery-1", "video", "video-v1")
        self.approve_piece("delivery-1", "video", "video-v1")
        self.service.approve_delivery("delivery-1", "brand-1")

        self.add_version("delivery-1", "capa", "cover-first-inclusion")

        result = self.service.get_delivery("delivery-1")
        self.assertFalse(result["approved"])
        self.assertEqual(
            result["pending_revisions"],
            [{"piece_type": "capa", "version_id": "cover-first-inclusion"}],
        )
        self.assertEqual(result["current_versions"]["capa"]["status"], "pending")
        with self.assertRaises(APIError) as raised:
            self.service.approve_delivery("delivery-1", "brand-1")
        self.assertEqual(raised.exception.status, 409)
        self.approve_piece("delivery-1", "capa", "cover-first-inclusion")
        self.assertTrue(self.service.get_delivery("delivery-1")["approved"])

        self.add_version("delivery-1", "capa", "cover-second-version")
        replaced = self.service.get_delivery("delivery-1")
        self.assertFalse(replaced["approved"])
        self.assertEqual(
            replaced["pending_revisions"],
            [{"piece_type": "capa", "version_id": "cover-second-version"}],
        )
        self.approve_piece("delivery-1", "capa", "cover-second-version")
        self.assertTrue(self.service.get_delivery("delivery-1")["approved"])

    def test_replacing_optional_cover_suspends_approval_until_current_cover_is_approved(self) -> None:
        self.make_delivery(["video"])
        self.add_version("delivery-1", "video", "video-v1")
        self.add_version("delivery-1", "capa", "cover-v1")
        self.approve_piece("delivery-1", "video", "video-v1")
        self.approve_piece("delivery-1", "capa", "cover-v1")
        self.service.approve_delivery("delivery-1", "brand-1")

        self.add_version("delivery-1", "capa", "cover-v2")

        pending = self.service.get_delivery("delivery-1")
        self.assertFalse(pending["approved"])
        self.assertEqual(pending["status"], "awaiting_piece_revisions")
        self.assertEqual(
            pending["pending_revisions"],
            [{"piece_type": "capa", "version_id": "cover-v2"}],
        )
        with self.assertRaises(APIError) as raised:
            self.service.approve_delivery("delivery-1", "brand-1")
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(
            raised.exception.details["pending_revisions"],
            [{"piece_type": "capa", "version_id": "cover-v2"}],
        )

        restored = self.service.approve_version(
            "delivery-1", "capa", "cover-v2", "reviewer-1"
        )
        self.assertTrue(restored["delivery"]["approved"])
        self.assertEqual(restored["delivery"]["events"][-1]["event_type"], "approved")

    def test_multiple_piece_revisions_accumulate_and_survive_restart(self) -> None:
        self.make_delivery(["video"])
        self.add_version("delivery-1", "video", "video-v1", duration=30)
        self.service.add_comment(
            "delivery-1", "video-v1", "comment-old-video", "reviewer-1", 12, "Keep this note"
        )
        self.approve_piece("delivery-1", "video", "video-v1")
        for piece, version in (
            ("capa", "cover-v1"),
            ("legenda", "caption-v1"),
        ):
            self.add_version("delivery-1", piece, version)
            self.approve_piece("delivery-1", piece, version)
        self.service.approve_delivery("delivery-1", "brand-1")

        self.add_version("delivery-1", "capa", "cover-v2")
        self.add_version("delivery-1", "legenda", "caption-v2")
        self.add_version("delivery-1", "capa", "cover-v3")
        self.add_version("delivery-1", "video", "video-v2", duration=40)

        pending = self.service.get_delivery("delivery-1")
        self.assertFalse(pending["approved"])
        self.assertEqual(
            pending["pending_revisions"],
            [
                {"piece_type": "capa", "version_id": "cover-v3"},
                {"piece_type": "legenda", "version_id": "caption-v2"},
                {"piece_type": "video", "version_id": "video-v2"},
            ],
        )
        with self.assertRaises(APIError) as raised:
            self.service.approve_delivery("delivery-1", "brand-1")
        self.assertEqual(raised.exception.status, 409)

        with self.assertRaises(APIError) as raised:
            self.approve_piece("delivery-1", "capa", "cover-v2")
        self.assertEqual(raised.exception.status, 409)

        self.approve_piece("delivery-1", "video", "video-v2")
        self.assertFalse(self.service.get_delivery("delivery-1")["approved"])
        self.assertEqual(
            self.service.list_comments("delivery-1", "video-v1")[0]["version_id"],
            "video-v1",
        )
        self.assertEqual(self.service.list_comments("delivery-1", "video-v2"), [])

        self.approve_piece("delivery-1", "legenda", "caption-v2")
        still_pending = self.service.get_delivery("delivery-1")
        self.assertFalse(still_pending["approved"])
        self.assertEqual(
            still_pending["pending_revisions"],
            [{"piece_type": "capa", "version_id": "cover-v3"}],
        )

        database = self.service.database_path
        self.service = ReviewService(database)
        after_restart = self.service.get_delivery("delivery-1")
        self.assertFalse(after_restart["approved"])
        self.assertEqual(after_restart["pending_revisions"], still_pending["pending_revisions"])
        with self.assertRaises(APIError) as raised:
            self.approve_piece("delivery-1", "capa", "cover-v2")
        self.assertEqual(raised.exception.status, 409)
        self.approve_piece("delivery-1", "video", "video-v2")
        self.assertFalse(self.service.get_delivery("delivery-1")["approved"])

        restored = self.service.approve_version(
            "delivery-1", "capa", "cover-v3", "reviewer-1"
        )
        self.assertTrue(restored["delivery"]["approved"])
        self.assertEqual(restored["delivery"]["pending_revisions"], [])

        self.service = ReviewService(database)
        self.assertTrue(self.service.get_delivery("delivery-1")["approved"])

    def test_comments_remain_attached_to_the_version_where_they_were_written(self) -> None:
        self.make_delivery(["video"])
        self.add_version("delivery-1", "video", "video-v1", duration=30)
        first = self.service.add_comment(
            "delivery-1", "video-v1", "comment-1", "reviewer-1", 12.5, "Trim this pause"
        )
        self.add_version("delivery-1", "video", "video-v2", duration=40)
        old_version_comment = self.service.add_comment(
            "delivery-1", "video-v1", "comment-2", "reviewer-2", 30, "At the end frame"
        )

        self.assertEqual(first["version_id"], "video-v1")
        self.assertEqual(first["seconds"], 12.5)
        self.assertEqual(old_version_comment["version_id"], "video-v1")
        self.assertEqual(
            [comment["comment_id"] for comment in self.service.list_comments("delivery-1", "video-v1")],
            ["comment-1", "comment-2"],
        )
        self.assertEqual(self.service.list_comments("delivery-1", "video-v2"), [])

    def test_comment_seconds_must_be_finite_nonnegative_and_within_known_duration(self) -> None:
        self.make_delivery(["video"])
        self.add_version("delivery-1", "video", "video-v1", duration=10)
        boundary = self.service.add_comment(
            "delivery-1", "video-v1", "at-end", "reviewer", 10, "At duration"
        )
        self.assertEqual(boundary["seconds"], 10)

        for invalid in (-0.1, math.inf, math.nan, True, 10.01):
            with self.subTest(seconds=invalid):
                with self.assertRaises(APIError):
                    self.service.add_comment(
                        "delivery-1", "video-v1", f"bad-{str(invalid)}", "reviewer", invalid, "Note"
                    )

    def test_zero_duration_allows_only_a_comment_at_second_zero(self) -> None:
        self.make_delivery(["video"])
        self.add_version("delivery-1", "video", "video-v1", duration=0)

        comment = self.service.add_comment(
            "delivery-1", "video-v1", "at-zero", "reviewer", 0, "Start"
        )
        self.assertEqual(comment["seconds"], 0)
        with self.assertRaises(APIError):
            self.service.add_comment(
                "delivery-1", "video-v1", "past-zero", "reviewer", 0.01, "Past end"
            )


class ReviewHTTPTests(unittest.TestCase):
    def test_invalid_unicode_returns_400_before_writes_and_preserves_valid_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ReviewService(Path(temp_dir) / "unicode.sqlite3")
            service.create_campaign("campaign", "Unicode 🎬 中文", ["video"])
            service.create_delivery("campaign", "delivery")
            service.create_version("delivery", "video", "video-v1", "fake://🎬", "creator", 30)
            service.approve_version("delivery", "video", "video-v1", "brand")
            service.approve_delivery("delivery", "brand")

            def snapshot():
                with sqlite3.connect(service.database_path) as connection:
                    return list(connection.iterdump())

            before = snapshot()
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ReviewAPI(service)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)

            def post(path, payload):
                connection.request("POST", path, body=json.dumps(payload).encode("utf-8"),
                                   headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, json.loads(response.read())

            try:
                for invalid in ("\ud800", "\udfff"):
                    cases = [
                        ("/campaigns", {"campaign_id": "bad-name", "name": invalid, "required_pieces": ["video"]}),
                        ("/campaigns", {"campaign_id": invalid, "name": "Name", "required_pieces": ["video"]}),
                        ("/deliveries/delivery/pieces/capa/versions", {"version_id": "bad-cover", "asset_ref": invalid, "submitted_by": "creator"}),
                        ("/deliveries/delivery/pieces/video/versions/video-v1/approve", {"reviewer_id": invalid}),
                        ("/deliveries/delivery/pieces/video/versions/video-v1/comments", {"comment_id": "bad-comment", "reviewer_id": "brand", "seconds": 3, "body": invalid}),
                    ]
                    for path, payload in cases:
                        with self.subTest(path=path, invalid=repr(invalid)):
                            status, body = post(path, payload)
                            self.assertEqual(status, 400)
                            self.assertIn("UTF-8", body["error"])
                            self.assertEqual(snapshot(), before)

                text = "  Ótimo 🎬 中文  "
                status, comment = post(
                    "/deliveries/delivery/pieces/video/versions/video-v1/comments",
                    {"comment_id": "valid-comment", "reviewer_id": "brand", "seconds": 3, "body": text},
                )
                self.assertEqual(status, 201)
                self.assertEqual(comment["body"], text)
                self.assertTrue(service.get_delivery("delivery")["approved"])
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_api_runs_on_loopback_and_exposes_campaign_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ReviewService(Path(temp_dir) / "http.sqlite3")
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ReviewAPI(service)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                payload = json.dumps(
                    {"campaign_id": "http-campaign", "name": "HTTP", "required_pieces": ["video"]}
                )
                connection.request(
                    "POST", "/campaigns", body=payload, headers={"Content-Type": "application/json"}
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 201)
                self.assertEqual(json.loads(response.read())["required_pieces"], ["video"])
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
