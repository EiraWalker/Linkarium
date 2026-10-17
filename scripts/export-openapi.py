"""Export the documented v1 API without introducing a schema framework."""
import json
from pathlib import Path

routes = {
    "/session": ["get"], "/session/login": ["post"], "/session/logout": ["post"],
    "/session/passkey/register/options": ["post"], "/session/passkey/register/verify": ["post"],
    "/session/passkey/login/options": ["post"], "/session/passkey/login/verify": ["post"],
    "/session/key/options": ["post"], "/session/key/verify": ["post"],
    "/session/passkey/grants": ["post"], "/session/passkeys": ["get"],
    "/session/passkeys/{id}": ["patch", "delete"],
    "/settings": ["get", "patch"], "/capture-network": ["get", "patch"], "/runtime/status": ["get"],
    "/bookmarks": ["get", "post"], "/bookmarks/{id}": ["get", "patch"],
    "/bookmarks/delete-preview": ["post"], "/bookmarks/bulk": ["post"],
    "/bookmarks/{id}/refresh": ["post"], "/bookmarks/{id}/snapshots": ["get"],
    "/folders": ["get", "post"], "/folders/{id}": ["patch", "delete"],
    "/folders/all/refresh-preview": ["post"], "/folders/all/refresh": ["post"],
    "/folders/{id}/delete-preview": ["post"], "/folders/{id}/refresh-preview": ["post"], "/folders/{id}/refresh": ["post"],
    "/tags": ["get", "post"], "/tags/{id}": ["patch", "delete"],
    "/job-groups": ["get"], "/job-groups/{id}": ["get"], "/jobs": ["get"], "/jobs/{id}": ["get"], "/jobs/{id}/cancel": ["post"], "/jobs/{id}/retry": ["post"],
    "/snapshots/{id}/preview": ["get"], "/snapshots/delete-preview": ["post"], "/snapshots/bulk-delete": ["post"],
    "/ai-profiles": ["get", "post"], "/ai-profiles/{id}": ["get", "patch"], "/ai-profiles/{id}/test": ["post"],
    "/classification/batches": ["post"], "/classification/proposals": ["get"], "/classification/proposals/{id}": ["patch"], "/classification/accept": ["post"], "/classification/reject": ["post"],
    "/imports": ["get", "post"], "/imports/preview": ["post"], "/imports/{id}": ["get"], "/imports/{id}/execute": ["post"], "/imports/{id}/review-notified": ["post"],
    "/backups": ["post"], "/backups/{id}": ["get"], "/backups/{id}/download": ["get"],
    "/restores/validate": ["post"], "/restores/{id}": ["get"], "/restores/{id}/execute": ["post"],
    "/restores/{id}/pre-backup/download": ["get"],
    "/maintenance-operations/{id}": ["get"], "/maintenance-operations/lookup": ["post"],
    "/data/clear": ["post"], "/data/reset": ["post"],
}
fields = {
    "url": {"type": "string", "format": "uri"}, "title": {"type": "string", "maxLength": 500}, "notes": {"type": "string", "maxLength": 10000},
    "folder_id": {"type": ["string", "null"], "format": "uuid"}, "tag_ids": {"type": "array", "items": {"type": "string", "format": "uuid"}},
    "revision": {"type": "integer", "minimum": 1}, "is_starred": {"type": "boolean"}, "snapshot_enabled": {"type": "boolean"}, "auto_refresh_paused": {"type": "boolean"},
    "refresh_interval_seconds": {"type": ["integer", "null"], "minimum": 3600, "maximum": 31536000},
}
schemas = {
    "Envelope": {"type": "object", "required": ["data"], "properties": {"data": {}, "page": {"type": "object", "properties": {"number": {"type": "integer"}, "size": {"type": "integer"}, "total": {"type": "integer"}}}}},
    "Error": {"type": "object", "required": ["error"], "properties": {"error": {"type": "object", "required": ["code", "message", "request_id"], "properties": {"code": {"type": "string"}, "message": {"type": "string"}, "request_id": {"type": "string"}, "field_errors": {"type": "object"}, "details": {}}}}},
    "BookmarkWrite": {"type": "object", "properties": fields},
    "PasswordLogin": {"type": "object", "required": ["password"], "properties": {"password": {"type": "string", "maxLength": 1024, "writeOnly": True}}},
    "RevisionItems": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "object", "required": ["id", "revision"], "properties": {"id": {"type": "string", "format": "uuid"}, "revision": fields["revision"]}}},
    "BulkBookmarks": {"type": "object", "required": ["action", "items"], "properties": {"action": {"enum": ["move", "add_tags", "remove_tags", "delete"]}, "items": {"$ref": "#/components/schemas/RevisionItems"}, "folder_id": fields["folder_id"], "tag_ids": fields["tag_ids"], "preview_token": {"type": "string"}}},
    "FolderWrite": {"type": "object", "properties": {"name": {"type": "string", "maxLength": 100}, "parent_id": fields["folder_id"], "position": {"type": "integer"}, "revision": fields["revision"], "preview_token": {"type": "string"}}},
    "AIProfileWrite": {"type": "object", "properties": {"name": {"type": "string"}, "base_url": {"type": "string", "format": "uri"}, "model": {"type": "string"}, "protocol": {"const": "chat-completions"}, "output_mode": {"enum": ["json_object", "json_schema", "text"]}, "enabled": {"type": "boolean"}, "api_key": {"type": "string", "writeOnly": True}, "revision": fields["revision"]}},
    "RestoreConfirmation": {"type": "object", "required": ["validation_token", "confirm_overwrite"], "properties": {"validation_token": {"type": "string"}, "confirm_overwrite": {"const": True}}},
    "ClearDataConfirmation": {"type": "object", "required": ["confirmation", "library_epoch"], "properties": {"confirmation": {"const": "清空仓库"}, "library_epoch": {"type": "integer", "minimum": 1}}},
    "ResetDataConfirmation": {"type": "object", "required": ["confirmation", "library_epoch"], "properties": {"confirmation": {"const": "重置数据"}, "library_epoch": {"type": "integer", "minimum": 1}}},
}
paths = {}
schemas["PasskeyName"] = {"type": "object", "required": ["name"], "properties": {
    "name": {"type": "string", "minLength": 1, "maxLength": 100}}}
