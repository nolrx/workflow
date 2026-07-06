"""
Unified response helpers for consistent API responses

All API endpoints should use these helpers to ensure consistent response format:
- Success: {"success": true, "data": ..., "message": ...}
- Error: {"success": false, "error": "ERROR_CODE", "message": "..."}
"""
from typing import Any, Optional, Tuple

from flask import jsonify
from flask.wrappers import Response


def success_response(
    data: Optional[Any] = None,
    message: Optional[str] = None,
    status_code: int = 200
) -> Tuple[Response, int]:
    """
    Create a successful response.

    Args:
        data: The response data (optional)
        message: A success message (optional)
        status_code: HTTP status code (default: 200)

    Returns:
        Tuple of (Response, status_code)

    Example:
        return success_response({"user_id": "123"})
        return success_response(message="Operation completed")
        return success_response({"project": project.to_dict()}, status_code=201)
    """
    response = {"success": True}
    if data is not None:
        response["data"] = data
    if message:
        response["message"] = message
    return jsonify(response), status_code


def error_response(
    error_code: str,
    message: str,
    status_code: int = 400
) -> Tuple[Response, int]:
    """
    Create an error response.

    Args:
        error_code: A short error code (e.g., "NOT_FOUND", "BAD_REQUEST")
        message: Human-readable error message
        status_code: HTTP status code (default: 400)

    Returns:
        Tuple of (Response, status_code)

    Common error codes:
        - BAD_REQUEST (400): Invalid input data
        - UNAUTHORIZED (401): Authentication required
        - FORBIDDEN (403): Access denied
        - NOT_FOUND (404): Resource not found
        - CONFLICT (409): Resource conflict
        - SERVER_ERROR (500): Internal server error

    Example:
        return error_response("NOT_FOUND", "Project not found", 404)
        return error_response("BAD_REQUEST", "Title is required")
    """
    return jsonify({
        "success": False,
        "error": error_code,
        "message": message
    }), status_code
