"""
launch.py — dependency-robust launcher for the ClaudeComms MCP bridge.

Claude Code runs this with whatever `python` it finds; stdlib is all this shim
needs. We then GUARANTEE the `mcp` package is available for the actual bridge,
trying in order:
  1. the current interpreter, if it already imports `mcp`
  2. a cached venv under CLAUDE_PLUGIN_DATA (survives plugin updates)
  3. `uv run` if uv is on PATH (resolves PEP 723 deps in comms_bridge.py)
  4. bootstrap a venv and `pip install mcp` (last resort; one-time, needs network)

Then we run comms_bridge.py with that interpreter, INHERITING stdio so it speaks
MCP straight to Claude Code. This is what makes the plugin survive the
multi-python / missing-deps trap that otherwise surfaces as a -32000 reconnect
error: the shim runs under any python, and provisions the right one for the server.
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRIDGE = os.path.join(HERE, "comms_bridge.py")


def log(*a):
    print("[launch]", *a, file=sys.stderr, flush=True)


def venv_dir():
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(ROOT, ".data")
    return os.path.join(base, "venv")


def venv_python(venv):
    for rel in ("Scripts/python.exe", "bin/python", "bin/python3"):
        p = os.path.join(venv, *rel.split("/"))
        if os.path.isfile(p):
            return p
    return None


def has_mcp(py):
    try:
        return subprocess.run(
            [py, "-c", "import mcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    except Exception:
        return False


def bootstrap_venv():
    venv = venv_dir()
    vpy = venv_python(venv)
    if vpy and has_mcp(vpy):
        return vpy
    os.makedirs(os.path.dirname(venv), exist_ok=True)
    base = sys.executable or "python"
    log(f"creating venv at {venv} (first run)")
    subprocess.run([base, "-m", "venv", venv], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    vpy = venv_python(venv)
    if not vpy:
        raise RuntimeError("venv creation produced no python")
    log("installing mcp into venv (first run only)")
    subprocess.run([vpy, "-m", "pip", "install", "-q", "--disable-pip-version-check", "mcp"],
                   check=True, stdout=subprocess.DEVNULL)  # keep MCP stdout clean
    return vpy


def resolve_cmd():
    if has_mcp(sys.executable):
        return [sys.executable, BRIDGE]
    vpy = venv_python(venv_dir())
    if vpy and has_mcp(vpy):
        return [vpy, BRIDGE]
    if shutil.which("uv"):
        log("using `uv run` to provision deps")
        return ["uv", "run", "--script", BRIDGE]
    return [bootstrap_venv(), BRIDGE]


def main():
    try:
        cmd = resolve_cmd()
    except Exception as e:
        log(f"FATAL: could not provision the 'mcp' dependency: {e}")
        log("Fix: ensure Python 3.10+ with pip and network access, OR `pip install uv`, "
            "OR `pip install mcp` for the python on your PATH, then reload the plugin.")
        sys.exit(1)
    # stdio passthrough: the bridge inherits our stdin/stdout/stderr and talks
    # MCP directly to Claude Code; we just wait for it to exit.
    sys.exit(subprocess.run(cmd).returncode)


if __name__ == "__main__":
    main()
