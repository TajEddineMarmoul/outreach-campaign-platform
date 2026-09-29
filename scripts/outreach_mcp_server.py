"""Codex-compatible stdio entry point for the Outreach MCP server."""

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from outreach_mcp.server import mcp  # noqa: E402


if __name__ == "__main__":
    mcp.run()
