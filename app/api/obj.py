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
from app.schema.obj import ObjSpec
from app.k8s.services import obj as obj_service

# ========== Endpoints ==========

@app.post("/api/obj")
def create_obj() -> Tuple[Response, int]:
    obj_spec: ObjSpec = ObjSpec.model_validate(
        request.get_json(
            silent = True,
        )
    )
    response: Dict[str, Any] = (
        obj_service.
        create_obj(obj_spec)
    )
    return (jsonify(response), 201)