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
import pytest
from flask.testing import FlaskClient

# Local imports.
from app.api.deployments import (
    _sse_event,
    _sse_log_stream,
)
from app.k8s.services.deployments import _log_lines

# ==================== Log lines ====================

# A log as the API streams it: CRLF and LF endings, a multi-byte character, an empty line, no final newline.
_LOG: bytes = b"line 1\nline 2\r\ncaf\xc3\xa9\n\nlast"
_LINES: List[str] = ["line 1", "line 2", "café", "", "last"]

@pytest.mark.parametrize("size", range(1, len(_LOG) + 1))
def test_log_lines_do_not_depend_on_chunk_boundaries(size: int):
    # Every chunk size, so chunks end mid-line, mid-CRLF and mid-character.
    chunks: List[bytes] = [_LOG[i:i + size] for i in range(0, len(_LOG), size)]
    assert (list(_log_lines(chunks)) == _LINES)

@pytest.mark.parametrize(
    ("chunks", "lines"),
    [
        ([],                       []),
        ([b""],                    []),
        ([b"one\n"],               ["one"]),
        ([b"\n"],                  [""]),
        ([b"bad \xff byte\n"],     ["bad � byte"]),    # undecodable bytes become U+FFFD, the line survives
    ],
    ids = ["no-chunks", "empty-chunk", "trailing-newline", "only-a-newline", "invalid-utf8"],
)
def test_log_lines_edge_cases(chunks: List[bytes], lines: List[str]):
    assert (list(_log_lines(chunks)) == lines)

# ==================== Server-Sent Events ====================

@pytest.mark.parametrize(
    ("data", "name", "expected"),
    [
        ("hello",    None,          "data: hello\n\n"),
        ("",         None,          "data: \n\n"),                                # an empty log line is still an event
        ("why",      "unavailable", "event: unavailable\ndata: why\n\n"),
        ("a\nb",     None,          "data: a\ndata: b\n\n"),                      # one data field per line
        ("a\r\nb\r", None,          "data: a\ndata: b\n\n"),                      # a bare CR would end the field early
    ],
    ids = ["line", "empty-line", "named-event", "multi-line", "carriage-returns"],
)
def test_sse_event(data: str, name: Optional[str], expected: str):
    assert (_sse_event(data, name) == expected)

def test_sse_log_stream():
    def events() -> Generator[Tuple[str, str], None, None]:
        yield ("line", "line 1")
        yield ("unavailable", "container is waiting to start")
    assert (list(_sse_log_stream(events())) == [
        "retry: 5000\n\n",
        "data: line 1\n\n",
        "event: unavailable\ndata: container is waiting to start\n\n",
        "event: end\ndata: \n\n",
    ])

def test_sse_log_stream_closes_the_log_when_the_browser_leaves():
    closed: List[bool] = []
    def events() -> Generator[Tuple[str, str], None, None]:
        try:
            while True:
                yield ("line", "tick")
        finally:
            closed.append(True)  # where the Kubernetes log connection is closed
    stream = _sse_log_stream(events())
    assert (next(stream) == "retry: 5000\n\n")
    assert (next(stream) == "data: tick\n\n")
    stream.close()  # what the server does when the client disconnects
    assert (closed == [True])

# ==================== Route ====================

_UID: str = "e5f1f2f8-2939-43ed-8e53-c8c980b220e2"

# Each request is rejected before any Kubernetes call, so no cluster is needed.
@pytest.mark.parametrize(
    ("path", "query", "code"),
    [
        (f"/api/deployments/{_UID}/logs",    {},                                      "BAD_REQUEST"),
        (f"/api/deployments/{_UID}/logs",    {"pod": ""},                             "BAD_REQUEST"),
        ("/api/deployments/not-a-uid/logs",  {"pod": "web-1"},                        "INVALID_PAYLOAD"),
        (f"/api/deployments/{_UID}/logs",    {"pod": "web-1", "namespace": "Bad_NS"}, "INVALID_PAYLOAD"),
    ],
    ids = ["pod-missing", "pod-empty", "bad-uid", "bad-namespace"],
)
def test_invalid_log_requests(
    client: FlaskClient,
    path:   str,
    query:  Dict[str, Any],
    code:   str,
):
    response = client.get(path, query_string=query)
    assert (response.status_code == 400)
    assert (response.get_json()["error"]["code"] == code)
