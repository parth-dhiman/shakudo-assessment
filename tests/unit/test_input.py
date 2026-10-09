# Standard imports.
from typing import (
    Any,
    Dict,
    List,
    Set,
)

# Third-party imports.
import pytest
from pydantic import ValidationError
from app.config import MANAGED_BY_LABEL
from app.schema.deployments import (
    DeploymentSpec,
    Namespace,
    NamespaceAndUid,
    PatchSpec,
)

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
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input())
    assert (spec.name  == _BASE_INPUT["name"])
    assert (spec.image == _BASE_INPUT["image"])

def test_full_input():
    DeploymentSpec.model_validate(
        _input(
            namespace   = "default",
            command     = ["echo", "hello"],
            env         = {"secret_key": "secret_value"},
            args        = [],
            resources   = {
                "requests": {
                    "cpu"   : "100m",
                    "memory": "1Gi",
                },
                "limits": {
                    "cpu"   : "200m",
                    "memory": "2Gi",
                }
            }
        )
    )

def test_name_is_required():
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate({
            "image" : "busybox",
        })
    assert (_error_fields(exc_info) == {"name"})

def test_image_is_required():
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate({
            "name" : "hello",
        })
    assert (_error_fields(exc_info) == {"image"})

@pytest.mark.parametrize("field", ["name", "namespace"])
@pytest.mark.parametrize("value", ["a", "job-1", "123-abc", "a" * 63])
def test_valid_dns_labels(field: str, value: str):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(**{
        field: value,
    }))
    assert (getattr(spec, field) == value)

@pytest.mark.parametrize("field", ["name", "namespace"])
@pytest.mark.parametrize("value", ["Hello", "job_1", "-job", "job-", "job.1", "", "a" * 64, "jöb", " job", 123, None])
def test_invalid_dns_labels(field: str, value: str):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(**{
            field: value,
        }))
    assert (_error_fields(exc_info) == {field})

@pytest.mark.parametrize("image", [
    "busybox",
    "busybox:1.36",
    "library/busybox",
    "ghcr.io/org/app:v1",
    "localhost:5000/app",
    "busybox@sha256:" + "a" * 64,
    "doesnotexist/nope:1.0",  # well-formed but missing: a runtime STUCK, not a 400
])
def test_valid_images(image: str):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(**{
        "image": image,
    }))
    assert (getattr(spec, "image") == image)

@pytest.mark.parametrize("image", ["", "BusyBox", "busy box", "busybox:", ":latest", "a//b", "app/", "busybox\n"])
def test_invalid_images(image: str):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(**{
            "image": image,
        }))
    assert (_error_fields(exc_info) == {"image"})


@pytest.mark.parametrize("section",  ["requests", "limits"])
@pytest.mark.parametrize("resource", ["cpu", "memory"])
@pytest.mark.parametrize("value",    ["100m", "0.5", "1", "64Mi", "1Gi", "0", "2k", "1.5Gi", "1e3", "+1", ".5"])
def test_valid_resources(section: str, resource: int, value: Any):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        resources = {
            section: {
                resource: value,
            }
        }
    ))
    assert (getattr(getattr(spec.resources, section), resource) == value)

@pytest.mark.parametrize("section",  ["requests", "limits"])
@pytest.mark.parametrize("resource", ["cpu", "memory"])
@pytest.mark.parametrize("value",     [
    "lots", "-1", "NaN", "Infinity", "Mi", "",
    "1K",        # decimal kilo is a lowercase k; the API server rejects K
    "1ki",       # binary suffixes are case-sensitive
    "1_000",     # Python's Decimal accepts underscores, Kubernetes does not
    "١",         # non-ASCII digits
    "1.2.3",
    " 1",        # surrounding whitespace is rejected (the API server would trim it)
    1,           # quantities are strings in this API
])
def test_invalid_resources(section: str, resource: str, value: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            resources = {
                section: {
                    resource: value,
            }
        }
        ))
    assert (_error_fields(exc_info) == {f"resources.{section}.{resource}"})