schemas["PasskeyRegistrationOptions"] = {"description": "Choose exactly one authority: local credential session, terminal initialization code, or enrollment token plus independent code. Explicit authority never falls back to the existing session.", "oneOf": [
    {"allOf": [{"$ref": "#/components/schemas/PasskeyName"}, {"not": {"anyOf": [{"required": [key]} for key in ("bootstrap_code", "first_passkey_code", "enrollment_token", "authorization_code", "setup_token")]}}]},
    {"type": "object", "required": ["name", "bootstrap_code"], "additionalProperties": False, "properties": {
        "name": schemas["PasskeyName"]["properties"]["name"], "bootstrap_code": {"type": "string", "pattern": "^[0-9]{8}$", "writeOnly": True}}},
    {"type": "object", "required": ["name", "first_passkey_code"], "additionalProperties": False, "properties": {
        "name": schemas["PasskeyName"]["properties"]["name"], "first_passkey_code": {"type": "string", "pattern": "^[0-9]{8}$", "writeOnly": True}}},
    {"type": "object", "required": ["name", "enrollment_token", "authorization_code"], "additionalProperties": False, "properties": {
        "name": schemas["PasskeyName"]["properties"]["name"], "enrollment_token": {"type": "string", "writeOnly": True},
        "authorization_code": {"type": "string", "pattern": "^[0-9]{8}$", "writeOnly": True}}},
]}
schemas["PasskeyVerification"] = {"type": "object", "required": ["challenge_id", "credential"], "properties": {
    "challenge_id": {"type": "string", "format": "uuid"}, "credential": {"type": "object", "description": "WebAuthn JSON credential returned by the browser"}}}
