import json
import hashlib
import os
from pathlib import Path
import socket

from .models import CaptureError, CaptureRequest, CaptureResult
from .preview import validate_preview


class LauncherClient:
    """Fixed-operation local client; input/output roots are chosen by the launcher."""
    def __init__(self, socket_path='/run/bookmark-capture/launcher.sock', spool_path=None):
        self.socket_path = socket_path
        self.spool_path = Path(spool_path or os.environ.get('BOOKMARK_CAPTURE_SPOOL', '/capture-spool'))

    def _call(self, payload):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(135)
            channel.connect(self.socket_path)
            channel.sendall(json.dumps(payload).encode('utf-8') + b'\n')
            data = b''
            while b'\n' not in data:
                chunk = channel.recv(4096)
                if not chunk:
                    raise CaptureError('launcher_protocol', 'Invalid capture launcher response')
                data += chunk
                if len(data) > 131072:
                    raise CaptureError('launcher_protocol', 'Launcher response exceeds size budget')
        response = json.loads(data.split(b'\n')[0])
        if not response.get('ok'):
            error = response.get('error', {})
            raise CaptureError(error.get('code', 'launcher_failed'), error.get('message', 'Capture launcher failed'))
        return response

    def capture(self, request: CaptureRequest) -> CaptureResult:
        request.validate()
        folder = self.spool_path / request.attempt_uuid
        folder.mkdir(mode=0o750, parents=True, exist_ok=False)
        allowed = ('attempt_uuid', 'generation', 'library_epoch', 'url', 'variant', 'mode')
        payload = {name: getattr(request, name) for name in allowed}
        (folder / 'request.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        response = self._call({'action': 'start', **{name: payload[name] for name in ('attempt_uuid', 'generation', 'library_epoch')}})
        result = CaptureResult(**response['result'])
        if result.artifact_path:
            artifact = folder / 'output' / Path(result.artifact_path).name
            if artifact.is_symlink() or not artifact.is_file():
                raise CaptureError('launcher_protocol', 'Launcher artifact is not a regular file')
            if not isinstance(result.sha256, str) or len(result.sha256) != 64 or any(value not in '0123456789abcdef' for value in result.sha256):
                raise CaptureError('launcher_protocol', 'Launcher artifact hash is invalid')
            if artifact.name != result.sha256 + '.html' or not 0 < artifact.stat().st_size <= request.max_bytes:
                raise CaptureError('launcher_protocol', 'Launcher artifact name or size is invalid')
            if artifact.stat().st_size != result.byte_size or hashlib.sha256(artifact.read_bytes()).hexdigest() != result.sha256:
                raise CaptureError('launcher_protocol', 'Launcher artifact integrity check failed')
            result.artifact_path = str(artifact)
        if result.preview_path:
            if not result.artifact_path or request.mode != 'capture':
                raise CaptureError('launcher_protocol', 'Preview has no matching archive')
            width, height = (1366, 768) if request.variant == 'desktop' else (390, 844)
            if (result.viewport_width, result.viewport_height) != (width, height):
                raise CaptureError('launcher_protocol', 'Launcher preview viewport is invalid')
            preview = folder / 'output' / Path(result.preview_path).name
            if (not isinstance(result.preview_sha256, str) or len(result.preview_sha256) != 64
                    or any(value not in '0123456789abcdef' for value in result.preview_sha256)
                    or preview.name != result.preview_sha256 + '.jpg'
                    or type(result.preview_byte_size) is not int or result.preview_byte_size <= 0):
                raise CaptureError('launcher_protocol', 'Launcher preview name or size is invalid')
            try:
                validate_preview(preview, result.preview_sha256, result.preview_byte_size, width, height)
            except (CaptureError, OSError) as exc:
                raise CaptureError('launcher_protocol', 'Launcher preview integrity check failed') from exc
            result.preview_path = str(preview)
        elif result.preview_sha256 is not None or result.preview_byte_size:
            raise CaptureError('launcher_protocol', 'Launcher preview fields are incomplete')
        return result

    def cancel(self, attempt_uuid, generation, library_epoch):
        return self._call({'action': 'cancel', 'attempt_uuid': attempt_uuid, 'generation': generation, 'library_epoch': library_epoch})

    def status(self, attempt_uuid, generation, library_epoch):
        return self._call({'action': 'status', 'attempt_uuid': attempt_uuid, 'generation': generation, 'library_epoch': library_epoch})
