"""HTTP actor used by integration tests; model output is the external seam."""
import json
import uuid

from django.contrib.auth import get_user_model
from runtime.models import ExecutionDevice
from runtime.testing import force_credential_login
from .errors import ApiError
from .local_ai import current_device


def bind_device(case):
    user, _ = get_user_model().objects.get_or_create(username="owner")
    force_credential_login(case.client, user)
    identity, proof = str(uuid.uuid4()), uuid.uuid4().hex * 2
    response = case.client.post("/api/v1/local-ai/devices", json.dumps({
        "id": identity, "proof": proof, "name": "Integration fixture"}), content_type="application/json")
    case.assertEqual(response.status_code, 201, response.content)
    case.client.defaults.update(HTTP_X_LINKARIUM_DEVICE=identity, HTTP_X_LINKARIUM_DEVICE_PROOF=proof)
    token = current_device.set(ExecutionDevice.objects.get(pk=identity))
    case.addCleanup(current_device.reset, token)
    return identity


def post(case, route, body):
    return case.client.post("/api/v1/local-ai/" + route, json.dumps(body), content_type="application/json")


def run_device(case, complete, *, limit=100):
    for _ in range(limit):
        response = post(case, "claim", {})
        case.assertEqual(response.status_code, 200, response.content)
        claim = response.json()["data"]
        if not claim or claim.get("waiting"):
            return
        if claim.get("invalidated"):
            raise ApiError(claim["error_code"], "Invalidated fixture input", 409)
        raw = complete(claim["messages"], schema=claim["schema"])
        response = post(case, "result", {**claim, "response": json.dumps(raw, ensure_ascii=False)})
        if response.status_code != 200:
            error = response.json()["error"]
            raise ApiError(error["code"], error["message"], response.status_code)
