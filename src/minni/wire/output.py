"""JSON stdout / exit-code contract for minni wire."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlatformResult:
    platform: str
    status: str  # wired | skipped | failed
    config_path: str | None = None
    server_path: str | None = None
    agent: str | None = None
    workspace: str | None = None
    verify: dict[str, bool] | None = None
    reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "platform": self.platform,
            "status": self.status,
            "config_path": self.config_path,
            "server_path": self.server_path,
            "agent": self.agent,
            "workspace": self.workspace,
            "verify": self.verify,
            "reason": self.reason,
        }
        for key, val in self.extra.items():
            if key not in out:
                out[key] = val
        return out


@dataclass
class WireOutput:
    schema: int = 1
    status: str = "ok"
    payload_version: str | None = None
    install_root: str | None = None
    results: list[PlatformResult] = field(default_factory=list)
    gc: dict[str, Any] = field(default_factory=dict)

    def emit(self) -> int:
        doc = {
            "schema": self.schema,
            "status": self.status,
            "payload_version": self.payload_version,
            "install_root": self.install_root,
            "results": [r.to_dict() for r in self.results],
            "gc": self.gc,
        }
        print(json.dumps(doc, indent=2))
        if self.status == "dry-run":
            return 0
        if self.status == "failed":
            return 1
        if self.status == "partial":
            return 1
        wired_or_skipped = all(
            r.status in ("wired", "skipped") for r in self.results
        )
        any_failed = any(r.status == "failed" for r in self.results)
        if any_failed:
            return 1
        if wired_or_skipped:
            return 0
        return 1

    @classmethod
    def preflight_error(cls, message: str) -> int:
        out = cls(status="failed", results=[
            PlatformResult(platform="*", status="failed", reason=message),
        ])
        print(message, file=sys.stderr)
        return 2 if out.emit() == 1 else 2

    def finalize_status(self, *, dry_run: bool) -> None:
        if dry_run:
            self.status = "dry-run"
            return
        statuses = {r.status for r in self.results}
        if not self.results:
            self.status = "failed"
        elif statuses <= {"skipped"}:
            self.status = "ok"
        elif "failed" in statuses and "wired" in statuses:
            self.status = "partial"
        elif "failed" in statuses:
            self.status = "failed"
        else:
            self.status = "ok"