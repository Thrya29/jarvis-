"""Daemon entrypoint: wires config, logging, locking and the API server together."""

from __future__ import annotations

import logging

import uvicorn

from jarvis import __version__
from jarvis.core.audit import AuditLog
from jarvis.core.config import Settings
from jarvis.core.instance import InstanceLock, load_or_create_token
from jarvis.core.paths import AppPaths
from jarvis.server.app import create_app

log = logging.getLogger(__name__)


def run_daemon(settings: Settings, paths: AppPaths) -> None:
    with InstanceLock(paths.lock_file):
        token = load_or_create_token(paths.token_file)
        audit = AuditLog(paths.audit_log)
        app = create_app(settings, token)
        audit.record("daemon.start", version=__version__)
        log.info(
            "JARVIS %s listening on http://%s:%d",
            __version__,
            settings.server.host,
            settings.server.port,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=settings.server.host,
                port=settings.server.port,
                log_config=None,
                access_log=False,
                ws_max_size=4 * 1024 * 1024,
            )
        )
        try:
            server.run()
        finally:
            audit.record("daemon.stop")
            log.info("JARVIS stopped")
