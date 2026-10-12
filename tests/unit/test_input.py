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
    RollbackSpec,
)

_BASE_INPUT: Dict[str, Any] = {
    "name":      "hello",
    "image":     "busybox",
    "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}},
}

# The smallest valid requests: quantity tests start from these, so a limit is never below its request.
_ZERO_REQUESTS: Dict[str, str] = {"cpu": "0", "memory": "0"}

def _resources(section: str, values: Dict[str, Any]) -> Dict[str, Any]:
    # resources with `values` under `section` ("requests" or "limits"), on top of the zero requests.
    if (section == "requests"):
        return {"requests": {**_ZERO_REQUESTS, **values}}
    return {"requests": _ZERO_REQUESTS, section: values}

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
        DeploymentSpec.model_validate({key: value for (key, value) in _BASE_INPUT.items() if key != "name"})
    assert (_error_fields(exc_info) == {"name"})

def test_image_is_required():
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate({key: value for (key, value) in _BASE_INPUT.items() if key != "image"})
    assert (_error_fields(exc_info) == {"image"})

@pytest.mark.parametrize("field", ["name", "namespace"])
@pytest.mark.parametrize("value", ["a", "job-1", "a" * 63])
def test_valid_dns_labels(field: str, value: str):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(**{
        field: value,
    }))
    assert (getattr(spec, field) == value)

@pytest.mark.parametrize("field", ["name", "namespace"])
@pytest.mark.parametrize("value", ["Hello", "123-abc", "job_1", "-job", "job-", "job.1", "", "a" * 64, "jöb", " job", 123, None])
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
        resources = _resources(section, {resource: value}),
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
            resources = _resources(section, {resource: value}),
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
                "requests": {**_ZERO_REQUESTS, resource: requested},
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
                    "requests": {**_ZERO_REQUESTS, resource: requested},
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
    for field in ("port", "command", "args", "env", "labels"):
        assert (getattr(spec, field) is None)
    assert (spec.resources.limits is None)

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
        {"requests": {"cpu": "100m", "memory": "64Mi"}},
        {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {}},
        {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"cpu": "1"}},   # limits stay optional, per resource
        {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": None},
    ],
    ids = ["requests-only", "empty-limits", "one-limit", "limits-null"],
)
def test_valid_resources_shape(resources: Any):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        resources = resources,
    ))
    assert (spec.resources.requests.cpu    == "100m")
    assert (spec.resources.requests.memory == "64Mi")

@pytest.mark.parametrize(
    ("resources", "field"),
    [
        ({"requests": {"cpu": "100m", "memory": "64Mi", "gpu": "1"}}, "resources.requests.gpu"),
        ({"requests": {"cpu": "100m", "memory": "64Mi"}, "claims": []}, "resources.claims"),
        ("100m",                                                         "resources"),
    ],
    ids = ["unknown-resource", "unknown-section", "not-an-object"],
)
def test_invalid_resources_shape(resources: Any, field: str):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            resources = resources,
        ))
    assert (_error_fields(exc_info) == {field})

# Every namespace the API creates has a quota on CPU and memory requests, so both requests are always required.
@pytest.mark.parametrize(
    ("resources", "fields"),
    [
        (None,                                   {"resources"}),
        ({},                                     {"resources.requests"}),
        ({"limits": {"cpu": "1", "memory": "1Gi"}}, {"resources.requests"}),   # limits are not copied into requests
        ({"requests": {}},                       {"resources.requests.cpu", "resources.requests.memory"}),
        ({"requests": {"cpu": "100m"}},          {"resources.requests.memory"}),
        ({"requests": {"memory": "64Mi"}},       {"resources.requests.cpu"}),
        ({"requests": {"cpu": None, "memory": "64Mi"}}, {"resources.requests.cpu"}),
    ],
    ids = ["null", "empty", "limits-only", "empty-requests", "cpu-only", "memory-only", "cpu-null"],
)
def test_requests_are_required(resources: Any, fields: Set[str]):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            resources = resources,
        ))
    assert (_error_fields(exc_info) == fields)

def test_resources_are_required():
    body: Dict[str, Any] = {key: value for (key, value) in _BASE_INPUT.items() if key != "resources"}
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(body)
    assert (_error_fields(exc_info) == {"resources"})

