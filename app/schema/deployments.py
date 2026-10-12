# Standard imports.
import re
from typing import (
    Dict,
    List,
    Self,
)
from decimal import Decimal

# Third-party imports.
from pydantic import (
    Field,
    field_validator,
    model_validator,
)
from kubernetes.utils import parse_quantity

# Local imports.
from app.config import MANAGED_BY_LABEL
from app.schema.common import (
    validate_dns_label,
    validate_image,
    validate_uid,
    ForbidExtraFields,
    MAX_INT32,
)
from app.schema.services import ServiceSpec
from app.schema.probe import (
    ProbeSpec,
    LivenessProbeSpec,
)

# ==================== Script-Level Constants ====================

_RESERVED_LABELS = (
    "app.kubernetes.io/name",
    MANAGED_BY_LABEL,
)

# Quantity grammar with ASCII digits only (https://kubernetes.io/docs/reference/kubernetes-api/common-definitions/quantity/).
_QUANTITY_NUMBER = r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
_QUANTITY_RE = re.compile(
    rf"[+-]?{_QUANTITY_NUMBER}"
    rf"(?:Ki|Mi|Gi|Ti|Pi|Ei|m|k|M|G|T|P|E|[eE][+-]?{_QUANTITY_NUMBER})?"
)

# ==================== Schema ====================

class ResourceObject(ForbidExtraFields):
    cpu:    str | None = None
    memory: str | None = None

    @field_validator("cpu", "memory")
    @classmethod
    def value_check(cls, v: str | None) -> str | None:

        # If resource value was not provided, let it be.
        if (v is None):
            return v

        # If it was provided, it must follow the quantity grammar and parse as a quantity.
        # parse_quantity alone also accepts "1K", "1_000" and non-ASCII digits, which the API server rejects.
        try:
            if not _QUANTITY_RE.fullmatch(v):
                raise ValueError(v)
            parsed_v: Decimal = parse_quantity(v)
        except ValueError:
            raise ValueError(
                "Must be a valid Kubernetes string quantity, "
                "like '100m', '0.5', or '64Mi'."
            )

        # Parsed value cannot be negative and must be finite.
        if (not parsed_v.is_finite()) or (parsed_v < 0):
            raise ValueError("Must be a finite, non-negative quantity.")

        # If we did not get halted by any of these conditionals, just return value as is.
        return v

class RequestsObject(ResourceObject):
    # Both required: every namespace the API creates has a quota on CPU and memory requests, and Kubernetes then
    # refuses pods that leave either out. Explicit requests also mean limits are never copied into requests.
    # https://kubernetes.io/docs/concepts/policy/resource-quotas/
    cpu:    str
    memory: str

class Resources(ForbidExtraFields):
    requests: RequestsObject
    limits:   ResourceObject | None = None

    @model_validator(mode="after")
    def requests_within_limits(self) -> Self:

        # Cannot do this check unless BOTH are available.
        if (self.requests is None) or (self.limits is None):
            return self

        for field in ("cpu", "memory"):
            request = getattr(self.requests, field)
            limit = getattr(self.limits, field)
            if (request is not None) and (limit is not None):
                if (parse_quantity(request) > parse_quantity(limit)):
                    raise ValueError(
                        f"requests.{field} ({request}) must not exceed limits.{field} ({limit})"
                    )
        return self

class DeploymentSpec(ForbidExtraFields):

    # Required.
    name:      str
    image:     str
    resources: Resources

    # Optional, but need default.
    namespace: str = "default"
    # strict: reject booleans and numeric strings instead of coercing them.
    replicas:  int = Field(default=1, ge=0, le=MAX_INT32, strict=True)

    # Optional, no defaults needed.
    # containerPort must be 0 < x < 65536 (kubectl explain deployment.spec.template.spec.containers.ports.containerPort).
    port:      int | None = Field(default=None, ge=1, le=65535, strict=True)
    command:   List[str] | None = None
    args:      List[str] | None = None
    env:       Dict[str, str] | None = None
    labels:    Dict[str, str] | None = None
    service:   ServiceSpec | None = None

    readiness_probe: ProbeSpec | None = None
    liveness_probe:  LivenessProbeSpec | None = None

    @field_validator("name", "namespace")
    @classmethod
    def check_dns_label(cls, v: str) -> str:
        return validate_dns_label(v)

    @field_validator("image")
    @classmethod
    def check_image(cls, v: str) -> str:
        return validate_image(v)

    @field_validator("labels")
    @classmethod
    def no_reserved_labels(cls, v: Dict[str, str] | None) -> Dict[str, str] | None:

        # If labels were not provided, let them be.
        if (v is None):
            return v

        # Key and value syntax is left to the API server (422 is mapped to 400).
        reserved: List[str] = sorted(set(v) & set(_RESERVED_LABELS))
        if reserved:
            raise ValueError(f"Reserved label keys cannot be set: {', '.join(reserved)}.")
        return v

class NamespaceAndUid(ForbidExtraFields):

    # The UID alone identifies a deployment; a namespace only narrows the search.
    namespace: str | None = None
    uid: str

    @field_validator("namespace")
    @classmethod
    def check_dns_label(cls, v: str | None) -> str | None:
        if (v is None):
            return v
        return validate_dns_label(v)

    @field_validator("uid")
    @classmethod
    def check_uid(cls, v: str) -> str:
        return validate_uid(v)

class Namespace(ForbidExtraFields):

    namespace: str | None = None

    @field_validator("namespace")
    @classmethod
    def check_dns_label(cls, v: str) -> str:
        if (v is None):
            return v
        return validate_dns_label(v)

class PatchSpec(ForbidExtraFields):

    """
    Spec needed to update a deployment. Must support at minimum: scaling replica count, updating the container image (triggering a rolling update).
    """

    # Both optional, but at least one is required.
    replicas: int | None = Field(default=None, ge=0, le=MAX_INT32, strict=True)
    image:    str | None = None

    @field_validator("image")
    @classmethod
    def check_image(cls, v: str | None) -> str | None:
        if (v is None):
            return v
        return validate_image(v)

    @model_validator(mode="after")
    def at_least_one_field(self) -> Self:
        if (self.replicas is None) and (self.image is None):
            raise ValueError("Provide at least one of: replicas, image.")
        return self

class RollbackSpec(ForbidExtraFields):

    # Optional: when omitted, roll back to the previous revision (like `kubectl rollout undo`).
    # Revisions are the deployment.kubernetes.io/revision numbers on the owned ReplicaSets, starting at 1.
    revision: int | None = Field(default=None, ge=1, strict=True)
    