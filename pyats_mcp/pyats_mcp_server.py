#!/usr/bin/env python3
"""
pyats_mcp_server.py - MCP Server with PyATS Network Automation Tools

This module defines the FastMCP server and all MCP tool endpoints for
network automation using PyATS. It imports all core functionality from
pyats_resources.py.
"""

from __future__ import annotations

import os
import sys
import json
import logging
import asyncio
from functools import partial
from typing import Any

from fastmcp import FastMCP

# Import all resources from pyats_resources module
from .pyats_resources import (
    # Core functions
    _load_testbed,
    reject_unsafe_script,
    _run_test_script,
    # Async operations
    run_show_command_async,
    apply_device_configuration_async,
    execute_learn_config_async,
    execute_learn_logging_async,
    run_ping_command_async,
    run_linux_command_async,
)

# -----------------------------------------------------------------------------
# Logging Setup
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("PyatsFastMCPServer")

# -----------------------------------------------------------------------------
# MCP Server Initialization
# -----------------------------------------------------------------------------
mcp = FastMCP("pyATS Network Automation Server")


# -----------------------------------------------------------------------------
# MCP Tools
# Note: All tools have the optional parameter 'toolCallId' for usage with n8n
# -----------------------------------------------------------------------------
@mcp.tool()
async def pyats_list_devices(toolCallId: str = None) -> str:
    """List all devices available in the testbed with their properties."""
    try:
        tb = _load_testbed()
        devices: dict[str, Any] = {}
        for name, dev in tb.devices.items():
            devices[name] = {
                "os": getattr(dev, "os", None),
                "type": getattr(dev, "type", None),
                "platform": getattr(dev, "platform", None),
                "connections": list(getattr(dev, "connections", {}).keys()),
            }
        return json.dumps({"status": "completed", "devices": devices}, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_list_devices: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


@mcp.tool()
async def pyats_run_show_command(device_name: str, command: str, toolCallId: str = None) -> str:
    """
    Execute a show command on a device and return parsed output (or raw if parsing fails).
    DO NOT use this for 'show logging' or 'show running-config' - use dedicated tools.
    DO NOT include pipes or redirects in commands.
    """
    try:
        result = await run_show_command_async(device_name, command)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_run_show_command: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


@mcp.tool()
async def pyats_configure_device(device_name: str, config_commands: Any, toolCallId: str = None) -> str:
    """
    Apply configuration to a device.

    IMPORTANT:
    - Pass configuration as a list of strings or multiline string
    - Do NOT include 'configure terminal', 'conf t', or 'end'
    - The server automatically handles config mode entry/exit
    - Preserve proper indentation for submode commands (interfaces, routing protocols, etc.)
    
    Example list format:
    ["cdp run", "interface GigabitEthernet0/0", " cdp enable", " exit"]
    
    Example multiline string format:
    '''
    cdp run
    interface GigabitEthernet0/0
     cdp enable
     exit
    '''
    """
    try:
        result = await apply_device_configuration_async(device_name, config_commands)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_configure_device: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


@mcp.tool()
async def pyats_show_running_config(device_name: str, toolCallId: str = None) -> str:
    """Get the complete running configuration from a device (raw output)."""
    try:
        result = await execute_learn_config_async(device_name)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_show_running_config: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


@mcp.tool()
async def pyats_show_logging(device_name: str, toolCallId: str = None) -> str:
    """Get device logs using 'show logging' (raw output)."""
    try:
        result = await execute_learn_logging_async(device_name)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_show_logging: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


@mcp.tool()
async def pyats_ping_from_network_device(device_name: str, command: str, toolCallId: str = None) -> str:
    """
    Execute a ping command from a network device (e.g., 'ping 1.1.1.1' or 'ping 1.1.1.1 repeat 100').
    Returns structured JSON (success rate, rtt) if parsing succeeds, otherwise raw output.
    This is preferred over pyats_run_show_command for connectivity checks.
    """
    try:
        result = await run_ping_command_async(device_name, command)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_ping_from_network_device: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


@mcp.tool()
async def pyats_run_linux_command(device_name: str, command: str, toolCallId: str = None) -> str:
    """Execute a Linux command on a device (for Linux-based network devices)."""
    try:
        result = await run_linux_command_async(device_name, command)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_run_linux_command: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


@mcp.tool()
async def pyats_run_dynamic_test(test_script_content: str, toolCallId: str = None) -> str:
    """
    Execute a standalone pyATS AEtest script for programmatic validation.
    
    CRITICAL REQUIREMENTS:
    - Script must NOT connect to devices (all data must be embedded)
    - Script must define TEST_DATA as a Python dict literal (no json.loads)
    - Embed all collected command outputs directly in TEST_DATA
    - Use this for health checks, validation, and complex troubleshooting
    
    Returns: Full job report with PASS/FAIL result and detailed test outcomes
    """
    if not (test_script_content or "").strip():
        return json.dumps({"status": "error", "error": "Empty test script content provided."}, indent=2)

    reason = reject_unsafe_script(test_script_content)
    if reason:
        return json.dumps({"status": "error", "error": reason}, indent=2)

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, partial(_run_test_script, test_script_content, 300))
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in pyats_run_dynamic_test: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": str(e)}, indent=2)


# =============================================================================
# SERVER STARTUP
# =============================================================================
def main():
    """Main entry point for the MCP server."""
    logger.info('🤖 pyATS MCP Server starting!')

    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    if transport == "http" or transport == "sse":
        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8000"))
        logger.info(f"✅ Starting MCP server with {transport} transport on {host}:{port}")
        mcp.run(transport=transport, host=host, port=port)
    else:
        logger.info("✅ Starting MCP server with stdio transport")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