# ==================== DeploymentSpec: service ====================

def test_service_is_optional():
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input())
    assert (spec.service is None)

def test_service_defaults():
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        service = {"port": 80},
    ))
    assert (spec.service.port == 80)
    for field in ("type", "protocol", "target_port"):
        assert (getattr(spec.service, field) is None)

@pytest.mark.parametrize(
    "service",
    [
        {"port": 80, "target_port": 8080, "type": "NodePort", "protocol": "UDP"},
        {"port": 80, "target_port": None, "type": None,       "protocol": None},
    ],
    ids = ["all-fields", "explicit-nulls"],
)
def test_valid_services(service: Dict[str, Any]):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        service = service,
    ))
    # Values come back unchanged: a validator that forgets to return would turn them into None.
    assert (spec.service.model_dump() == service)

@pytest.mark.parametrize("field", ["port", "target_port"])
@pytest.mark.parametrize("value", [1, 80, 8080, 65535])
def test_valid_service_ports(field: str, value: int):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        service = {"port": 80, field: value},
    ))
    assert (getattr(spec.service, field) == value)

@pytest.mark.parametrize("field", ["port", "target_port"])
@pytest.mark.parametrize("value", [
    0,
    -1,
    65536,
    80.0,                    # strict: a float is not an integer
    "80",                    # strict: no numeric strings
    True,                    # strict: a boolean is not an integer
])
def test_invalid_service_ports(field: str, value: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            service = {"port": 80, field: value},
        ))
    assert (_error_fields(exc_info) == {f"service.{field}"})

@pytest.mark.parametrize(
    "service",
    [
        {},
        {"port": None},
        {"target_port": 8080, "type": "NodePort"},
    ],
    ids = ["empty", "null", "other-fields-only"],
)
def test_service_port_is_required(service: Dict[str, Any]):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            service = service,
        ))
    assert (_error_fields(exc_info) == {"service.port"})

@pytest.mark.parametrize("type", ["ClusterIP", "NodePort", None])
def test_valid_service_types(type: Any):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        service = {"port": 80, "type": type},
    ))
    assert (spec.service.type == type)

@pytest.mark.parametrize("type", [
    "LoadBalancer",          # a real Service type, but not one we support
    "ExternalName",          # a real Service type, but not one we support
    "clusterip",             # values are case-sensitive
    "NODEPORT",
    " ClusterIP",            # no trimming
    "",
    1,
])
def test_invalid_service_types(type: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            service = {"port": 80, "type": type},
        ))
    assert (_error_fields(exc_info) == {"service.type"})

@pytest.mark.parametrize("protocol", ["TCP", "UDP", "SCTP", None])
def test_valid_service_protocols(protocol: Any):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        service = {"port": 80, "protocol": protocol},
    ))
    assert (spec.service.protocol == protocol)

@pytest.mark.parametrize("protocol", [
    "tcp",                   # values are case-sensitive
    "HTTP",                  # an application protocol, not a transport one
    "TCP ",                  # no trimming
    "",
    6,                       # TCP's IP protocol number is not accepted
])
def test_invalid_service_protocols(protocol: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            service = {"port": 80, "protocol": protocol},
        ))
    assert (_error_fields(exc_info) == {"service.protocol"})

@pytest.mark.parametrize("field", [
    "foo",
    "targetPort",            # Kubernetes spelling, not ours
    "nodePort",              # allocated by the API server
    "name",                  # taken from the deployment
    "selector",              # taken from the deployment
])
def test_service_unknown_fields(field: str):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            service = {"port": 80, field: 1},
        ))
    assert (_error_fields(exc_info) == {f"service.{field}"})

@pytest.mark.parametrize(
    "service",
    [80, "80", True, [], [{"port": 80}]],
    ids = ["int", "string", "bool", "empty-list", "list-of-objects"],
)
def test_service_must_be_an_object(service: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            service = service,
        ))
    assert (_error_fields(exc_info) == {"service"})

# ==================== DeploymentSpec: probes ====================

