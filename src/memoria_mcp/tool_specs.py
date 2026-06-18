"""Canonical MCP tool definitions for Ripple Memory.

The daemon, stdio proxy, and install checks must all expose the same small tool
surface. Keep schema here so transport code does not grow memory logic.
"""
from __future__ import annotations

from typing import List

from mcp.types import Tool


PROJECT_DESC = (
    "Project name for memory isolation. Different projects have separate "
    "memory databases. Auto-created on first use."
)

EXPECTED_CORE_TOOLS = [
    "memoria_remember",
    "memoria_recall",
    "memoria_read",
    "memoria_forget",
]


def build_memory_tools(*, expose_project_tools: bool = False) -> List[Tool]:
    tools = [
        Tool(
            name="memoria_remember",
            description=(
                "Store a memory. Use for important facts, decisions, patterns, insights, "
                "code solutions, debugging discoveries. The memory system handles all "
                "maintenance automatically (decay, compression, archival). To replace an "
                "old口径 without adding tools, pass fact_key and supersedes_ref_ids."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The memory content to store"},
                    "project": {"type": "string", "description": PROJECT_DESC, "default": "default"},
                    "type": {
                        "type": "string",
                        "enum": ["fact", "code_pattern", "debug_insight", "arch_decision", "preference", "rule"],
                        "description": "Memory type (default: fact)",
                        "default": "fact",
                    },
                    "importance": {"type": "number", "description": "Importance 0-1 (default: 0.5)", "default": 0.5},
                    "confidence": {"type": "number", "description": "Confidence 0-1 (default: 0.7)", "default": 0.7},
                    "fact_key": {
                        "type": "string",
                        "description": "Optional stable topic key for current-vs-historical memory, e.g. build.deploy.policy",
                    },
                    "supersedes_ref_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional ref_ids/node_ids this new memory replaces. Default recall filters those old口径 as superseded.",
                    },
                    "evolution_status": {
                        "type": "string",
                        "enum": ["active", "pending_conflict"],
                        "description": "Optional status for fact_key writes. Use pending_conflict when the new口径 is not confirmed.",
                        "default": "active",
                    },
                    "evolution_reason": {
                        "type": "string",
                        "description": "Optional short reason for replacing old口径.",
                    },
                },
                "required": ["content", "project"],
            },
        ),
        Tool(
            name="memoria_recall",
            description=(
                "Search memories by query. Returns ranked memory summaries with ref_id/read_hint. "
                "Default recall filters clearly superseded old口径. Use memoria_read when an exact memory needs to be expanded."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "project": {"type": "string", "description": PROJECT_DESC, "default": "default"},
                    "top_k": {"type": "integer", "description": "Max results (default: 5)", "default": 5},
                    "include_evolution": {
                        "type": "boolean",
                        "description": "Set true to include superseded historical口径 in results for audit/debug.",
                        "default": False,
                    },
                },
                "required": ["query", "project"],
            },
        ),
        Tool(
            name="memoria_read",
            description=(
                "Read exact memory content for a ref_id returned by memoria_recall. "
                "Supports memory_node:<id>, memory_index:<id>, raw node_id, offset, and max_chars."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ref_id": {"type": "string", "description": "Memory ref_id from memoria_recall, e.g. memory_node:<id>"},
                    "node_id": {"type": "string", "description": "Raw memory node id fallback"},
                    "project": {"type": "string", "description": PROJECT_DESC, "default": "default"},
                    "offset": {"type": "integer", "description": "Character offset for paging", "default": 0},
                    "max_chars": {"type": "integer", "description": "Maximum characters to return", "default": 4000},
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="memoria_forget",
            description=(
                "Delete a memory by ID, or delete a whole project when scope='project' "
                "and confirm='DELETE:<project>'. User-visible deletion removes active rows/readable "
                "records, but does not promise forensic secure erase."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["memory", "project"],
                        "description": "Delete a single memory or a whole project.",
                        "default": "memory",
                    },
                    "node_id": {"type": "string", "description": "ID of the memory to delete when scope='memory'"},
                    "project": {"type": "string", "description": PROJECT_DESC, "default": "default"},
                    "confirm": {
                        "type": "string",
                        "description": "Required only for scope='project': DELETE:<sanitized_project_name>",
                    },
                },
                "required": ["project"],
            },
        ),
    ]
    if expose_project_tools:
        tools.extend([
            Tool(
                name="memoria_list_projects",
                description=(
                    "List all memory projects with their status and size. "
                    "Use to see what projects exist and when they were last accessed."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="memoria_archive_project",
                description=(
                    "Archive an active memory project without deleting it. "
                    "Archived projects are restored automatically when used again."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": PROJECT_DESC},
                    },
                    "required": ["project"],
                },
            ),
        ])
    return tools
