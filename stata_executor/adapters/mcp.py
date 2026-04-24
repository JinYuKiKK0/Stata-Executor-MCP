from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from ..contract import RunDoRequest, RunInlineRequest
from ..engine import StataExecutor


INSTRUCTIONS = "Use doctor before run_do or run_inline when configuration is uncertain."


@dataclass(frozen=True, slots=True)
class _EnvConfig:
    stata_executable: str | None
    edition: str | None
    env_error: str | None


def _parse_env_edition(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if value not in {"mp", "se", "be"}:
        return None, "STATA_EXECUTOR_EDITION must be one of: mp, se, be."
    return value, None


def _load_env_config() -> _EnvConfig:
    stata_executable = os.environ.get("STATA_EXECUTOR_STATA_EXECUTABLE")
    edition, env_error = _parse_env_edition(os.environ.get("STATA_EXECUTOR_EDITION"))
    return _EnvConfig(stata_executable=stata_executable, edition=edition, env_error=env_error)


server: Server = Server("stata-executor", version="0.1.0", instructions=INSTRUCTIONS)
_env = _load_env_config()
_executor = StataExecutor()


@server.list_tools()
async def _list_tools() -> list[Tool]:
    return [
        Tool(
            name="doctor",
            title="Doctor",
            description="Validate MCP environment configuration and resolve the Stata executable path.",
            inputSchema={"type": "object", "properties": {}},
            outputSchema=_doctor_output_schema(),
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        Tool(
            name="run_do",
            title="Run Do-File",
            description="Execute an existing Stata do-file and return the factual execution result.",
            inputSchema=_execution_input_schema(required=["script_path"]),
            outputSchema=_execution_output_schema(),
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=False,
            ),
        ),
        Tool(
            name="run_inline",
            title="Run Inline Commands",
            description="Execute inline Stata commands and return the factual execution result.",
            inputSchema=_execution_input_schema(required=["commands"]),
            outputSchema=_execution_output_schema(),
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=False,
            ),
        ),
    ]


@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    if _env.env_error:
        return _build_error(_env.env_error)

    if name == "doctor":
        result = _executor.doctor(
            stata_executable=_env.stata_executable,
            edition=_env.edition,
            config_source="env" if _env.stata_executable else "missing",
        )
        return _build_result(result.to_dict(), is_error=False)

    if name == "run_do":
        execution = _executor.run_do(
            RunDoRequest(
                script_path=arguments["script_path"],
                working_dir=arguments.get("working_dir"),
                timeout_sec=arguments.get("timeout_sec"),
                artifact_globs=tuple(arguments.get("artifact_globs") or ()),
                edition=_env.edition,
                stata_executable=_env.stata_executable,
                env_overrides=arguments.get("env_overrides") or {},
            )
        )
        return _build_result(execution.to_dict(), is_error=execution.status == "failed")

    if name == "run_inline":
        execution = _executor.run_inline(
            RunInlineRequest(
                commands=arguments["commands"],
                working_dir=arguments.get("working_dir"),
                timeout_sec=arguments.get("timeout_sec"),
                artifact_globs=tuple(arguments.get("artifact_globs") or ()),
                edition=_env.edition,
                stata_executable=_env.stata_executable,
                env_overrides=arguments.get("env_overrides") or {},
            )
        )
        return _build_result(execution.to_dict(), is_error=execution.status == "failed")

    return _build_error(f"Unknown tool: {name}")


def _build_result(payload: dict[str, Any], *, is_error: bool) -> CallToolResult:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=payload,
        isError=is_error,
    )


def _build_error(message: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        isError=True,
    )


def _execution_input_schema(*, required: list[str]) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "working_dir": {"type": "string", "minLength": 1},
        "timeout_sec": {"type": "integer", "minimum": 1},
        "artifact_globs": {"type": "array", "items": {"type": "string"}},
        "env_overrides": {"type": "object", "additionalProperties": {"type": "string"}},
    }
    if "script_path" in required:
        properties["script_path"] = {"type": "string", "minLength": 1}
    if "commands" in required:
        properties["commands"] = {"type": "string", "minLength": 1}
    return {"type": "object", "properties": properties, "required": required}


def _execution_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["succeeded", "failed"]},
            "phase": {"type": "string"},
            "exit_code": {"type": "integer"},
            "error_kind": {"type": ["string", "null"]},
            "summary": {"type": "string"},
            "result_text": {"type": "string"},
            "diagnostic_excerpt": {"type": "string"},
            "error_signature": {"type": ["string", "null"]},
            "failed_command": {"type": ["string", "null"]},
            "artifacts": {"type": "array", "items": {"type": "string"}},
            "elapsed_ms": {"type": "integer"},
        },
        "required": [
            "status",
            "phase",
            "exit_code",
            "error_kind",
            "summary",
            "result_text",
            "diagnostic_excerpt",
            "error_signature",
            "failed_command",
            "artifacts",
            "elapsed_ms",
        ],
    }


def _doctor_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "ready": {"type": "boolean"},
            "summary": {"type": "string"},
            "config_path": {"type": "string"},
            "config_exists": {"type": "boolean"},
            "config_source": {"type": "string", "enum": ["explicit", "env", "missing"]},
            "stata_executable": {"type": ["string", "null"]},
            "edition": {"type": ["string", "null"]},
            "defaults": {
                "type": "object",
                "properties": {
                    "timeout_sec": {"type": "integer"},
                    "artifact_globs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["timeout_sec", "artifact_globs"],
            },
            "errors": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "ready",
            "summary",
            "config_path",
            "config_exists",
            "config_source",
            "stata_executable",
            "edition",
            "defaults",
            "errors",
        ],
    }


async def _run_stdio() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> int:
    anyio.run(_run_stdio)
    return 0
