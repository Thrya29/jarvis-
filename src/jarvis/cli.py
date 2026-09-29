"""Command-line interface: ``jarvis run | doctor | config | secret | version``."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import platform
import socket
import sys
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


def cmd_version(args: argparse.Namespace, paths: AppPaths) -> int:
    print(f"jarvis {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis", description="JARVIS - AI control layer")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="start the JARVIS daemon").set_defaults(func=cmd_run)
    sub.add_parser("doctor", help="diagnose the installation").set_defaults(func=cmd_doctor)
    sub.add_parser("version", help="print version").set_defaults(func=cmd_version)

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
    args = build_parser().parse_args(argv)
    paths = get_paths().ensure()
    func: Callable[[argparse.Namespace, AppPaths], int] = args.func
    return func(args, paths)
