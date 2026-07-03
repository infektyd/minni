from typing import Any


def make_response(result: Any, request_id: Any = None) -> dict:
    """Build a JSON-RPC success response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def make_error(
    code: int, message: str, request_id: Any = None, *, data: Any = None
) -> dict:
    """Build a JSON-RPC error response (optional structured error.data)."""
    error: dict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error,
    }

