# Standard imports.

# Third-party imports.
from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
)

# Local imports.
from app.schema.common import (
    validate_dns_label,
    validate_image,
)

# ==================== Schema ====================

class ForbidExtraFields(BaseModel):
    model_config: ConfigDict = ConfigDict(
        extra = "forbid",
    ) 

class ObjSpec(ForbidExtraFields):

    # Required.
    name:  str
    image: str

    # Optional, but need default.
    namespace: str = "default"

    @field_validator("name", "namespace")
    @classmethod
    def validate_dns_label(cls, v: str) -> str:  
        return validate_dns_label(v)

    @field_validator("image")
    @classmethod
    def validate_image(cls, v: str) -> str:
        return validate_image(v)