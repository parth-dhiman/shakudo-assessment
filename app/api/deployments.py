# Standard imports.
from typing import (
    Any,
    Dict,
    List,
    Tuple,
)

# Third-party imports.
from flask import (
    request,
    jsonify,
    Response,
)

# Local imports.
from app.api import app
from app.schema.deployments import (
    DeploymentSpec,
    Namespace,
    NamespaceAndUid,
    PatchSpec,
)
from app.k8s.services import deployments as deployments_service

# ========== Endpoints ==========

@app.post("/api/deployments")
def create_deployment() -> Tuple[Response, int]:
    deployment_spec: DeploymentSpec = DeploymentSpec.model_validate(
        request.get_json(
            silent = True,
        )
    )
    response: Dict[str, Any] = (
        deployments_service.
        create_deployment(deployment_spec)
    )
    return (jsonify(response), 201)

@app.get("/api/deployments")
def list_deployments() -> Tuple[Response, int]:
    namespace: str = request.args.get("namespace")
    spec: Namespace = Namespace.model_validate({
        "namespace": namespace,
    })
    response: Dict[str, Any] = (
        deployments_service.
        list_deployments(spec)
    )
    return (jsonify(response), 200)

@app.get("/api/deployments/<uid>")
def get_deployment(uid: str) -> Tuple[Response, int]:
    namespace: str = request.args.get("namespace")
    spec: NamespaceAndUid = NamespaceAndUid.model_validate({
        "namespace": namespace,
        "uid": uid,
    })
    response: Dict[str, Any] = (
        deployments_service.
        get_deployment(spec)
    )
    return (jsonify(response), 200)

@app.patch("/api/deployments/<uid>")
def patch_deployment(uid: str) -> Tuple[Response, int]:
    """
    Update a deployment. Must support at minimum: scaling replica count, updating the container image (triggering a rolling update).
    Pass it throughPatchSpec, and then deployments_service.patch_deployment.
    """
    namespace: str = request.args.get("namespace")
    spec: NamespaceAndUid = NamespaceAndUid.model_validate({
        "namespace": namespace,
        "uid": uid,
    })
    patch: PatchSpec = PatchSpec.model_validate(
        request.get_json(
            silent = True,
        )
    )
    response: Dict[str, Any] = (
        deployments_service.
        patch_deployment(spec, patch)
    )
    return (jsonify(response), 200)

@app.delete("/api/deployments/<uid>")
def delete_deployment(uid: str) -> Tuple[Response, int]:
    namespace: str = request.args.get("namespace")
    spec: NamespaceAndUid = NamespaceAndUid.model_validate({
        "namespace": namespace,
        "uid": uid,
    })
    deployments_service.delete_deployment(spec)
    return Response(
        status = 204,
    )

@app.post("/api/deployments/<uid>/restart")
def restart_deployment(uid: str) -> Tuple[Response, int]:
    namespace: str = request.args.get("namespace")
    spec: NamespaceAndUid = NamespaceAndUid.model_validate({
        "namespace": namespace,
        "uid": uid,
    })
    response: Dict[str, Any] = (
        deployments_service.
        restart_deployment(spec)
    )
    return (jsonify(response), 201)