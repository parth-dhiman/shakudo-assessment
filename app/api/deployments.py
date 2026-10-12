# Standard imports.
from typing import (
    Any,
    Dict,
    Generator,
    List,
    Optional,
    Tuple,
)

# Third-party imports.
from flask import (
    abort,
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
    RollbackSpec,
)
from app.k8s.services import deployments as deployments_service

# ========== Helpers ==========

def _sse_event(data: str, name: Optional[str] = None) -> str:
    # One Server-Sent Event. Each line of data gets its own "data:" field; the browser joins them with newlines.
    # https://html.spec.whatwg.org/multipage/server-sent-events.html#event-stream-interpretation
    fields: List[str] = [f"event: {name}"] if name else []
    fields += [f"data: {line}" for line in data.replace("\r", "").split("\n")]
    return "\n".join(fields) + "\n\n"

def _sse_log_stream(events: Generator[Tuple[str, str], None, None]) -> Generator[str, None, None]:
    # Log lines are plain messages; "unavailable" carries Kubernetes' reason; "end" marks the end of this stream.
    # For a live log the browser then reconnects after `retry` ms and gets the last lines again.
    try:
        yield "retry: 5000\n\n"
        for (kind, text) in events:
            yield _sse_event(text, None if (kind == "line") else kind)
        yield _sse_event("", "end")
    finally:
        events.close()  # The browser went away: close the Kubernetes log connection too.

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

@app.post("/api/deployments/<uid>/rollback")
def rollback_deployment(uid: str) -> Tuple[Response, int]:
    namespace: str = request.args.get("namespace")
    spec: NamespaceAndUid = NamespaceAndUid.model_validate({
        "namespace": namespace,
        "uid": uid,
    })
    # An empty body rolls back to the previous revision; a malformed one is still a 400.
    rollback: RollbackSpec = RollbackSpec.model_validate(
        request.get_json(
            silent = True,
        ) if request.get_data() else {}
    )
    response: Dict[str, Any] = (
        deployments_service.
        rollback_deployment(spec, rollback)
    )
    return (jsonify(response), 200)

@app.get("/api/deployments/<uid>/history")
def rollback_info(uid: str) -> Tuple[Response, int]:
    namespace: str = request.args.get("namespace")
    spec: NamespaceAndUid = NamespaceAndUid.model_validate({
        "namespace": namespace,
        "uid": uid,
    })
    response: Dict[str, Any] = (
        deployments_service.
        rollback_info(spec)
    )
    return (jsonify(response), 200)

@app.get("/api/deployments/<uid>/logs")
def stream_logs(uid: str) -> Response:
    namespace: str = request.args.get("namespace")
    spec: NamespaceAndUid = NamespaceAndUid.model_validate({
        "namespace": namespace,
        "uid": uid,
    })
    pod: Optional[str] = request.args.get("pod")
    if not pod:
        abort(400, "Query parameter 'pod' is required.")
    previous: bool = (request.args.get("previous") == "true")

    # The deployment and pod are checked here, so a wrong one is still a JSON 404; only the log itself streams.
    events = deployments_service.pod_log_events(spec, pod, previous)
    return Response(
        _sse_log_stream(events),
        mimetype = "text/event-stream",
        headers  = {"Cache-Control": "no-cache"},
    )