"""Shell and Python execution in a constrained child process.

Constraints: working directory inside an allowed folder, secrets stripped from the
environment, no console window, no stdin, hard timeout that kills the whole process
tree, and capped output. Every call requires approval unless the user opted out.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field

from jarvis.safety.policy import Risk
from jarvis.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

_SECRET_ENV_PREFIXES = ("ANTHROPIC_", "JARVIS_", "AWS_", "AZURE_", "GITHUB_TOKEN", "GH_TOKEN")
MAX_OUTPUT = 200_000


def scrubbed_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_SECRET_ENV_PREFIXES)}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/T", "/F", "/PID", str(proc.pid),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        await killer.wait()
    else:
        proc.kill()
    await proc.wait()


async def run_process(argv: list[str], cwd: Path, timeout_s: float) -> tuple[int | None, str]:
    """Run argv; return (exit code or None on timeout, combined output)."""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=scrubbed_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        creationflags=flags,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        await _kill_tree(proc)
        return None, ""
    except asyncio.CancelledError:
        await _kill_tree(proc)
        raise
    text = out[:MAX_OUTPUT].decode("utf-8", errors="replace")
    return proc.returncode, text


def _format(code: int | None, output: str, timeout_s: float) -> str:
    if code is None:
        return f"Timed out after {timeout_s:.0f}s; the process was killed."
    return f"exit code {code}\n{output.strip() or '(no output)'}"


class RunShellArgs(ToolArgs):
    command: str = Field(description="PowerShell command or script to run.")
    cwd: str | None = Field(default=None, description="Working folder (default: Documents).")
    timeout_s: float = Field(default=60, gt=0, le=600)


class RunShell(Tool[RunShellArgs]):
    name = "run_shell"
    description = (
        "Run a PowerShell command non-interactively and return exit code and output. "
        "Prefer the dedicated file/Office tools when they fit; use this for anything else "
        "(git, installed CLIs, system queries). Requires the user's approval."
    )
    args_model: ClassVar[type[ToolArgs]] = RunShellArgs
    risk = Risk.EXECUTE

    def risk_for(self, args: RunShellArgs, ctx: ToolContext) -> Risk:
        ctx.guard.resolve(args.cwd or ctx.guard.default_root)
        return Risk.EXECUTE

    def summarize(self, args: RunShellArgs) -> str:
        return f"Run PowerShell: {args.command[:300]}"

    def details(self, args: RunShellArgs) -> str:
        return args.command

    async def run(self, args: RunShellArgs, ctx: ToolContext) -> ToolResult:
        cwd = ctx.guard.resolve(args.cwd or ctx.guard.default_root)
        if not cwd.is_dir():
            raise ToolError(f"{cwd} is not a folder")
        argv = [
            "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-Command", args.command,
        ]  # fmt: skip
        timeout = min(args.timeout_s, ctx.settings.agent.tool_timeout_s - 5)
        code, out = await run_process(argv, cwd, timeout)
        return ToolResult(_format(code, out, timeout), untrusted=True, source="run_shell output")


class RunPythonArgs(ToolArgs):
    code: str = Field(description="Python 3 source to execute as a script.")
    cwd: str | None = Field(default=None, description="Working folder (default: Documents).")
    timeout_s: float = Field(default=60, gt=0, le=600)


def python_argv(script: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        # The packaged jarvis.exe embeds an interpreter; see `jarvis _pyexec`.
        return [sys.executable, "_pyexec", str(script)]
    return [sys.executable, "-I", str(script)]


class RunPython(Tool[RunPythonArgs]):
    name = "run_python"
    description = (
        "Run a Python script (data processing, calculations, file conversions). openpyxl, "
        "python-docx and pypdf are available. Print results to stdout. Requires approval."
    )
    args_model: ClassVar[type[ToolArgs]] = RunPythonArgs
    risk = Risk.EXECUTE

    def risk_for(self, args: RunPythonArgs, ctx: ToolContext) -> Risk:
        ctx.guard.resolve(args.cwd or ctx.guard.default_root)
        return Risk.EXECUTE

    def summarize(self, args: RunPythonArgs) -> str:
        first = next((ln for ln in args.code.splitlines() if ln.strip()), "")
        return f"Run Python script ({len(args.code.splitlines())} lines): {first[:120]}"

    def details(self, args: RunPythonArgs) -> str:
        return args.code

    async def run(self, args: RunPythonArgs, ctx: ToolContext) -> ToolResult:
        cwd = ctx.guard.resolve(args.cwd or ctx.guard.default_root)
        scripts = ctx.work_dir / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        script = scripts / f"{uuid.uuid4().hex}.py"
        script.write_text(args.code, encoding="utf-8")
        try:
            timeout = min(args.timeout_s, ctx.settings.agent.tool_timeout_s - 5)
            code, out = await run_process(python_argv(script), cwd, timeout)
        finally:
            script.unlink(missing_ok=True)
        return ToolResult(_format(code, out, timeout), untrusted=True, source="run_python output")


EXEC_TOOLS: list[Tool[Any]] = [RunShell(), RunPython()]