schemas["FileKeyVerification"] = {"type": "object", "required": ["challenge_id", "signature"], "properties": {
    "challenge_id": {"type": "string", "format": "uuid"}, "signature": {"type": "string", "description": "Base64url Ed25519 signature; never the private key file"}}}
schemas["EnrollmentGrant"] = {"type": "object", "required": ["enrollment_url", "authorization_code", "expires_at"], "properties": {
    "enrollment_url": {"type": "string", "format": "uri"}, "authorization_code": {"type": "string", "description": "Shown once; never embedded in the URL or QR"},
    "expires_at": {"type": "string", "format": "date-time", "description": "Absolute deadline, five minutes from issuance"}}}
schemas["BookmarkRevisionItems"] = {**schemas["RevisionItems"], "maxItems": 1000}
schemas["ClassificationReview"] = {"type": "object", "required": ["items"], "properties": {"items": {"$ref": "#/components/schemas/BookmarkRevisionItems"}, "tree_revision": {"type": "integer", "minimum": 1}}}
schemas["BulkBookmarks"]["properties"]["items"] = {"$ref": "#/components/schemas/BookmarkRevisionItems"}
schemas["CaptureNetworkWrite"] = {"type": "object", "required": ["mode"], "properties": {
    "mode": {"enum": ["direct", "clash", "manual"]},
    "proxy_url": {"type": "string", "description": "Manual mode requires a loopback HTTP proxy without credentials"},
}}
for route, methods in routes.items():
    item = {}
    for method in methods:
        entry = {"operationId": method + route.replace("/", "_").replace("{id}", "id"), "tags": [route.split("/")[1]], "security": [{"ownerSession": []}], "responses": {"200": {"description": "Success; see docs/api-contract.md for entity fields", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Envelope"}}}}}}
        parameters = []
        if "{id}" in route:
            parameters.append({"name": "id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}})
        if method != "get":
            parameters.append({"name": "X-CSRFToken", "in": "header", "required": True, "schema": {"type": "string"}, "description": "Masked csrf_token returned by GET /session; rotate after login"})
            runtime = route in ("/backups", "/restores/validate", "/data/clear", "/data/reset") or route.endswith("/execute") and route.startswith("/restores")
            parameters.append({"name": "Idempotency-Key", "in": "header", "required": runtime, "schema": {"type": "string", "maxLength": 128}})
            name = "PasswordLogin" if route == "/session/login" else "BulkBookmarks" if route == "/bookmarks/bulk" else "BookmarkWrite" if route in ("/bookmarks", "/bookmarks/{id}") else "FolderWrite" if route in ("/folders", "/folders/{id}") else "AIProfileWrite" if route.startswith("/ai-profiles") and not route.endswith("/test") else "RestoreConfirmation" if route.startswith("/restores/") and route.endswith("/execute") else None
            if route in ("/classification/accept", "/classification/reject"):
                name = "ClassificationReview"
            if route == "/capture-network":
                name = "CaptureNetworkWrite"
            if route in ("/data/clear", "/data/reset"):
                name = "ClearDataConfirmation" if route == "/data/clear" else "ResetDataConfirmation"
            if route == "/session/passkey/register/options":
                name = "PasskeyRegistrationOptions"
            if route in ("/session/passkey/register/verify", "/session/passkey/login/verify"):
                name = "PasskeyVerification"
            if route == "/session/key/verify":
                name = "FileKeyVerification"
            if route == "/session/passkeys/{id}" and method == "patch":
                name = "PasskeyName"
            content = {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"} if name else {"type": "object"}}}
            if route in ("/imports", "/imports/preview", "/restores/validate"):
                content = {"multipart/form-data": {"schema": {"type": "object", "required": ["file"], "properties": {"file": {"type": "string", "format": "binary"}}}}}
            entry["requestBody"] = {"required": True, "content": content}
            entry["responses"]["202"] = {"description": "Accepted; business job/group ID or runtime operation ID"}
            entry["responses"]["201"] = {"description": "Created"}
        if route in ("/session", "/session/login"):
            entry["security"] = []
        if route.startswith(("/session/passkey/register/", "/session/passkey/login/", "/session/key/")):
            entry["security"] = []
        if route.startswith("/session/"):
            parameters = [parameter for parameter in parameters if parameter["name"] != "Idempotency-Key"]
            entry["responses"].pop("202", None)
            entry["responses"].pop("201", None)
        if route == "/session/passkey/grants":
            entry["requestBody"]["required"] = False
            entry["responses"].pop("200", None)
            entry["responses"]["201"] = {"description": "Independent link and code, valid five minutes; requires a valid credential session", "content": {
                "application/json": {"schema": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/EnrollmentGrant"}}}}}}
        if route == "/session/passkeys/{id}" and method == "delete":
            entry.pop("requestBody", None)
            entry["description"] = "Atomically preserve a usable login method. Revoke all sessions tied to this credential; return deleted and logged_out."
        if route == "/session/passkey/register/verify":
            entry["description"] = "Successful registration returns registered/authenticated/credential_id/already_completed/registration_mode. The mode is the server-authorized local/bootstrap/first_passkey/add_device/reset purpose. Same-payload completion replay never creates a new login session."
        if route == "/session/login":
            entry["description"] = "Debug password only when BOOKMARK_AUTH_DEBUG is enabled; normal mode always rejects."
        if route.endswith("/preview") and route.startswith("/snapshots/"):
            entry["responses"]["200"] = {"description": "Private sandboxed immutable HTML", "content": {"text/html": {"schema": {"type": "string"}}}}
        if route.endswith("/download"):
            entry["responses"]["200"] = {"description": "Private ZIP64 library package", "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}
        if method == "get" and route == "/bookmarks":
            parameters.append({"name": "selection", "in": "query", "schema": {"type": "boolean"}, "description": "When true, return all matching {id,revision} items without pagination or previews; other filters still apply."})
            for name in ("q", "fields", "scope", "folder_id", "include_descendants", "tag_ids", "starred", "unclassified", "variant", "sort", "order", "page", "page_size"):
                parameters.append({"name": name, "in": "query", "schema": {"type": "string"}})
        if method == "get" and route == "/job-groups":
            parameters.extend([
                {"name": "page", "in": "query", "schema": {"type": "integer", "minimum": 1}},
                {"name": "page_size", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 48}},
            ])
            entry["responses"]["200"]["description"] = "Durable task catalog: data.groups, active_count and pending_proposals with envelope.page; running and active groups first, then newest. Summaries omit job payloads."
        if method == "get" and route == "/classification/proposals":
            parameters.append({"name": "selection", "in": "query", "schema": {"type": "boolean"}, "description": "Return all matching {id,revision} entries across pages, at most 1000; preserve status and scope filters."})
            for name in ("status", "import_id", "bookmark_id", "group_id", "page", "page_size"):
                parameters.append({"name": name, "in": "query", "schema": {"type": "string"}})
        for code in ((400, 401, 403, 404, 409, 410, 413, 429, 503) if route.startswith("/session") else (400, 401, 403, 404, 409, 413, 429, 503)):
            entry["responses"][str(code)] = {"description": "Structured error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
        entry["parameters"] = parameters
        item[method] = entry
    paths[route] = item
spec = {"openapi": "3.1.0", "info": {"title": "Bookmark Library API", "version": "0.1.0", "description": "Single owner; read docs/api-contract.md for transaction, revision and maintenance semantics."}, "servers": [{"url": "/api/v1"}], "paths": paths, "components": {"securitySchemes": {"ownerSession": {"type": "apiKey", "in": "cookie", "name": "sessionid"}}, "schemas": schemas}}
output = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
output.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Exported {len(paths)} API paths to {output}")
