# Standard imports.
import json
import logging
from typing import (
    Any,
    Dict,
    List,
    Tuple,
)

# Third-party imports.
from flask import (
    Flask,
    Response,
    jsonify,
)
from pydantic import ValidationError
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError
from werkzeug.exceptions import HTTPException
from app.exceptions import NotFoundError

logger = logging.getLogger(__name__)
    
# ---------- Helpers -----------

def _error(
    status:  int,
    code:    str,
    message: str,
    details: List[Dict[str, str]] | None = None,
) -> Tuple[Response, int]:
    
    body: Dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
        },
    }

    if details:
        body["error"]["details"] = details
    
    return (jsonify(body), status)

# ---------- Handlers -----------

def _handle_validation_e(e: ValidationError) -> Tuple[Response, int]:

    details = [
        {
            "message": error["msg"],
            "field": ".".join(str(part) for part in error["loc"]) or "body",
        }
        for error in e.errors()
    ]

    logger.warning("Invalid Payload: %s", details)

    return _error(
        status  = 400,
        code    = "INVALID_PAYLOAD",
        message = "Payload sent was invalid.",
        details = details,
    )

def _handle_k8s_api_e(e: ApiException) -> Tuple[Response, int]:

    # Determine an error code based on status.
    # Translate Kubernetes' status into ours. Anything unlisted (401, 403, 429, 5xx) is our side's fault.
    _MAP_STATUS: Dict[int, Tuple[int, str]] = {
        404: (404, "NOT_FOUND"),
        409: (409, "CONFLICT"),
        422: (400, "INVALID_PAYLOAD"),
    }
    (status, code) = _MAP_STATUS.get(e.status, (500, "KUBERNETES_ERROR"))

    # Define a message value.
    try:
        body: Dict[str, Any] = json.loads(e.body)
        message: str = body["message"]
    except (TypeError, json.JSONDecodeError, KeyError):
        if e.reason:
            message: str = e.reason
        else:
            message: str = "Kubernetes API error."
    
    if (status >= 500):
        logger.exception("Kubernetes API call failed (%s): %s", e.status, message)
    else:
        logger.warning("Kubernetes API returned %s: %s", e.status, message)

    return _error(
        status = status,
        code   = code,
        message = message,
    )

def _handle_k8s_unreachable_e(e: HTTPError) -> Tuple[Response, int]:
    logger.exception("Kubernetes API was unreachable: %s", str(e))
    return _error(
        status  = 503,
        code    = "K8S_UNREACHABLE",
        message = "Could not reach Kubernetes API.",
    )

def _handle_not_found_e(e: NotFoundError) -> Tuple[Response, int]:
    logger.warning("Not found error, %s", str(e))
    return _error(
        status  = 404,
        code    = "NOT_FOUND",
        message = str(e),
    )

def _handle_unexpected_e(e: Exception) -> Tuple[Response, int]:
    logger.exception("Unhandled Error: %s", str(e))
    return _error(
        status = 500,
        code   = "INTERNAL_ERROR",
        message = "There was an error on our side.",
    )

def _handle_flask_e(e: HTTPException) -> Tuple[Response, int]:
    # No log required, since this is the top-most layer where an error will actually land.
    return _error(
        status  = e.code,
        code    = e.name.upper().replace(" ", "_"),
        message = e.description,
    )

# ---------- Public Function -----------

def register_error_handlers(app: Flask) -> None:
    app.register_error_handler(ValidationError, _handle_validation_e)
    app.register_error_handler(ApiException,    _handle_k8s_api_e)
    app.register_error_handler(HTTPError,       _handle_k8s_unreachable_e)
    app.register_error_handler(NotFoundError,   _handle_not_found_e)
    app.register_error_handler(Exception,       _handle_unexpected_e)
    app.register_error_handler(HTTPException,   _handle_flask_e)