# Both probe fields share ProbeSpec's rules; LivenessProbeSpec only narrows success_threshold.
_PROBE_FIELDS: List[str] = ["readiness_probe", "liveness_probe"]
_PROBE_TIMINGS: List[str] = [
    "initial_delay_seconds",
    "period_seconds",
    "timeout_seconds",
    "success_threshold",
    "failure_threshold",
]
_PROBE: Dict[str, Any] = {"http_get": {"port": 80}}

def _probe(**overrides):
    return {
        **_PROBE,
        **overrides,
    }

def test_probes_are_optional():
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input())
    assert (spec.readiness_probe is None)
    assert (spec.liveness_probe  is None)

@pytest.mark.parametrize("field", _PROBE_FIELDS)
def test_probe_defaults(field: str):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(**{
        field: _probe(),
    }))
    probe = getattr(spec, field)
    assert (probe.http_get.port == 80)
    for name in ("path", "scheme"):
        assert (getattr(probe.http_get, name) is None)
    for name in _PROBE_TIMINGS:
        assert (getattr(probe, name) is None)

@pytest.mark.parametrize("field", _PROBE_FIELDS)
@pytest.mark.parametrize(
    ("probe", "kubernetes"),
    [
        (
            _PROBE,
            {"httpGet": {"port": 80}},
        ),
        (
            {
                "http_get":              {"port": 8080, "path": "/healthz/live", "scheme": "HTTPS"},
                "initial_delay_seconds": 15,
                "period_seconds":        10,
                "timeout_seconds":       2,
                "success_threshold":     1,
                "failure_threshold":     3,
            },
            {
                "httpGet":             {"port": 8080, "path": "/healthz/live", "scheme": "HTTPS"},
                "initialDelaySeconds": 15,
                "periodSeconds":       10,
                "timeoutSeconds":      2,
                "successThreshold":    1,
                "failureThreshold":    3,
            },
        ),
        (
            {"http_get": {"port": 80, "path": None, "scheme": None}, **{name: None for name in _PROBE_TIMINGS}},
            {"httpGet": {"port": 80}},
        ),
    ],
    ids = ["minimal", "all-fields", "explicit-nulls"],
)
def test_valid_probes(field: str, probe: Dict[str, Any], kubernetes: Dict[str, Any]):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(**{
        field: probe,
    }))
    # Values come back unchanged.
    assert (getattr(spec, field).model_dump(exclude_unset=True) == probe)
    # What _build_deployment puts on the container: camelCase keys, unset fields left to the API server's defaults.
    assert (getattr(spec, field).model_dump(exclude_none=True, by_alias=True) == kubernetes)

def test_both_probes_together():
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        readiness_probe = _probe(success_threshold=2),
        liveness_probe  = _probe(success_threshold=1),
    ))
    assert (spec.readiness_probe.success_threshold == 2)
    assert (spec.liveness_probe.success_threshold  == 1)

@pytest.mark.parametrize("field", _PROBE_FIELDS)
@pytest.mark.parametrize(
    "probe",
    [{}, {"http_get": None}, {"period_seconds": 10}],
    ids = ["empty", "null", "timings-only"],
)
def test_probe_http_get_is_required(field: str, probe: Dict[str, Any]):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(**{
            field: probe,
        }))
    assert (_error_fields(exc_info) == {f"{field}.http_get"})

# -------------------- probes: http_get --------------------

@pytest.mark.parametrize("field", _PROBE_FIELDS)
@pytest.mark.parametrize("port", [1, 80, 8080, 65535])
def test_valid_probe_ports(field: str, port: int):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(**{
        field: _probe(http_get={"port": port}),
    }))
    assert (getattr(spec, field).http_get.port == port)

@pytest.mark.parametrize("field", _PROBE_FIELDS)
@pytest.mark.parametrize("port", [
    0,
    -1,
    65536,
    80.0,                    # strict: a float is not an integer
    "80",                    # strict: no numeric strings
    "http",                  # named container ports are not supported
    True,                    # strict: a boolean is not an integer
    None,
])
def test_invalid_probe_ports(field: str, port: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(**{
            field: _probe(http_get={"port": port}),
        }))
    assert (_error_fields(exc_info) == {f"{field}.http_get.port"})

@pytest.mark.parametrize("field", _PROBE_FIELDS)
def test_probe_port_is_required(field: str):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(**{
            field: _probe(http_get={"path": "/healthz"}),
        }))
    assert (_error_fields(exc_info) == {f"{field}.http_get.port"})

