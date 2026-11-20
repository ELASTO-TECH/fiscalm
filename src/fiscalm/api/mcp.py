"""JSON-only Streamable HTTP and newline-framed stdio MCP, without dependencies."""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import TextIO

from fiscalm import PRODUCT
from fiscalm.api import mcp_tools, tokens, v1 as api_v1
from fiscalm.ledger import Ledger

MODERN = "2026-07-28"
LEGACY = "2025-11-25"
PREFIX = "io.modelcontextprotocol/"
INFO = {"name": PRODUCT, "version": "1.0.0"}
CAPABILITIES = {"tools": {}}


def error(ident: object, code: int, message: str, data: dict | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": ident,
        "error": {"code": code, "message": message, **({"data": data} if data else {})},
    }


def dispatch(L: Ledger, credential: dict, message: object, headers=None) -> tuple[int, dict | None]:
    """No batches (removed in MCP 2025-06-18); notifications never execute tools."""

    invalid = _request_error(message)
    if invalid is not None:
        return invalid
    ident, method, params = message.get("id"), message["method"], message.get("params", {})
    if "id" not in message:
        return 202, None
    invalid = _metadata_error(ident, params)
    if invalid is not None:
        return invalid
    meta = params.get("_meta", {})
    version = meta.get(PREFIX + "protocolVersion")
    modern = version is not None
    invalid = _header_error(ident, method, params, version, modern, headers)
    if invalid is not None:
        return invalid
    try:
        handlers = {
            "ping": _ping,
            "tools/list": _list_tools,
            "tools/call": _call_tool,
            **({"server/discover": _discover} if modern else {"initialize": _initialize}),
        }
        handler = handlers.get(method)
        if handler is None:
            return (404 if modern else 200), error(ident, -32601, "Method not found.")
        if method == "initialize" and (
            not isinstance(params.get("protocolVersion"), str) or not isinstance(params.get("capabilities"), dict)
        ):
            return 400, error(ident, -32602, "Declare protocolVersion and capabilities.")
        if method == "tools/call" and params.get("name") not in {
            t["name"] for t in mcp_tools.definitions(credential["scope"])
        }:
            return 400, error(ident, -32602, "Unknown or unavailable tool. Check tools/list.")
        result = handler(L, credential, params)
    except ValueError:
        return 400, error(ident, -32602, "Invalid tool name or arguments. Check tools/list.")
    except Exception:
        return 500, error(ident, -32603, "The request failed. Retry later or contact the administrator.")
    if modern:
        result.update(resultType="complete", _meta={PREFIX + "serverInfo": INFO})
    return 200, {"jsonrpc": "2.0", "id": ident, "result": result}


def _request_error(message: object) -> tuple[int, dict] | None:
    if (
        not isinstance(message, dict)
        or message.get("jsonrpc") != "2.0"
        or not isinstance(message.get("method"), str)
        or "result" in message
        or "error" in message
        or ("id" in message and (type(message["id"]) not in (str, int)))
    ):
        return 400, error(None, -32600, "Invalid JSON-RPC request.")
    ident, params = message.get("id"), message.get("params", {})
    if not isinstance(params, dict):
        return 400, error(ident, -32602, "Params must be an object.")
    return None


def _metadata_error(ident: object, params: dict) -> tuple[int, dict] | None:
    meta = params.get("_meta", {})
    if not isinstance(meta, dict):
        return 400, error(ident, -32602, "Metadata must be an object.")
    version = meta.get(PREFIX + "protocolVersion")
    modern = version is not None
    if modern:
        if version != MODERN:
            return 400, error(
                ident, -32022, "Unsupported protocol version.", {"supported": [MODERN, LEGACY], "requested": version}
            )
        if not isinstance(meta.get(PREFIX + "clientCapabilities"), dict):
            return 400, error(ident, -32602, "Declare clientCapabilities in request metadata.")
    return None


def _header_error(
    ident: object, method: str, params: dict, version: object, modern: bool, headers
) -> tuple[int, dict] | None:
    if headers is None:
        return None
    accept = {x.strip().split(";")[0] for x in headers.get("Accept", "").split(",")}
    if not {"application/json", "text/event-stream"} <= accept:
        return 406, error(ident, -32600, "Accept must include application/json and text/event-stream.")
    if modern:
        expected = {"MCP-Protocol-Version": version, "Mcp-Method": method}
        if method == "tools/call":
            expected["Mcp-Name"] = params.get("name")
        for key, value in expected.items():
            got = headers.get(key)
            if key == "Mcp-Name" and got and got.startswith("=?base64?") and got.endswith("?="):
                try:
                    got = base64.b64decode(got[9:-2], validate=True).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    got = None
            if got != value:
                return 400, error(ident, -32020, "Required MCP headers must match the request body.")
    elif method != "initialize" and headers.get("MCP-Protocol-Version") != LEGACY:
        return 400, error(
            ident, -32022, "Use a supported MCP-Protocol-Version header.", {"supported": [MODERN, LEGACY]}
        )
    return None


def _initialize(L: Ledger, credential: dict, params: dict) -> dict:
    return {
        "protocolVersion": LEGACY,
        "serverInfo": INFO,
        "capabilities": CAPABILITIES,
        "instructions": mcp_tools.UNTRUSTED.strip(),
    }


def _discover(L: Ledger, credential: dict, params: dict) -> dict:
    return {
        "supportedVersions": [MODERN, LEGACY],
        "capabilities": CAPABILITIES,
        "instructions": mcp_tools.UNTRUSTED.strip(),
    }


def _ping(L: Ledger, credential: dict, params: dict) -> dict:
    return {}


def _list_tools(L: Ledger, credential: dict, params: dict) -> dict:
    return {"tools": mcp_tools.definitions(credential["scope"])}


def _call_tool(L: Ledger, credential: dict, params: dict) -> dict:
    try:
        data = mcp_tools.call(L, credential["scope"], params.get("name"), params.get("arguments", {}), credential)
        result = {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
            "structuredContent": data,
            "isError": False,
        }
    except (api_v1.Error, ValueError) as e:
        result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "error": {
                                "code": getattr(e, "code", "invalid_arguments"),
                                "message": getattr(e, "message", "Invalid tool arguments. Check tools/list."),
                            }
                        }
                    ),
                }
            ],
            "isError": True,
        }
    return result


def stdio(L: Ledger, source: TextIO | None = None, target: TextIO | None = None) -> int:
    """Authenticate every line, so revocation and scopes also apply to local clients."""
    source, target = source or sys.stdin, target or sys.stdout
    bearer = os.environ.pop("FISCALM_MCP_TOKEN", "")
    if not tokens.verify(L, bearer):
        print("FISCALM_MCP_TOKEN is missing or invalid. Create a token first.", file=sys.stderr)
        return 2
    while True:
        try:
            line = source.readline(65537)
        except (UnicodeDecodeError, RecursionError):
            target.write(json.dumps(error(None, -32700, "Invalid JSON.")) + "\n")
            target.flush()
            continue
        if not line:
            return 0
        credential = tokens.verify(L, bearer)
        if not credential:
            print("The MCP token was revoked. Create or select another token.", file=sys.stderr)
            return 2
        try:
            if len(line) > 65536 or not line.endswith("\n"):
                print("MCP input must be newline-framed and at most 64 KB.", file=sys.stderr)
                return 2
            message = json.loads(line)
            _, result = dispatch(L, credential, message)
        except (ValueError, UnicodeDecodeError, RecursionError):
            result = error(None, -32700, "Invalid JSON.")
        if result is not None:
            target.write(json.dumps(result, ensure_ascii=False) + "\n")
            target.flush()
