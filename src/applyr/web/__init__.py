"""Local web UI — a fourth front door over the same tool registry.

Reads return JSON; writes go through dispatch() and therefore land as pending
proposals. The browser's Confirm button is the same confirm_proposal call
Claude makes over MCP. Serves on localhost only; nothing leaves the machine.
"""
