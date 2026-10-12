# Standard imports.
from typing import (
    Any,
    Dict,
    List,
)

# Third-party imports.
from kubernetes import client

# Local imports.
from app.k8s.api import (
    init_core_api,
)
from app.schema.services import ServiceSpec
from app.k8s.services.common import (
    create_namespace,
    namespace_exists,
)

def _build_service(
    service_spec: ServiceSpec,
    deployment:   client.V1Deployment,
) -> Dict[str, Any]:

    # Port: set only the optional fields that were provided.
    # Without a targetPort, Kubernetes uses `port`, so default it to the container port when the Deployment declares one.
    # https://kubernetes.io/docs/concepts/services-networking/service/#defining-a-service
    port: Dict[str, Any] = {"port": service_spec.port}
    container_ports: List[client.V1ContainerPort] = deployment.spec.template.spec.containers[0].ports or []
    if (service_spec.target_port is not None):
        port["targetPort"] = service_spec.target_port
    elif container_ports:
        port["targetPort"] = container_ports[0].container_port
    if (service_spec.protocol is not None):
        port["protocol"] = service_spec.protocol

    # Same selector as the Deployment, so the Service targets exactly its pods.
    spec: Dict[str, Any] = {
        "selector": deployment.spec.selector.match_labels,
        "ports":    [port],
    }
    if (service_spec.type is not None):
        spec["type"] = service_spec.type

    # The ownerReference lets the garbage collector delete the Service along with its Deployment.
    # https://kubernetes.io/docs/concepts/overview/working-with-objects/owners-dependents/
    return {
        "apiVersion": "v1",
        "kind":       "Service",
        "metadata": {
            "name":      deployment.metadata.name,
            "namespace": deployment.metadata.namespace,
            "labels":    deployment.metadata.labels,
            "ownerReferences": [{
                "apiVersion": "apps/v1",
                "kind":       "Deployment",
                "name":       deployment.metadata.name,
                "uid":        deployment.metadata.uid,
            }],
        },
        "spec": spec,
    }

def create_service(
    deployment:   client.V1Deployment,
    service_spec: ServiceSpec,
) -> Dict[str, Any]:

    # The Service lives next to its Deployment: owner references cannot cross namespaces.
    namespace: str = deployment.metadata.namespace

    # Ensure that the namespace exists.
    if not namespace_exists(namespace):
        create_namespace(namespace)

    # Build the Service object as a Dict object.
    body: Dict[str, Any] = _build_service(service_spec, deployment)

    # Create the object.
    core_api: client.CoreV1Api = init_core_api()
    response: client.V1Service = core_api.create_namespaced_service(
        body      = body,
        namespace = namespace,
    )

    # Return details in dict. Read them from the response: the server defaults type and targetPort,
    # and allocates the clusterIP and (for NodePort) the nodePort.
    service_port: client.V1ServicePort = response.spec.ports[0]
    return {
        "accessPort": service_port.port,
        "nodePort": service_port.node_port,
    }