@pytest.mark.parametrize("field", _PROBE_FIELDS)
@pytest.mark.parametrize("path", ["/", "/healthz/live", "/status?verbose=1", None])
def test_valid_probe_paths(field: str, path: Any):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(**{
        field: _probe(http_get={"port": 80, "path": path}),
    }))
    assert (getattr(spec, field).http_get.path == path)

@pytest.mark.parametrize("field", _PROBE_FIELDS)
@pytest.mark.parametrize("path", [80, True, ["/healthz"]])
def test_invalid_probe_paths(field: str, path: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(**{
            field: _probe(http_get={"port": 80, "path": path}),
        }))
    assert (_error_fields(exc_info) == {f"{field}.http_get.path"})

@pytest.mark.parametrize("field", _PROBE_FIELDS)
@pytest.mark.parametrize("scheme", ["HTTP", "HTTPS", None])
def test_valid_probe_schemes(field: str, scheme: Any):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(**{
        field: _probe(http_get={"port": 80, "scheme": scheme}),
    }))
    assert (getattr(spec, field).http_get.scheme == scheme)

@pytest.mark.parametrize("field", _PROBE_FIELDS)
@pytest.mark.parametrize("scheme", [
    "http",                  # values are case-sensitive
    "https",
    "TCP",                   # a transport protocol, not an HTTP scheme
    "HTTP ",                 # no trimming
    "",
    1,
])
def test_invalid_probe_schemes(field: str, scheme: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(**{
            field: _probe(http_get={"port": 80, "scheme": scheme}),
        }))
    assert (_error_fields(exc_info) == {f"{field}.http_get.scheme"})

@pytest.mark.parametrize("field", _PROBE_FIELDS)
@pytest.mark.parametrize("unknown", [
    "foo",
    "Port",                  # field names are case-sensitive
    "host",                  # not supported
    "http_headers",          # not supported
    "httpHeaders",           # Kubernetes spelling, not ours
])
def test_probe_http_get_unknown_fields(field: str, unknown: str):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(**{
            field: _probe(http_get={"port": 80, unknown: 1}),
        }))
    assert (_error_fields(exc_info) == {f"{field}.http_get.{unknown}"})

# -------------------- probes: timings and thresholds --------------------

@pytest.mark.parametrize("field", _PROBE_FIELDS)
@pytest.mark.parametrize(("timing", "value"), [
    ("initial_delay_seconds", 0),
    ("initial_delay_seconds", 15),
    ("initial_delay_seconds", 2_147_483_647),
    ("period_seconds",        1),
    ("period_seconds",        2_147_483_647),
    ("timeout_seconds",       1),
    ("timeout_seconds",       2_147_483_647),
    ("success_threshold",     1),
    ("failure_threshold",     1),
    ("failure_threshold",     2_147_483_647),
])
def test_valid_probe_timings(field: str, timing: str, value: int):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(**{
        field: _probe(**{timing: value}),
    }))
    assert (getattr(getattr(spec, field), timing) == value)

@pytest.mark.parametrize("field",  _PROBE_FIELDS)
@pytest.mark.parametrize("timing", _PROBE_TIMINGS)
@pytest.mark.parametrize("value",  [
    -1,
    2_147_483_648,           # these are int32 fields
    10.0,                    # strict: a float is not an integer
    "10",                    # strict: no numeric strings
    True,                    # strict: a boolean is not an integer
])
def test_invalid_probe_timings(field: str, timing: str, value: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(**{
            field: _probe(**{timing: value}),
        }))
    assert (_error_fields(exc_info) == {f"{field}.{timing}"})

# initial_delay_seconds may be 0; every other timing must be at least 1 (kubectl explain pod.spec.containers.livenessProbe).
@pytest.mark.parametrize("field",  _PROBE_FIELDS)
@pytest.mark.parametrize("timing", ["period_seconds", "timeout_seconds", "success_threshold", "failure_threshold"])
def test_probe_timings_below_one(field: str, timing: str):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(**{
            field: _probe(**{timing: 0}),
        }))
    assert (_error_fields(exc_info) == {f"{field}.{timing}"})

