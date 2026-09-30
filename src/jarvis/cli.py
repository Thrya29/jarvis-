"""Command-line interface: ``jarvis run | doctor | config | secret | version``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import io
import json
import logging
import platform
import signal
import socket
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from jarvis import __version__
from jarvis.core.config import LLMProvider, Settings, load_settings, write_default_config
from jarvis.core.instance import AlreadyRunningError
from jarvis.core.logging_setup import configure_logging
from jarvis.core.paths import AppPaths, get_paths
from jarvis.core.secrets import SecretName, delete_secret, get_secret, set_secret

log = logging.getLogger("jarvis")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _load_or_exit() -> Settings:
    try:
        return load_settings()
    except ValidationError as exc:
        print(f"Invalid configuration ({get_paths().config_file}):\n{exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def cmd_run(args: argparse.Namespace, paths: AppPaths) -> int:
    from jarvis.server.daemon import run_daemon

    settings = _load_or_exit()
    configure_logging(settings.logging, paths.log_dir)
    try:
        run_daemon(settings, paths)
    except AlreadyRunningError as exc:
        log.error("%s", exc)
        return 3
    return 0


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _ollama_reachable(host: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{host.rstrip('/')}/api/tags", timeout=3) as resp:  # noqa: S310 - host is user config
            models = [m.get("name") for m in json.load(resp).get("models", [])]
        return True, f"models: {', '.join(models) or 'none pulled'}"
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return False, f"not reachable at {host} ({exc})"


def run_checks(paths: AppPaths) -> list[Check]:
    checks: list[Check] = [
        Check("python", sys.version_info[:2] == (3, 12), platform.python_version()),
        Check("windows", sys.platform == "win32", platform.platform()),
    ]
    try:
        settings = load_settings()
    except ValidationError as exc:
        checks.append(Check("config", False, str(exc).splitlines()[0]))
        return checks
    checks.append(Check("config", True, str(paths.config_file)))

    for label, d in (("data dir", paths.data_dir), ("log dir", paths.log_dir)):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            checks.append(Check(label, True, str(d)))
        except OSError as exc:
            checks.append(Check(label, False, f"{d}: {exc}"))

    provider = settings.llm.provider
    has_key = get_secret(SecretName.ANTHROPIC_API_KEY) is not None
    checks.append(
        Check(
            "anthropic key",
            has_key,
            "configured" if has_key else "missing - run `jarvis secret set ANTHROPIC_API_KEY`",
            required=provider is LLMProvider.ANTHROPIC,
        )
    )
    ok, detail = _ollama_reachable(settings.llm.ollama.host)
    checks.append(Check("ollama", ok, detail, required=provider is LLMProvider.OLLAMA))

    missing = [str(r) for r in settings.safety.allowed_roots if not r.is_dir()]
    checks.append(
        Check(
            "allowed roots",
            not missing,
            f"missing: {', '.join(missing)}"
            if missing
            else f"{len(settings.safety.allowed_roots)} folders",
        )
    )

    free = _port_free(settings.server.host, settings.server.port)
    checks.append(
        Check(
            "api port",
            free,
            f"{settings.server.host}:{settings.server.port} "
            + ("free" if free else "in use (JARVIS already running?)"),
            required=False,
        )
    )
    return checks


def cmd_doctor(args: argparse.Namespace, paths: AppPaths) -> int:
    checks = run_checks(paths)
    failed = False
    for c in checks:
        mark = "OK  " if c.ok else ("FAIL" if c.required else "WARN")
        failed |= c.required and not c.ok
        print(f"[{mark}] {c.name:<14} {c.detail}")
    return 1 if failed else 0


def cmd_config(args: argparse.Namespace, paths: AppPaths) -> int:
    if args.action == "path":
        print(paths.config_file)
    elif args.action == "init":
        existed = paths.config_file.exists()
        target = write_default_config(paths.config_file, overwrite=args.force)
        print(f"{'Kept existing' if existed and not args.force else 'Wrote'} {target}")
    else:
        print(json.dumps(_load_or_exit().model_dump(mode="json"), indent=2))
    return 0


def cmd_secret(args: argparse.Namespace, paths: AppPaths) -> int:
    name = SecretName(args.name)
    if args.action == "set":
        value = getpass.getpass(f"{name.value}: ")
        try:
            set_secret(name, value)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        print(f"Stored {name.value} in Windows Credential Manager.")
    elif args.action == "delete":
        print("Deleted." if delete_secret(name) else "Not set.")
    else:
        print("set" if get_secret(name) else "not set")
    return 0


async def _run_goals(goals: list[str] | None, paths: AppPaths, assume_no: bool) -> int:
    from jarvis.agent.factory import build_agent
    from jarvis.agent.loop import TaskResult, TaskStatus
    from jarvis.interfaces.console import ConsoleApprover, print_event
    from jarvis.llm import LLMError

    settings = _load_or_exit()
    configure_logging(settings.logging, paths.log_dir, console=False)
    try:
        agent, provider = build_agent(settings, paths, ConsoleApprover(assume_no), print_event)
    except LLMError as exc:
        print(exc, file=sys.stderr)
        return 2
    loop = asyncio.get_running_loop()
    running: asyncio.Task[TaskResult] | None = None

    def on_sigint(signum: int, frame: object) -> None:
        # Ctrl+C cancels the task in progress; when idle it exits.
        if running is not None and not running.done():
            loop.call_soon_threadsafe(running.cancel)
        else:
            raise KeyboardInterrupt

    previous = signal.signal(signal.SIGINT, on_sigint)
    try:
        if goals is not None:
            status = TaskStatus.COMPLETED
            for goal in goals:
                running = asyncio.create_task(agent.run(goal))
                try:
                    status = (await running).status
                except asyncio.CancelledError:
                    return 130
            return 0 if status is TaskStatus.COMPLETED else 1
        print(
            f"JARVIS {__version__} ({provider.name}). Type a goal or 'exit'; Ctrl+C stops a task."
        )
        while True:
            line = await _read_line("\nyou> ")
            if line is None or line.strip().lower() in {"exit", "quit"}:
                return 0
            if not line.strip():
                continue
            running = asyncio.create_task(agent.run(line.strip()))
            with contextlib.suppress(asyncio.CancelledError):
                await running
    finally:
        signal.signal(signal.SIGINT, previous)
        await provider.aclose()


async def _read_line(prompt: str) -> str | None:
    """input() on a daemon thread, so an idle Ctrl+C can exit without waiting for Enter."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str | None] = loop.create_future()

    def reader() -> None:
        try:
            value: str | None = input(prompt)
        except (EOFError, KeyboardInterrupt):
            value = None

        def deliver() -> None:
            if not fut.done():
                fut.set_result(value)

        loop.call_soon_threadsafe(deliver)

    threading.Thread(target=reader, daemon=True).start()
    return await fut


