#!/usr/bin/env python3
"""
pyats_resources.py - Core PyATS helper functions and async operations

This module contains all the underlying functionality for interacting with
network devices through PyATS, including:
- Device connection management with caching
- Command execution and parsing
- Configuration application
- Test script execution
- Output cleaning and validation
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import string
import logging
import textwrap
import asyncio
import subprocess
import shutil
from pathlib import Path
from functools import partial
from typing import Dict, Any, Optional, List, Union

from dotenv import load_dotenv
from pyats.topology import loader
from genie.libs.parser.utils import get_parser

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logger = logging.getLogger("PyatsFastMCPServer")

# -----------------------------------------------------------------------------
# Environment Variables
# -----------------------------------------------------------------------------
load_dotenv()

TESTBED_PATH = os.getenv("PYATS_TESTBED_PATH")
if not TESTBED_PATH or not os.path.exists(TESTBED_PATH):
    logger.critical(f"❌ CRITICAL: PYATS_TESTBED_PATH not set or file not found: {TESTBED_PATH}")
    sys.exit(1)

logger.info(f"✅ Using testbed file: {TESTBED_PATH}")

# Artifact retention configuration
ARTIFACTS_DIR = Path(os.getenv("PYATS_MCP_ARTIFACTS_DIR", str(Path.home() / ".pyats-mcp" / "artifacts"))).resolve()
KEEP_ARTIFACTS = os.getenv("PYATS_MCP_KEEP_ARTIFACTS", "1") == "1"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

# Caching configuration
_CACHE_TTL_S = int(os.getenv("PYATS_MCP_TESTBED_CACHE_TTL", "30"))
_TESTBED_CACHE: Dict[str, Any] = {"loaded_at": 0.0, "tb": None}

_CONN_CACHE_TTL_S = int(os.getenv("PYATS_MCP_CONN_CACHE_TTL", "0"))
_CONN_CACHE: Dict[str, Dict[str, Any]] = {}

# -----------------------------------------------------------------------------
# Output Cleaning
# -----------------------------------------------------------------------------
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def clean_output(output: str) -> str:
    """Remove ANSI escape codes and non-printable characters from output."""
    output = ANSI_ESCAPE.sub("", output)
    return "".join(ch for ch in output if ch in string.printable)


# -----------------------------------------------------------------------------
# Testbed and Device Management
# -----------------------------------------------------------------------------
def _load_testbed():
    """Load testbed with TTL-based caching."""
    now = time.time()
    if _TESTBED_CACHE["tb"] is None or (now - _TESTBED_CACHE["loaded_at"]) > _CACHE_TTL_S:
        _TESTBED_CACHE["tb"] = loader.load(TESTBED_PATH)
        _TESTBED_CACHE["loaded_at"] = now
    return _TESTBED_CACHE["tb"]


def _evict_expired_connections() -> None:
    """Remove expired connections from cache based on TTL."""
    if _CONN_CACHE_TTL_S <= 0:
        return
    now = time.time()
    expired = [k for k, v in _CONN_CACHE.items() if (now - float(v.get("last_used", 0))) > _CONN_CACHE_TTL_S]
    for name in expired:
        dev = _CONN_CACHE.get(name, {}).get("device")
        try:
            if dev and getattr(dev, "is_connected", lambda: False)():
                logger.info(f"Conn cache TTL expired; disconnecting {name}...")
                dev.disconnect()
        except Exception:
            pass
        _CONN_CACHE.pop(name, None)


def _get_device(device_name: str):
    """
    Get a device from testbed, managing connections and caching.
    
    Args:
        device_name: Name of the device in the testbed
        
    Returns:
        Connected device object
        
    Raises:
        ValueError: If device not found in testbed
    """
    tb = _load_testbed()
    device = tb.devices.get(device_name)
    if not device:
        raise ValueError(f"Device '{device_name}' not found in testbed '{TESTBED_PATH}'.")

    if _CONN_CACHE_TTL_S > 0:
        _evict_expired_connections()
        if device_name in _CONN_CACHE:
            cached = _CONN_CACHE[device_name].get("device")
            if cached and getattr(cached, "is_connected", lambda: False)():
                _CONN_CACHE[device_name]["last_used"] = time.time()
                return cached

    if not device.is_connected():
        logger.info(f"Connecting to {device_name}...")
        device.connect(
            connection_timeout=120,
            learn_hostname=True,
            log_stdout=False,
            mit=True,
        )
        logger.info(f"Connected to {device_name}")

    if _CONN_CACHE_TTL_S > 0:
        _CONN_CACHE[device_name] = {"device": device, "last_used": time.time()}

    return device


def _disconnect_device(device, force: bool = False):
    """
    Disconnect from device, respecting cache TTL unless forced.
    
    Args:
        device: Device object to disconnect
        force: If True, disconnect immediately regardless of cache settings
    """
    if not device:
        return

    if _CONN_CACHE_TTL_S > 0 and not force:
        try:
            _CONN_CACHE[getattr(device, "name", "unknown")]["last_used"] = time.time()
        except Exception:
            pass
        return

    if getattr(device, "is_connected", lambda: False)():
        try:
            logger.info(f"Disconnecting from {device.name}...")
            device.disconnect()
            logger.info(f"Disconnected from {device.name}")
        except Exception as e:
            logger.warning(f"Error disconnecting: {e}")


# -----------------------------------------------------------------------------
# Show Command Validation
# -----------------------------------------------------------------------------
SHOW_BLOCK_CHARS = ["|", ">", "<"]
SHOW_BLOCK_WORDS = {"copy", "delete", "erase", "reload", "write", "configure", "conf"}


def validate_show_command(command: str) -> Optional[str]:
    """
    Validate that a command is a safe show command.
    
    Args:
        command: Command string to validate
        
    Returns:
        Error message if invalid, None if valid
    """
    cmd = (command or "").strip()
    cmd_lower = cmd.lower()

    if not cmd_lower.startswith("show"):
        return f"Command '{command}' is not a 'show' command."

    if any(ch in cmd_lower for ch in SHOW_BLOCK_CHARS):
        return f"Command '{command}' contains disallowed pipe/redirection."

    tokens = re.findall(r"[a-zA-Z0-9_-]+", cmd_lower)
    for t in tokens:
        if t in SHOW_BLOCK_WORDS:
            return f"Command '{command}' contains disallowed term '{t}'."

    return None


# -----------------------------------------------------------------------------
# Configuration Normalization
# -----------------------------------------------------------------------------
_WRAPPER_LINES = {
    "configure terminal",
    "conf t",
    "config t",
    "configure t",
    "end",
}


def _normalize_config_lines(config_commands: Union[str, List[Any], None]) -> List[str]:
    """
    Normalize config payload into list of CLI lines.
    
    Key behaviors:
    1. Accepts list[str] or multiline string
    2. Splits semicolon-joined commands
    3. Strips wrapper commands (configure terminal, end)
    4. Preserves indentation for submode commands
    5. Does NOT remove 'exit' (needed for interface context)
    
    Args:
        config_commands: Configuration as string or list
        
    Returns:
        List of normalized configuration lines
    """
    if config_commands is None:
        return []

    # Build initial lines
    if isinstance(config_commands, list):
        raw_lines = [str(x) for x in config_commands]
    else:
        # Handle multiline string
        cleaned = textwrap.dedent(str(config_commands)).strip("\n")
        raw_lines = cleaned.splitlines()

    out: List[str] = []
    for line in raw_lines:
        s = line.rstrip("\r\n")
        if not s.strip():
            continue

        # Split semicolon-separated commands
        if ";" in s:
            parts = [p.strip() for p in s.split(";") if p.strip()]
            for p in parts:
                low = p.lower()
                if low in _WRAPPER_LINES:
                    continue
                out.append(p)
            continue

        # Check if line is a wrapper command
        low = s.strip().lower()
        if low in _WRAPPER_LINES:
            continue

        out.append(s)

    return out


# -----------------------------------------------------------------------------
# Async Command Execution Functions
# -----------------------------------------------------------------------------
async def run_show_command_async(device_name: str, command: str) -> Dict[str, Any]:
    """
    Execute a show command asynchronously with parsing attempt.
    
    Args:
        device_name: Name of device in testbed
        command: Show command to execute
        
    Returns:
        Dictionary with status, parsed/raw output, and metadata
    """
    def _run():
        val_err = validate_show_command(command)
        if val_err:
            return {"status": "error", "error": val_err}

        device = None
        try:
            device = _get_device(device_name)
            raw = device.execute(command, timeout=60)
            cleaned = clean_output(raw)

            parser_cls = get_parser(command, device)
            if parser_cls:
                try:
                    parser_obj = parser_cls(device=device)
                    parsed = parser_obj.parse(output=cleaned)
                    return {
                        "status": "completed",
                        "device": device_name,
                        "command": command,
                        "parsed_output": parsed,
                        "raw_output": cleaned,
                        "parser_used": parser_cls.__name__,
                    }
                except Exception as e:
                    logger.warning(f"Parser failed: {e}")

            return {
                "status": "completed",
                "device": device_name,
                "command": command,
                "raw_output": cleaned,
                "parser_used": None,
            }

        except Exception as e:
            logger.error(f"Error running show command: {e}", exc_info=True)
            return {"status": "error", "device": device_name, "command": command, "error": str(e)}
        finally:
            _disconnect_device(device)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


async def apply_device_configuration_async(device_name: str, config_commands: Any) -> Dict[str, Any]:
    """
    Apply configuration to a device asynchronously.
    
    Args:
        device_name: Name of device in testbed
        config_commands: Configuration as string or list
        
    Returns:
        Dictionary with status and configuration results
    """
    def _apply():
        lines = _normalize_config_lines(config_commands)
        if not lines:
            return {"status": "error", "error": "No valid configuration lines provided."}

        device = None
        try:
            device = _get_device(device_name)
            raw_output = device.configure(lines, timeout=180)
            cleaned = clean_output(raw_output)

            return {
                "status": "completed",
                "device": device_name,
                "lines_sent": lines,
                "raw_output": cleaned,
            }

        except Exception as e:
            logger.error(f"Error applying config: {e}", exc_info=True)
            return {
                "status": "error",
                "device": device_name,
                "lines_sent": lines,
                "error": str(e),
            }
        finally:
            _disconnect_device(device)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _apply)


async def execute_learn_config_async(device_name: str) -> Dict[str, Any]:
    """
    Get running configuration from device asynchronously.
    
    Args:
        device_name: Name of device in testbed
        
    Returns:
        Dictionary with status and running configuration
    """
    def _learn():
        device = None
        try:
            device = _get_device(device_name)
            raw = device.execute("show running-config", timeout=120)
            cleaned = clean_output(raw)

            return {
                "status": "completed",
                "device": device_name,
                "running_config": cleaned,
            }

        except Exception as e:
            logger.error(f"Error learning config: {e}", exc_info=True)
            return {"status": "error", "device": device_name, "error": str(e)}
        finally:
            _disconnect_device(device)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _learn)


async def execute_learn_logging_async(device_name: str) -> Dict[str, Any]:
    """
    Get device logs asynchronously.
    
    Args:
        device_name: Name of device in testbed
        
    Returns:
        Dictionary with status and logging output
    """
    def _learn():
        device = None
        try:
            device = _get_device(device_name)
            raw = device.execute("show logging", timeout=120)
            cleaned = clean_output(raw)

            return {
                "status": "completed",
                "device": device_name,
                "logging": cleaned,
            }

        except Exception as e:
            logger.error(f"Error learning logging: {e}", exc_info=True)
            return {"status": "error", "device": device_name, "error": str(e)}
        finally:
            _disconnect_device(device)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _learn)


async def run_ping_command_async(device_name: str, command: str) -> Dict[str, Any]:
    """
    Execute ping command from network device asynchronously.
    
    Args:
        device_name: Name of device in testbed
        command: Ping command to execute
        
    Returns:
        Dictionary with status and ping results (parsed if possible)
    """
    def _ping():
        device = None
        try:
            device = _get_device(device_name)
            raw = device.execute(command, timeout=180)
            cleaned = clean_output(raw)

            parser_cls = get_parser(command, device)
            if parser_cls:
                try:
                    parser_obj = parser_cls(device=device)
                    parsed = parser_obj.parse(output=cleaned)
                    return {
                        "status": "completed",
                        "device": device_name,
                        "command": command,
                        "parsed_output": parsed,
                        "raw_output": cleaned,
                        "parser_used": parser_cls.__name__,
                    }
                except Exception as e:
                    logger.warning(f"Ping parser failed: {e}")

            return {
                "status": "completed",
                "device": device_name,
                "command": command,
                "raw_output": cleaned,
                "parser_used": None,
            }

        except Exception as e:
            logger.error(f"Error running ping: {e}", exc_info=True)
            return {"status": "error", "device": device_name, "command": command, "error": str(e)}
        finally:
            _disconnect_device(device)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _ping)


async def run_linux_command_async(device_name: str, command: str) -> Dict[str, Any]:
    """
    Execute Linux command on device asynchronously.
    
    Args:
        device_name: Name of device in testbed
        command: Linux command to execute
        
    Returns:
        Dictionary with status and command output
    """
    def _exec():
        device = None
        try:
            device = _get_device(device_name)
            raw = device.execute(command, timeout=120)
            cleaned = clean_output(raw)

            return {
                "status": "completed",
                "device": device_name,
                "command": command,
                "output": cleaned,
            }

        except Exception as e:
            logger.error(f"Error running Linux command: {e}", exc_info=True)
            return {"status": "error", "device": device_name, "command": command, "error": str(e)}
        finally:
            _disconnect_device(device)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _exec)


# -----------------------------------------------------------------------------
# Test Script Security and Execution
# -----------------------------------------------------------------------------
BANNED_IMPORTS = ["socket", "requests", "urllib", "httpx", "telnetlib", "paramiko", "netmiko"]
BANNED_PATTERNS = [
    r"\.connect\(",
    r"\.disconnect\(",
    r"Testbed\(",
    r"loader\.load",
    r"subprocess\.",
    r"os\.system",
    r"eval\(",
    r"exec\(",
    r"__import__",
]


def reject_unsafe_script(script: str) -> Optional[str]:
    """
    Check test script for unsafe patterns.
    
    Args:
        script: Test script content to validate
        
    Returns:
        Error message if unsafe, None if safe
    """
    lower = script.lower()
    for imp in BANNED_IMPORTS:
        if f"import {imp}" in lower or f"from {imp}" in lower:
            return f"Script contains banned import: {imp}"

    for pat in BANNED_PATTERNS:
        if re.search(pat, script, re.IGNORECASE):
            return f"Script contains banned pattern: {pat}"

    return None


def _extract_overall_result(stdout: str) -> str:
    """Extract overall test result from pyATS stdout."""
    for line in stdout.splitlines():
        line_lower = line.lower()
        if "passed" in line_lower and "overall" in line_lower:
            return "PASSED"
        if "failed" in line_lower and "overall" in line_lower:
            return "FAILED"
    return "UNKNOWN"


def _run_test_script(test_script_content: str, timeout_s: int = 300) -> Dict[str, Any]:
    """
    Execute a pyATS test script synchronously.
    
    Args:
        test_script_content: Python test script content
        timeout_s: Execution timeout in seconds
        
    Returns:
        Dictionary with test execution results
    """
    run_dir = ARTIFACTS_DIR / f"test_{int(time.time() * 1000)}"
    run_dir.mkdir(parents=True, exist_ok=True)

    script_path = run_dir / "test_script.py"
    job_path = run_dir / "job.py"
    report_path = run_dir / "report.json"

    try:
        script_path.write_text(test_script_content, encoding="utf-8")

        safe_script_path = str(script_path).replace("\\", "\\\\")
        job_content = f"""from pyats.easypy import run
