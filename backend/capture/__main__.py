import argparse
import json
import sys
from contextlib import nullcontext
from pathlib import Path

from .models import CaptureError, CaptureRequest
from .runner import capture
from .diagnostics import CaptureTrace, development_trace_path


def main():
    parser = argparse.ArgumentParser(description='Produce a bounded dynamic Monolith HTML archive')
    parser.add_argument('--request', required=True, help='JSON request file (never a shell command)')
    parser.add_argument('--diagnostics', help='Write a redacted stage trace outside temporary capture output')
    arguments = parser.parse_args()
    try:
        body = Path(arguments.request).read_bytes()
        if len(body) > 16384:
            raise CaptureError('invalid_request', 'Request file exceeds size budget')
        request = CaptureRequest(**json.loads(body))
        request.validate()
        trace_path = arguments.diagnostics or development_trace_path(request)
        with CaptureTrace(trace_path, request) if trace_path else nullcontext():
            result = capture(request)
        print(json.dumps({'ok': True, 'result': result.to_dict()}, ensure_ascii=False))
    except CaptureError as exc:
        print(json.dumps({'ok': False, 'error': {'code': exc.code, 'message': str(exc)}}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