def cmd_do(args: argparse.Namespace, paths: AppPaths) -> int:
    return asyncio.run(_run_goals([" ".join(args.goal)], paths, args.no))


def cmd_chat(args: argparse.Namespace, paths: AppPaths) -> int:
    try:
        return asyncio.run(_run_goals(None, paths, False))
    except KeyboardInterrupt:
        return 130


def cmd_pyexec(args: argparse.Namespace, paths: AppPaths) -> int:
    """Run a script with the bundled interpreter (used by run_python in frozen builds)."""
    import runpy

    sys.argv = [args.script]
    runpy.run_path(args.script, run_name="__main__")
    return 0


def cmd_version(args: argparse.Namespace, paths: AppPaths) -> int:
    print(f"jarvis {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis", description="JARVIS - AI control layer")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="start the JARVIS daemon").set_defaults(func=cmd_run)
    sub.add_parser("doctor", help="diagnose the installation").set_defaults(func=cmd_doctor)
    sub.add_parser("version", help="print version").set_defaults(func=cmd_version)

    p_do = sub.add_parser("do", help="accomplish one goal, then exit")
    p_do.add_argument("goal", nargs="+", help="what you want done, in plain language")
    p_do.add_argument("--no", action="store_true", help="decline every approval prompt")
    p_do.set_defaults(func=cmd_do)
    sub.add_parser("chat", help="interactive session in the terminal").set_defaults(func=cmd_chat)

    p_exec = sub.add_parser("_pyexec")  # internal
    p_exec.add_argument("script")
    p_exec.set_defaults(func=cmd_pyexec)

    p_cfg = sub.add_parser("config", help="inspect or initialise configuration")
    p_cfg.add_argument("action", choices=["show", "path", "init"])
    p_cfg.add_argument("--force", action="store_true", help="overwrite on init")
    p_cfg.set_defaults(func=cmd_config)

    p_sec = sub.add_parser("secret", help="manage API keys in Windows Credential Manager")
    p_sec.add_argument("action", choices=["set", "delete", "status"])
    p_sec.add_argument("name", choices=[s.value for s in SecretName])
    p_sec.set_defaults(func=cmd_secret)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    paths = get_paths().ensure()
    func: Callable[[argparse.Namespace, AppPaths], int] = args.func
    return func(args, paths)