@pytest.mark.parametrize("value", [1, 2, 2_147_483_647, None])
def test_valid_readiness_success_thresholds(value: Any):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        readiness_probe = _probe(success_threshold=value),
    ))
    assert (spec.readiness_probe.success_threshold == value)

@pytest.mark.parametrize("value", [1, None])
def test_valid_liveness_success_thresholds(value: Any):
    spec: DeploymentSpec = DeploymentSpec.model_validate(_input(
        liveness_probe = _probe(success_threshold=value),
    ))
    assert (spec.liveness_probe.success_threshold == value)

# successThreshold "Must be 1 for liveness and startup" (kubectl explain pod.spec.containers.livenessProbe.successThreshold).
@pytest.mark.parametrize("value", [2, 3, 2_147_483_647])
def test_liveness_success_threshold_must_be_one(value: int):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(
            liveness_probe = _probe(success_threshold=value),
        ))
    assert (_error_fields(exc_info) == {"liveness_probe.success_threshold"})

# -------------------- probes: shape --------------------

@pytest.mark.parametrize("field", _PROBE_FIELDS)
@pytest.mark.parametrize("unknown", [
    "foo",
    "httpGet",                           # Kubernetes spelling, not ours
    "initialDelaySeconds",               # Kubernetes spelling, not ours
    "tcp_socket",                        # only HTTP probes are supported
    "exec",                              # only HTTP probes are supported
    "grpc",                              # only HTTP probes are supported
    "termination_grace_period_seconds",  # not supported
])
def test_probe_unknown_fields(field: str, unknown: str):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(**{
            field: _probe(**{unknown: 1}),
        }))
    assert (_error_fields(exc_info) == {f"{field}.{unknown}"})

@pytest.mark.parametrize("field", _PROBE_FIELDS)
@pytest.mark.parametrize(
    "probe",
    [80, "/healthz", True, [], [_PROBE]],
    ids = ["int", "string", "bool", "empty-list", "list-of-objects"],
)
def test_probe_must_be_an_object(field: str, probe: Any):
    with pytest.raises(ValidationError) as exc_info:
        DeploymentSpec.model_validate(_input(**{
            field: probe,
        }))
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

# ==================== RollbackSpec ====================

@pytest.mark.parametrize(
    "body",
    [{}, {"revision": None}],
    ids = ["empty", "revision-null"],
)
def test_rollback_revision_is_optional(body: Dict[str, Any]):
    # No revision means the previous one (like `kubectl rollout undo`).
    assert (RollbackSpec.model_validate(body).revision is None)

@pytest.mark.parametrize("revision", [
    1,
    2,
    10,
    9_223_372_036_854_775_807,   # no upper limit: the controller's revisions are int64, a missing one is a 400 later
])
def test_valid_rollback_revisions(revision: int):
    assert (RollbackSpec.model_validate({"revision": revision}).revision == revision)

@pytest.mark.parametrize("revision", [
    0,                       # kubectl's --to-revision=0 means "previous"; here that is an omitted revision
    -1,
    2.0,                     # strict: a float is not an integer
    "2",                     # strict: no numeric strings
    True,                    # strict: a boolean is not an integer
    [2],
])
def test_invalid_rollback_revisions(revision: Any):
    with pytest.raises(ValidationError) as exc_info:
        RollbackSpec.model_validate({
            "revision": revision,
        })
    assert (_error_fields(exc_info) == {"revision"})

@pytest.mark.parametrize("field", [
    "to_revision",           # kubectl's flag name, not ours
    "toRevision",
    "Revision",              # field names are case-sensitive
    "uid",                   # in the URL, not the body
    "namespace",             # in the query string, not the body
    "image",
])
def test_rollback_unknown_fields(field: str):
    with pytest.raises(ValidationError) as exc_info:
        RollbackSpec.model_validate({
            "revision": 1,
            field:      "x",
        })
    assert (_error_fields(exc_info) == {field})

# The route turns an empty body into {}, so None here stands for a body that is present but not valid JSON.
@pytest.mark.parametrize("body", [None, [], "revision=2", 2])
def test_rollback_body_must_be_an_object(body: Any):
    with pytest.raises(ValidationError) as exc_info:
        RollbackSpec.model_validate(body)
    assert (_error_fields(exc_info) == {""})
