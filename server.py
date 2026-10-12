"""Loopback-only HTTP API for the video-review workflow."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from service import APIError, ReviewService


class ReviewAPI:
    def __init__(self, service: ReviewService) -> None:
        self.service = service

    def dispatch(
        self, method: str, target: str, payload: dict[str, object] | None = None
    ) -> tuple[int, dict[str, object] | list[dict[str, object]]]:
        path = urlsplit(target).path
        parts = [unquote(part) for part in path.split("/") if part]
        body = payload or {}

        try:
            if method == "POST" and parts == ["campaigns"]:
                return 201, self.service.create_campaign(
                    body.get("campaign_id"), body.get("name"), body.get("required_pieces")
                )
            if method == "GET" and len(parts) == 2 and parts[0] == "campaigns":
                return 200, self.service.get_campaign(parts[1])
            if (
                method == "POST"
                and len(parts) == 3
                and parts[0] == "campaigns"
                and parts[2] == "deliveries"
            ):
                return 201, self.service.create_delivery(
                    parts[1], body.get("delivery_id")
                )
            if method == "GET" and len(parts) == 2 and parts[0] == "deliveries":
                return 200, self.service.get_delivery(parts[1])
            if (
                method == "POST"
                and len(parts) == 3
                and parts[0] == "deliveries"
                and parts[2] == "approve"
            ):
                return 200, self.service.approve_delivery(
                    parts[1], body.get("reviewer_id")
                )
            if (
                method == "POST"
                and len(parts) == 5
                and parts[0] == "deliveries"
                and parts[2] == "pieces"
                and parts[4] == "versions"
            ):
                return 201, self.service.create_version(
                    parts[1],
                    parts[3],
                    body.get("version_id"),
                    body.get("asset_ref"),
                    body.get("submitted_by"),
                    body.get("duration_seconds"),
                )
            if (
                method == "GET"
                and len(parts) == 6
                and parts[0] == "deliveries"
                and parts[2] == "pieces"
                and parts[4] == "versions"
            ):
                return 200, self.service.get_version(parts[1], parts[3], parts[5])
            if (
                method == "POST"
                and len(parts) == 7
                and parts[0] == "deliveries"
                and parts[2] == "pieces"
                and parts[4] == "versions"
                and parts[6] == "approve"
            ):
                return 200, self.service.approve_version(
                    parts[1], parts[3], parts[5], body.get("reviewer_id")
                )
            if (
                len(parts) == 7
                and parts[0] == "deliveries"
                and parts[2] == "pieces"
                and parts[4] == "versions"
                and parts[6] == "comments"
            ):
                if parts[3] != "video":
                    raise APIError(404, "comments are only available for video versions")
                if method == "GET":
                    return 200, self.service.list_comments(parts[1], parts[5])
                if method == "POST":
                    return 201, self.service.add_comment(
                        parts[1],
                        parts[5],
                        body.get("comment_id"),
                        body.get("reviewer_id"),
                        body.get("seconds"),
                        body.get("body"),
                    )
        except APIError as exc:
            return exc.status, exc.as_dict()
        return 404, {"error": "route not found"}


def make_handler(api: ReviewAPI) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ContyVideoReview/1.0"

        def _send_json(self, status: int, body: object) -> None:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _read_json(self) -> dict[str, object]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise APIError(400, "invalid Content-Length") from exc
            if length < 0:
                raise APIError(400, "invalid Content-Length")
            if length > 1_000_000:
                raise APIError(413, "request body is too large")
            raw = self.rfile.read(length)
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise APIError(400, "request body must be valid JSON") from exc
            if not isinstance(decoded, dict):
                raise APIError(400, "request body must be a JSON object")
            return decoded

        def _handle(self, method: str) -> None:
            try:
                payload = self._read_json() if method == "POST" else None
                status, result = api.dispatch(method, self.path, payload)
                self._send_json(status, result)
            except APIError as exc:
                self._send_json(exc.status, exc.as_dict())
            except Exception:
                self._send_json(500, {"error": "internal server error"})

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def serve(database_path: str, port: int = 8765) -> None:
    api = ReviewAPI(ReviewService(database_path))
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(api))
    print(f"video-review API listening on http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local video-review API")
    parser.add_argument("--database", default="data/video_review.sqlite3")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.database, args.port)


if __name__ == "__main__":
    main()
