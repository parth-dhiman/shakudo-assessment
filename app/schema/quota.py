# Local imports.
from app.schema.common import ForbidExtraFields
from app.schema.deployments import RequestsObject

class QuotaConfig(ForbidExtraFields):

    # quota.yaml: the total CPU and memory requests each namespace the API creates may hold.
    # Same shape and quantity rules as a deployment's resources.requests.
    requests: RequestsObject
