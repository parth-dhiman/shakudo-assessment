# Standard imports.
import re

# ==================== Helper RegEx ====================

# -------------------- DNS Label --------------------

_NAMECHECK_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")

# -------------------- UID --------------------

# metadata.uid is a UUID (https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#uids).
_UID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# ---------------- Image --------------------

# Image reference grammar (same rules Docker and the kubelet use to parse image names).
_IMAGE_PATH_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_IMAGE_REGISTRY = (
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?)*"
    r"(?::[0-9]+)?"
)
_IMAGE_TAG = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
_IMAGE_DIGEST = r"[A-Za-z][A-Za-z0-9]*(?:[-_+.][A-Za-z][A-Za-z0-9]*)*:[0-9a-fA-F]{32,}"

_IMAGE_RE = re.compile(
    rf"(?:{_IMAGE_REGISTRY}/)?"                                 # optional registry + "/"
    rf"{_IMAGE_PATH_COMPONENT}(?:/{_IMAGE_PATH_COMPONENT})*"    # path: one or more components
    rf"(?::{_IMAGE_TAG})?"                                      # optional ":tag"
    rf"(?:@{_IMAGE_DIGEST})?"                                   # optional "@digest"
)

# ==================== Helper Functions ====================

def validate_dns_label(v: str) -> str:
    if not _NAMECHECK_RE.fullmatch(v):
        raise ValueError(
            "Must be lowercase letters, digits, or '-', "
            "start and end with a letter or digit, max 63 characters."
        )
    return v

def validate_uid(v: str) -> str:
    if not _UID_RE.fullmatch(v):
        raise ValueError(
            "Must be a UUID, like 'e5f1f2f8-2939-43ed-8e53-c8c980b220e2'."
        )
    # The API server emits lowercase UIDs, and lookups compare strings.
    return v.lower()

def validate_image(v: str) -> str:
    if not _IMAGE_RE.fullmatch(v):
        raise ValueError(
            "Must be a valid image reference, "
            "like 'busybox', 'busybox:1.36', or 'ghcr.io/org/app:v1'."
        )
    return v
