# Standard imports.
from typing import (
    Any,
    Dict,
    Optional,
)
from datetime import datetime

# Third-party imports.
from kubernetes import client
from kubernetes.client.rest import ApiException
from app.exceptions import NotFoundError

# Local imports.
from app.k8s.api import (
    init_core_api,
    init_batch_api,
    init_apps_api,
)
from app.config import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
)
from app.schema.obj import ObjSpec
    
def _namespace_exists(namespace: str) -> bool:
    try:
        core_api: client.CoreV1Api = init_core_api()
        core_api.read_namespace(namespace)
        return True # No error raised, namespace exists.
    except ApiException as e:
        if (e.status == 404):
            return False
        else:
            raise

def _create_namespace(namespace: str) -> None:
    core_api: client.CoreV1Api = init_core_api()
    core_api.create_namespace(
        client.V1Namespace(
            metadata = client.V1ObjectMeta(
                name = namespace,
            )
        )
    )

def _read_managed_obj(
    name:      str,
    namespace: str,
) -> client.V1Deployment:
    
    apps_api: client.AppsV1Api = init_apps_api()

    obj: client.V1Deployment = apps_api.read_namespaced_deployment(
        name      = name,
        namespace = namespace,
    )

    # Verify that the object is managed by this service.
    labels = obj.metadata.labels or {}
    if (labels.get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE):
        raise NotFoundError(
            name      = name,
            namespace = namespace,
        )

    return obj

def _build_obj(obj_spec: ObjSpec) -> Dict[str, Any]:
    return {
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "metadata": {
        "name": obj_spec.name,
        "labels": {
            "app": "nginx",
            MANAGED_BY_LABEL: MANAGED_BY_VALUE,
        }
    },
    "spec": {
        "replicas": 3,
        "selector": {
            "matchLabels": {
                "app": "nginx"
            }
        },
        "template": {
            "metadata": {
                "labels": {
                    "app": "nginx"
                }
            },
            "spec": {
                "containers": [
                    {
                        "name": "nginx",
                        "image": "nginx:1.14.2",
                        "ports": [
                            {
                                "containerPort": 80
                            }
                        ]
                    }
                ]
            }
        }
    }
    }

def create_obj(obj_spec: ObjSpec) -> Dict[str, Any]:

    # Access the namespace, ensure it exists.
    namespace: str = obj_spec.namespace
    if not _namespace_exists(namespace):
        _create_namespace(namespace)

    # Build the Kubernetes object as a Dict object.
    body: Dict[str, Any] = _build_obj(obj_spec)

    # Create the object.
    apps_api: client.AppsV1Api = init_apps_api()
    response = apps_api.create_namespaced_deployment(
        body      = body,
        namespace = obj_spec.namespace,
    )

    # Return details in dict.
    return {
        "name":      response.metadata.name,
        "namespace": response.metadata.namespace,
        "uid":       response.metadata.uid,
    }
    