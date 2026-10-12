# Standard imports.
from typing import (
    Literal,
)

# Third-party imports.
from pydantic import (
    Field,
)

# Local imports.
from app.schema.common import ForbidExtraFields

class ServiceSpec(ForbidExtraFields):

    # Required.
    port: int = Field(
        ge     = 1,
        le     = 65535,
        strict = True,
    )

    # Optional, do not need default.
    # Kubernetes handles defaulting for us (except for target port in this case).
    type:     Literal["ClusterIP", "NodePort"] | None = None
    protocol: Literal["TCP", "UDP", "SCTP"] | None = None
    target_port: int | None = Field(
        default = None, 
        ge      = 1,
        le      = 65535,
        strict  = True,
    )
    