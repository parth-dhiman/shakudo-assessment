# Standard imports.
from typing import (
    Any,
    Dict,
)

# Third-party imports.
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

# Script-Level Constants.
_BASE_API_URL: str = "/api/obj"
_BASE_SPEC: Dict[str, Any] = {
    "name":  "hello",
    "image": "busybox",
}

def _create_obj(
    client:    FlaskClient,
    namespace: str,
):
    payload: Dict[str, Any] = {
        **_BASE_SPEC,
        "namespace": namespace,
    }
    response: TestResponse = client.post(
        _BASE_API_URL,
        json = payload,
    )
    assert (response.status_code == 201)

def test_lifecycle(
    client: FlaskClient,
    namespace: str,
):    
    _create_obj(client, namespace)