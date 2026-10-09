# Standard imports.
from typing import (
    Any,
    Dict,
    Set,
)

# Third-party imports.
import pytest
from pydantic import ValidationError
from app.schema.obj import ObjSpec

_BASE_INPUT: Dict[str, Any] = {
    "name":  "hello",
    "image": "busybox",
}

def _input(**overrides):
    return {
        **_BASE_INPUT,
        **overrides,
    }

def _error_fields(exc_info) -> Set[str]:
    return {
        ".".join(str(p) for p in e["loc"])
        for e in exc_info.value.errors()
    }

def test_base_input():
    spec: ObjSpec = ObjSpec.model_validate(_input())
    assert (spec.name  == _BASE_INPUT["name"])
    assert (spec.image == _BASE_INPUT["image"])

def test_name_is_required():
    with pytest.raises(ValidationError) as exc_info:
        ObjSpec.model_validate({
            "image" : "busybox",
        })
    assert (_error_fields(exc_info) == {"name"})

def test_image_is_required():
    with pytest.raises(ValidationError) as exc_info:
        ObjSpec.model_validate({
            "name" : "hello",
        })
    assert (_error_fields(exc_info) == {"image"})

@pytest.mark.parametrize("field", ["name", "namespace"])
@pytest.mark.parametrize("value", ["a", "job-1", "123-abc", "a" * 63])
def test_valid_dns_labels(field: str, value: str):
    spec: ObjSpec = ObjSpec.model_validate(_input(**{
        field: value,
    }))
    assert (getattr(spec, field) == value)