@pytest.mark.parametrize(
    ("resource", "requested", "limit"),
    [
        ("cpu", "1", "1"),       # equal is allowed
        ("cpu", "500m", "1"),    # proves quantities are compared numerically, not as text
        ("memory", "512Mi", "1Gi"),
        ("cpu", "0.5", "500m"),  # equal across notations
        ("memory", "1G", "1Gi"), # 10^9 < 2^30
    ],
)
def test_requests_under_limits(
    resource: str,
    requested: Any,
    limit: Any,
) -> None:
    spec: DeploymentSpec = DeploymentSpec.model_validate(
        _input(
            resources={
                "requests": {resource: requested},
                "limits":   {resource: limit},
            }
        )
    )

    assert (getattr(spec.resources.requests, resource) == requested)
    assert (getattr(spec.resources.limits, resource) == limit)

@pytest.mark.parametrize(
    ("resource", "requested", "limit"),
    [
        ("cpu", "2", "1"),
        ("memory", "1Gi", "512Mi"),
        ("cpu", "1001m", "1"),
        ("memory", "2G", "1Gi"), # 2 * 10^9 > 2^30
    ],
)
def test_requests_exceeds_limit(
    resource: str,
    requested: Any,
    limit: Any,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(
            _input(
                resources = {
                    "requests": {resource: requested},
                    "limits":   {resource: limit},
                }
            )
        )
    assert (_error_fields(exc_info) == {"resources"})

@pytest.mark.parametrize("command", [
    ["echo", "hello"],
    ["sh", "-c", "echo hi"],
    [],
    None,
])
def test_valid_command(command: Any):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        command = command,
    ))
    assert (spec.command == command)

@pytest.mark.parametrize("command", [
    "echo hi",               # a string, not a list
    42,
    {"run": "echo hi"},
])
def test_invalid_command(command: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            command = command,
        ))
    assert (_error_fields(exc_info) == {"command"})

@pytest.mark.parametrize("args", [
    ["hello"],
    ["-c", "echo hi"],
    [],
    None,
])
def test_valid_args(args: Any):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        args = args,
    ))
    assert (spec.args == args)

@pytest.mark.parametrize("args", [
    "hello",                 # a string, not a list
    42,
    {"arg": "hello"},
])
def test_invalid_args(args: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            args = args,
        ))
    assert (_error_fields(exc_info) == {"args"})

@pytest.mark.parametrize("env", [
    {"GREETING": "hi"},
    {"A": ""},               # an empty value is allowed
    {},
    None,
])
def test_valid_env(env: Any):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        env = env,
    ))
    assert (spec.env == env)

@pytest.mark.parametrize("env", [
    ["A=1"],                 # a list, not an object
    "A=1",
    42,
])
def test_invalid_env(env: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            env = env,
        ))
    assert (_error_fields(exc_info) == {"env"})

# ==================== DeploymentSpec: defaults, unknown fields ====================

def test_defaults():
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input())
    assert (spec.namespace == "default")
    assert (spec.replicas  == 1)
    for field in ("port", "command", "args", "env", "labels", "resources"):
        assert (getattr(spec, field) is None)

@pytest.mark.parametrize("field", [
    "foo",
    "Replicas",              # field names are case-sensitive
    "containerPort",         # Kubernetes spelling, not ours
    "completions",           # a Job field
])
def test_unknown_fields(field: str):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(**{
            field: 1,
        }))
    assert (_error_fields(exc_info) == {field})

# ==================== DeploymentSpec: replicas, port ====================

@pytest.mark.parametrize("replicas", [0, 1, 3, 2_147_483_647])
def test_valid_replicas(replicas: int):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        replicas = replicas,
    ))
    assert (spec.replicas == replicas)

@pytest.mark.parametrize("replicas", [
    -1,
    2_147_483_648,           # spec.replicas is an int32
    2.5,
    3.0,                     # strict: a float is not an integer
    "3",                     # strict: no numeric strings
    True,                    # strict: a boolean is not an integer
    None,
])
def test_invalid_replicas(replicas: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            replicas = replicas,
        ))
    assert (_error_fields(exc_info) == {"replicas"})

