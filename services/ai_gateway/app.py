"""Minimal production-shaped HTTP boundary for health and route decisions."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from packages.contracts import Product

from .config import ConfigurationError, load_config
from .router import ModelRouter, RouteRequest, RoutingError

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("UWO_GATEWAY_CONFIG", ROOT / "infrastructure/config/ai-gateway.json"))


class GatewayHandler(BaseHTTPRequestHandler):
    router: ModelRouter
    server_version = "UWOAIGateway/0.1"

    def _respond(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        if self.path == "/healthz":
            self._respond(HTTPStatus.OK, {"status": "ok", "service": "ai-gateway"})
            return
        self._respond(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "message": "route not found"}})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        if self.path != "/v1/route":
            self._respond(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "message": "route not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16_384:
                raise ValueError("request body must be between 1 and 16384 bytes")
            body = json.loads(self.rfile.read(length))
            request = RouteRequest(
                tenant_id=body["tenant_id"],
                product=Product(body["product"]),
                model=body["model"],
                region=body["region"],
            )
            if not all(isinstance(value, str) and value for value in (request.tenant_id, request.model, request.region)):
                raise ValueError("tenant_id, model, and region must be non-empty strings")
            result = self.router.route(request)
        except RoutingError as exc:
            self._respond(HTTPStatus.FORBIDDEN, {"error": {"code": exc.code, "message": str(exc)}})
            return
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": {"code": "invalid_request", "message": str(exc)}})
            return
        self._respond(HTTPStatus.OK, asdict(result))

    def log_message(self, format: str, *args: object) -> None:
        print(json.dumps({"event": "http_request", "message": format % args}))


def create_server(host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    config = load_config(CONFIG_PATH)
    GatewayHandler.router = ModelRouter(config)
    return ThreadingHTTPServer((host, port), GatewayHandler)


def main() -> None:
    try:
        server = create_server(os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080")))
    except (ConfigurationError, ValueError) as exc:
        raise SystemExit(f"gateway startup failed: {exc}") from exc
    server.serve_forever()


if __name__ == "__main__":
    main()
