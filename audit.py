"""Structured, rotating audit logs written by a background logging thread."""

import json
import logging
import logging.handlers
import queue
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

RUN_ID = ContextVar("pixiv_run_id", default="-")


def exception_info(exc: BaseException) -> dict:
    # Exception messages may embed proxy passwords or full request URLs.
    chain = []
    current = exc
    seen = set()
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(
            {
                "type": type(current).__name__,
                "errno": getattr(current, "errno", None),
                "ssl_verify_code": getattr(current, "verify_code", None),
            }
        )
        current = current.__cause__ or current.__context__
    return {
        "exception_type": type(exc).__name__,
        "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
        "exception_chain": chain,
        "frames": [
            {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
            for f in traceback.extract_tb(exc.__traceback__)
        ],
    }


class Audit:
    def __init__(self, directory: Path, settings):
        directory.mkdir(parents=True, exist_ok=True)
        self.boot_id = uuid4().hex[:12]
        self.secrets = [settings.cookie, settings.proxy]
        for item in settings.cookie.split(";"):
            if "=" in item:
                self.secrets.append(item.split("=", 1)[1].strip())
        if settings.proxy:
            parsed = urlsplit(settings.proxy)
            self.secrets.extend([unquote(parsed.username or ""), unquote(parsed.password or "")])
        self.secrets = sorted({s for s in self.secrets if s}, key=len, reverse=True)
        self.logger = logging.Logger("pixiv.audit." + self.boot_id, level=logging.INFO)
        self.logger.propagate = False
        self.queue = queue.SimpleQueue()
        self.logger.addHandler(logging.handlers.QueueHandler(self.queue))
        self.handler = logging.handlers.RotatingFileHandler(
            directory / "pixiv.jsonl",
            maxBytes=settings.log_max_mb * 1024 * 1024,
            backupCount=settings.log_backups,
            encoding="utf-8",
        )
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.listener = logging.handlers.QueueListener(self.queue, self.handler)
        self.listener.start()
        self.closed = False

    def emit(self, event: str, **fields):
        if self.closed:
            return
        record = {
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "boot_id": self.boot_id,
            "run_id": RUN_ID.get(),
            "event": event,
            **fields,
        }

        # Defense in depth; call sites never supply cookies, headers, raw exceptions
        # or raw chat messages. Replacing before serialization keeps JSON valid.
        def redact(value):
            if isinstance(value, str):
                for secret in self.secrets:
                    value = value.replace(secret, "[REDACTED]")
                return value
            if isinstance(value, dict):
                return {k: redact(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [redact(v) for v in value]
            return value

        self.logger.info(json.dumps(redact(record), ensure_ascii=False))

    def close(self):
        if not self.closed:
            self.closed = True
            self.listener.stop()
            self.handler.close()


class NullAudit:
    def emit(self, event, **fields):
        pass
