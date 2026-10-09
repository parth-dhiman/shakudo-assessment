# Standard imports.
from functools import cache

# Third-party imports.
from kubernetes import (
    client,
    config,
)

@cache
def _init_client() -> client.ApiClient:
    config.load_kube_config() # We're managing a cluster, we're not inside it.
    return client.ApiClient()

def init_core_api() -> client.CoreV1Api:
    return client.CoreV1Api(
        api_client = _init_client(),
    )

def init_apps_api() -> client.AppsV1Api:
    return client.AppsV1Api(
        api_client = _init_client(),
    )

def init_batch_api() -> client.BatchV1Api:
    return client.BatchV1Api(
        api_client = _init_client(),
    )
