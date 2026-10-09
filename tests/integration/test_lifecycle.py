# Standard imports.
import time
from typing import (
    Any,
    Dict,
    List,
    Optional,
)

# Third-party imports.
from flask.testing import FlaskClient
from kubernetes import client as k8s
from werkzeug.test import TestResponse

# Local imports.
from app.k8s.api import (
    init_apps_api,
    init_core_api,
)

# Script-Level Constants.
_BASE_API_URL: str = "/api/deployments"
_BASE_SPEC: Dict[str, Any] = {
    "name":  "hello",
    "image": "busybox",
}
_RESTARTED_AT_ANNOTATION: str = "kubectl.kubernetes.io/restartedAt"
_CASCADE_TIMEOUT_SECONDS: int = 60

def _create_deployment(
    client:    FlaskClient,
    namespace: str,
) -> str:
    payload: Dict[str, Any] = {
        **_BASE_SPEC,
        "namespace": namespace,
    }
    response: TestResponse = client.post(
        _BASE_API_URL,
        json = payload,
    )
    assert (response.status_code == 201)
    return response.get_json()["uid"]

def _list_deployments(
    client:    FlaskClient,
    namespace: Optional[str] = None,
) -> List[str]:
    # Returns the listed UIDs; no namespace lists every namespace.
    response: TestResponse = client.get(
        _BASE_API_URL,
        query_string = {"namespace": namespace} if namespace else {},
    )
    assert (response.status_code == 200)
    return [item["uid"] for item in response.get_json()["items"]]

def _get_deployment(
    client: FlaskClient,
    uid:    str,
    status: int = 200,
) -> Dict[str, Any]:
    # The UID alone identifies the deployment; no namespace needed.
    response: TestResponse = client.get(
        f"{_BASE_API_URL}/{uid}",
    )
    assert (response.status_code == status)
    return response.get_json()

def _patch_deployment(
    client: FlaskClient,
    uid:    str,
    patch:  Dict[str, Any],
) -> Dict[str, Any]:
    response: TestResponse = client.patch(
        f"{_BASE_API_URL}/{uid}",
        json = patch,
    )
    assert (response.status_code == 200)
    body: Dict[str, Any] = response.get_json()
    assert (body["uid"] == uid)

    # The response has no pod template, so check the change on the cluster object itself.
    deployment: k8s.V1Deployment = init_apps_api().read_namespaced_deployment(
        name      = body["name"],
        namespace = body["namespace"],
    )
    if ("replicas" in patch):
        assert (deployment.spec.replicas == patch["replicas"])
    if ("image" in patch):
        assert (deployment.spec.template.spec.containers[0].image == patch["image"])
    return body

def _restart_deployment(
    client: FlaskClient,
    uid:    str,
) -> Dict[str, Any]:
    response: TestResponse = client.post(
        f"{_BASE_API_URL}/{uid}/restart",
    )
    assert (response.status_code == 201)
    body: Dict[str, Any] = response.get_json()
    assert (body["uid"] == uid)

    # Like `kubectl rollout restart`: the pod template carries the restartedAt annotation.
    deployment: k8s.V1Deployment = init_apps_api().read_namespaced_deployment(
        name      = body["name"],
        namespace = body["namespace"],
    )
    annotations: Dict[str, str] = deployment.spec.template.metadata.annotations or {}
    assert (_RESTARTED_AT_ANNOTATION in annotations)
    return body

def _delete_deployment(
    client:    FlaskClient,
    uid:       str,
    namespace: str,
) -> None:
    response: TestResponse = client.delete(
        f"{_BASE_API_URL}/{uid}",
    )
    assert (response.status_code == 204)

    # Background cascade: the garbage collector removes the owned ReplicaSets and Pods shortly after.
    # The test namespace holds only this deployment, so anything left in it is a leftover.
    apps_api: k8s.AppsV1Api = init_apps_api()
    core_api: k8s.CoreV1Api = init_core_api()
    deadline: float = time.monotonic() + _CASCADE_TIMEOUT_SECONDS
    while True:
        leftovers: List[str] = [
            obj.metadata.name
            for obj in (
                apps_api.list_namespaced_replica_set(namespace).items
                + core_api.list_namespaced_pod(namespace).items
            )
        ]
        if not leftovers:
            break
        assert (time.monotonic() < deadline), f"Not garbage-collected: {leftovers}"
        time.sleep(1)

def test_lifecycle(
    client: FlaskClient,
    namespace: str,
):
    # Create.
    uid: str = _create_deployment(client, namespace)

    # It is listed, both under its namespace and across all namespaces.
    assert (_list_deployments(client, namespace) == [uid])
    assert (uid in _list_deployments(client))

    # Detail, read live from the cluster.
    deployment: Dict[str, Any] = _get_deployment(client, uid)
    assert (deployment["name"]      == _BASE_SPEC["name"])
    assert (deployment["namespace"] == namespace)
    assert (deployment["replicas"]["desired"] == 1)

    # Scale, and the replica count changes.
    _patch_deployment(client, uid, {"replicas": 3})
    assert (_get_deployment(client, uid)["replicas"]["desired"] == 3)

    # Image update (starts a rolling update).
    _patch_deployment(client, uid, {"image": "busybox:1.36"})

    # Rollout restart.
    _restart_deployment(client, uid)

    # Delete, and it is gone.
    _delete_deployment(client, uid, namespace)
    _get_deployment(client, uid, status=404)
    assert (_list_deployments(client, namespace) == [])