@pytest.mark.parametrize("port", [1, 80, 8080, 65535, None])
def test_valid_port(port: Any):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        port = port,
    ))
    assert (spec.port == port)

@pytest.mark.parametrize("port", [0, -1, 65536, 80.0, "80", True])
def test_invalid_port(port: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            port = port,
        ))
    assert (_error_fields(exc_info) == {"port"})

# ==================== DeploymentSpec: labels, list and map items ====================

@pytest.mark.parametrize(
    "labels",
    [
        {"team": "demo"},
        {"app.kubernetes.io/part-of": "shop"},   # other recommended labels are fine
        {"bad key!": "x"},                       # key/value syntax is left to the API server (422 -> 400)
        {},
        None,
    ],
    ids = ["plain", "other-recommended-label", "syntax-left-to-api-server", "empty", "none"],
)
def test_valid_labels(labels: Any):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        labels = labels,
    ))
    assert (spec.labels == labels)

@pytest.mark.parametrize(
    "labels",
    [
        {"app.kubernetes.io/name": "x"},
        {MANAGED_BY_LABEL: "x"},
        {"team": "demo", "app.kubernetes.io/name": "x"},
        ["team=demo"],
        "team=demo",
    ],
    ids = ["reserved-name", "reserved-managed-by", "one-reserved-among-others", "list", "string"],
)
def test_invalid_labels(labels: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            labels = labels,
        ))
    assert (_error_fields(exc_info) == {"labels"})

@pytest.mark.parametrize("field", ["command", "args"])
def test_list_items_must_be_strings(field: str):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(**{
            field: ["echo", 1],
        }))
    assert (_error_fields(exc_info) == {f"{field}.1"})

@pytest.mark.parametrize("field", ["env", "labels"])
def test_map_values_must_be_strings(field: str):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(**{
            field: {"A": 1},
        }))
    assert (_error_fields(exc_info) == {f"{field}.A"})

# ==================== DeploymentSpec: resources shape ====================

@pytest.mark.parametrize(
    "resources",
    [
        {},
        {"requests": {}},
        {"limits": {"cpu": "1"}},                # limits alone: requests default to limits
    ],
    ids = ["empty", "empty-requests", "limits-only"],
)
def test_valid_resources_shape(resources: Any):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        resources = resources,
    ))
    assert (spec.resources is not None)

@pytest.mark.parametrize(
    ("resources", "field"),
    [
        ({"requests": {"gpu": "1"}}, "resources.requests.gpu"),
        ({"claims": []},             "resources.claims"),
        ("100m",                     "resources"),
    ],
    ids = ["unknown-resource", "unknown-section", "not-an-object"],
)
def test_invalid_resources_shape(resources: Any, field: str):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            resources = resources,
        ))
    assert (_error_fields(exc_info) == {field})

# ==================== NamespaceAndUid ====================

_UID: str = "e5f1f2f8-2939-43ed-8e53-c8c980b220e2"

def test_uid_alone_is_enough():
    spec: NamespaceAndUid = NamespaceAndUid.model_validate({
        "uid": _UID,
    })
    assert (spec.uid == _UID)
    assert (spec.namespace is None)

@pytest.mark.parametrize("uid", [_UID, _UID.upper()])
def test_valid_uids(uid: str):
    spec: NamespaceAndUid = NamespaceAndUid.model_validate({
        "uid": uid,
    })
    assert (spec.uid == _UID)                    # normalised to lowercase, as the API server emits it

@pytest.mark.parametrize("uid", [
    "",
    "not-a-uid",
    _UID.replace("-", ""),                       # no hyphens
    "{" + _UID + "}",
    "urn:uuid:" + _UID,
    _UID + "\n",
    _UID[:-1],                                   # one digit short
    _UID[:-1] + "g",                             # not hex
])
def test_invalid_uids(uid: str):
    with pytest.raises(ValidationError) as exc_info:
        NamespaceAndUid.model_validate({
            "uid": uid,
        })
    assert (_error_fields(exc_info) == {"uid"})

