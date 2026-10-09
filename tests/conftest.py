
# Standard imports.
from typing import Iterator
import secrets

# Third-party imports.
import pytest
from flask.testing import FlaskClient
from kubernetes import client as k8s
from kubernetes.client.exceptions import ApiException

# Local imports.
from app.api import app
from app.k8s.api import init_core_api

@pytest.fixture
def client() -> FlaskClient:
    app.config['TESTING'] = True
    return app.test_client()

@pytest.fixture
def namespace() -> Iterator[str]:
    namespace: str = f"test-{secrets.token_hex(3)}"
    yield namespace
    core_api: k8s.CoreV1Api = init_core_api()
    try:
        core_api.delete_namespace(
            name = namespace,
        )
    except ApiException as e:
         # 404: If namespace was somehow never created
         # i.e., test failed midway. That is acceptable.
        if (e.status != 404):
            raise

