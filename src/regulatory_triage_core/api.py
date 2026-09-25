"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database
from .triage import TriageService


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          triage: TriageService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    triage = triage or TriageService(service.database, service.clock)
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        # ---- 优先级编排 ----
        if method == "POST" and parsed.path == "/rules":
            result = triage.register_rules(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/facts/refresh":
            result = triage.refresh_facts(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/emergencies":
            result = triage.declare_emergency(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/plans":
            result = triage.generate_plan(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "GET" and parsed.path == "/plans":
            plan_date = query.get("plan_date", [""])[0]
            if not plan_date:
                raise ValidationError("plan_date 不能为空")
            district_id = query.get("district_id", ["default"])[0]
            plan_version = query.get("plan_version", [None])[0]
            return 200, triage.get_plan(plan_date, district_id,
                                       int(plan_version) if plan_version else None)
        if method == "GET" and parsed.path == "/plans/explain":
            plan_date = query.get("plan_date", [""])[0]
            site_id = query.get("site_id", [""])[0]
            if not plan_date or not site_id:
                raise ValidationError("plan_date 与 site_id 不能为空")
            district_id = query.get("district_id", ["default"])[0]
            plan_version = query.get("plan_version", [None])[0]
            return 200, triage.explain_site(plan_date=plan_date, site_id=site_id,
                                           district_id=district_id,
                                           plan_version=int(plan_version) if plan_version else None)
        if method == "POST" and parsed.path == "/dispatches/claim":
            result = triage.claim_dispatch(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/dispatches/release":
            result = triage.release_dispatch(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/dispatches/reassign":
            result = triage.reassign_dispatch(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "GET" and parsed.path == "/dispatches":
            plan_date = query.get("plan_date", [""])[0]
            if not plan_date:
                raise ValidationError("plan_date 不能为空")
            district_id = query.get("district_id", ["default"])[0]
            status = query.get("status", [None])[0]
            return 200, triage.list_dispatches(plan_date=plan_date, district_id=district_id,
                                              status=status)
        if method == "GET" and parsed.path == "/dispatch-history":
            dispatch_id = query.get("dispatch_id", [""])[0]
            if not dispatch_id:
                raise ValidationError("dispatch_id 不能为空")
            return 200, triage.dispatch_history(dispatch_id)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    triage: TriageService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                triage=self.triage)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动环保监管优先级编排服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    service = DomainService(database)
    Handler.service = service
    Handler.triage = TriageService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