def test_uid_is_required():
    with pytest.raises(ValidationError) as exc_info:
        NamespaceAndUid.model_validate({
            "namespace": "default",
        })
    assert (_error_fields(exc_info) == {"uid"})

@pytest.mark.parametrize("namespace", ["default", "a" * 63, None])
def test_valid_uid_namespaces(namespace: Any):
    spec: NamespaceAndUid = NamespaceAndUid.model_validate({
        "namespace": namespace,
        "uid":       _UID,
    })
    assert (spec.namespace == namespace)

@pytest.mark.parametrize("namespace", ["Default", "", "a" * 64, "kube_system"])
def test_invalid_uid_namespaces(namespace: str):
    with pytest.raises(ValidationError) as exc_info:
        NamespaceAndUid.model_validate({
            "namespace": namespace,
            "uid":       _UID,
        })
    assert (_error_fields(exc_info) == {"namespace"})

# ==================== Namespace (list filter) ====================

@pytest.mark.parametrize("namespace", ["default", "kube-system", "a" * 63, None])
def test_valid_namespace_filters(namespace: Any):
    spec: Namespace = Namespace.model_validate({
        "namespace": namespace,
    })
    assert (spec.namespace == namespace)

def test_namespace_filter_is_optional():
    assert (Namespace.model_validate({}).namespace is None)

@pytest.mark.parametrize("namespace", ["Bad_NS", "", "a" * 64, 123])
def test_invalid_namespace_filters(namespace: Any):
    with pytest.raises(ValidationError) as exc_info:
        Namespace.model_validate({
            "namespace": namespace,
        })
    assert (_error_fields(exc_info) == {"namespace"})

# ==================== PatchSpec ====================

@pytest.mark.parametrize(
    "patch",
    [
        {"replicas": 0},
        {"replicas": 2_147_483_647},
        {"image": "nginx:1.27"},
        {"replicas": 3, "image": "nginx"},
        {"replicas": 3, "image": None},          # null means "not provided"
    ],
    ids = ["scale-to-zero", "scale-to-int32-max", "image-only", "both", "image-null"],
)
def test_valid_patches(patch: Dict[str, Any]):
    spec: PatchSpec = PatchSpec.model_validate(patch)
    for field, value in patch.items():
        assert (getattr(spec, field) == value)

@pytest.mark.parametrize("replicas", [-1, 2_147_483_648, 2.5, "3", True])
def test_invalid_patch_replicas(replicas: Any):
    with pytest.raises(ValidationError) as exc_info:
        PatchSpec.model_validate({
            "replicas": replicas,
        })
    assert (_error_fields(exc_info) == {"replicas"})

@pytest.mark.parametrize("image", ["", "NGINX", "nginx:", "nginx latest"])
def test_invalid_patch_images(image: str):
    with pytest.raises(ValidationError) as exc_info:
        PatchSpec.model_validate({
            "image": image,
        })
    assert (_error_fields(exc_info) == {"image"})

@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"replicas": None},
        {"image": None},
        {"replicas": None, "image": None},
    ],
    ids = ["empty", "replicas-null", "image-null", "both-null"],
)
def test_patch_needs_at_least_one_field(patch: Dict[str, Any]):
    with pytest.raises(ValidationError) as exc_info:
        PatchSpec.model_validate(patch)
    assert (_error_fields(exc_info) == {""})    # a model-level error has no field location

@pytest.mark.parametrize("field", ["namespace", "uid", "name", "labels", "env"])
def test_patch_unknown_fields(field: str):
    with pytest.raises(ValidationError) as exc_info:
        PatchSpec.model_validate({
            "replicas": 1,
            field:      "x",
        })
    assert (_error_fields(exc_info) == {field})

@pytest.mark.parametrize("body", [None, [], "replicas=3", 3])
def test_patch_body_must_be_an_object(body: Any):
    with pytest.raises(ValidationError) as exc_info:
        PatchSpec.model_validate(body)
    assert (_error_fields(exc_info) == {""})