def main(runtime):
    run(testscript='{safe_script_path}', runtime=runtime)
"""
        job_path.write_text(job_content, encoding="utf-8")

        pyats_exec = shutil.which("pyats") or "pyats"
        cmd = [pyats_exec, "run", "job", str(job_path), "--json-job", str(report_path)]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env={**os.environ, "PYATS_TESTBED_PATH": TESTBED_PATH},
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "error": f"pyATS job timed out after {timeout_s}s",
                "artifacts_dir": str(run_dir)
            }

        report_data = None
        if report_path.exists():
            try:
                txt = report_path.read_text(encoding="utf-8")
                report_data = json.loads(txt) if txt.strip() else None
            except Exception as e:
                logger.warning(f"Failed to parse report JSON: {e}")

        overall = _extract_overall_result(result.stdout)

        payload = {
            "status": "completed",
            "returncode": result.returncode,
            "overall_result": overall,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "report": report_data,
            "artifacts_dir": str(run_dir),
            "paths": {
                "script": str(script_path),
                "job": str(job_path),
                "report": str(report_path),
            },
        }

        if not KEEP_ARTIFACTS:
            shutil.rmtree(run_dir, ignore_errors=True)

        return payload

    except Exception as e:
        logger.error(f"Error executing dynamic test: {e}", exc_info=True)
        return {"status": "error", "error": str(e), "artifacts_dir": str(run_dir)}
