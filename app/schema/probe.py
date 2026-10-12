# Standard imports.
from typing import (
    Literal,
)

# Third-party imports.
from pydantic import (
    Field,
    AliasGenerator,
    ConfigDict,
)
from pydantic.alias_generators import to_camel

# Local imports.
from app.schema.common import (
    ForbidExtraFields,
    MAX_INT32,
)

class HttpGet(ForbidExtraFields):
    port: int = Field(
        ge     = 1,
        le     = 65535,
        strict = True,
    )
    path: str | None = Field(
        default = None,
    )
    scheme: Literal["HTTP", "HTTPS"] | None = None

class ProbeSpec(ForbidExtraFields):

    # Requests use snake_case; model_dump(by_alias=True) emits the camelCase Kubernetes keys.
    model_config = ConfigDict(
        alias_generator = AliasGenerator(
            serialization_alias = to_camel,
        ),
    )

    # Required.
    http_get: HttpGet

    # Optional, need default.
    initial_delay_seconds: int | None =	Field(
        default = None,
        ge      = 0,
        le      = MAX_INT32,
        strict  = True,
    )
    period_seconds: int | None = Field(
        default = None,
        ge      = 1,
        le      = MAX_INT32,
        strict  = True,
    )
    timeout_seconds: int | None = Field(
        default = None,
        ge      = 1,
        le      = MAX_INT32,
        strict  = True,
    )
    success_threshold: int | None =	Field(
        default = None,
        ge      = 1,
        le      = MAX_INT32,
        strict  = True,
    )
    failure_threshold: int | None =	Field(
        default = None,
        ge      = 1,
        le      = MAX_INT32,
        strict  = True,
    )

class LivenessProbeSpec(ProbeSpec):
    success_threshold: int | None =	Field(
        default = None,
        ge      = 1,
        le      = 1,
        strict  = True,
    )
    