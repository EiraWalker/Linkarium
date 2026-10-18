"""Bound AI response bytes before decoding, including buffered mock responses."""
import gzip
import json
import unittest
from unittest.mock import patch

import httpx

from library.ai_transport import AIError, ChatClient, MAX_RESPONSE_BYTES, request_budget


def envelope(value):
    return json.dumps({'choices': [{'message': {'content': json.dumps(value)}}]}).encode()


class ObservedStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk


class AIResponseLimitTests(unittest.TestCase):
    def complete(self, response, reserve=None, timeout=120):
        requests = []
        def transport(request):
            requests.append(request)
            return response
        client = ChatClient('https://fixture.invalid/v1', 'fixture-key', 'fixture',
                            transport=httpx.MockTransport(transport), total_timeout=timeout)
        with request_budget(reserve):
            result = client.complete([{'role': 'user', 'content': 'fixture'}])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers['Accept-Encoding'], 'identity')
        return result

    def test_identity_json_succeeds_for_streamed_and_buffered_mock_responses(self):
        body = envelope({'ok': True})
        for buffered in (False, True):
            with self.subTest(buffered=buffered):
                reservations = []
                response = (httpx.Response(200, content=body, headers={'Content-Encoding': 'identity'})
                            if buffered else httpx.Response(200, stream=ObservedStream([body[:7], body[7:]])))
                self.assertEqual(self.complete(response, lambda: reservations.append(True)), {'ok': True})
                self.assertEqual(reservations, [True])

    def test_gzip_expansion_is_rejected_before_the_compressed_stream_is_read(self):
        compressed = gzip.compress(b'x' * (8 * 1024 * 1024))
        self.assertLess(len(compressed), 10000)
        stream = ObservedStream([compressed])
        response = httpx.Response(200, headers={'Content-Encoding': 'gzip'}, stream=stream)
        reservations = []
        with self.assertRaises(AIError) as caught:
            self.complete(response, lambda: reservations.append(True))
        self.assertEqual(caught.exception.code, 'AI_INVALID_RESULT')
        self.assertEqual(stream.reads, 0)
        self.assertEqual(reservations, [True])

    def test_empty_compressed_stream_cannot_enter_the_response_read_loop(self):
        class NeverRead(httpx.SyncByteStream):
            def __iter__(self):
                raise AssertionError('Unsupported compressed stream was consumed')
                yield b''
        for encoding in ('gzip', 'deflate', 'br', 'gzip, identity'):
            with self.subTest(encoding=encoding), self.assertRaises(AIError) as caught:
                self.complete(httpx.Response(200, headers={'Content-Encoding': encoding}, stream=NeverRead()))
            self.assertEqual(caught.exception.code, 'AI_INVALID_RESULT')

    def test_raw_byte_budget_applies_before_accumulating_buffered_or_streamed_data(self):
        content = b'x' * (MAX_RESPONSE_BYTES + 1)
        for buffered in (False, True):
            response = httpx.Response(200, content=content) if buffered else httpx.Response(200, stream=ObservedStream([content]))
            with self.subTest(buffered=buffered), self.assertRaises(AIError) as caught:
                self.complete(response)
            self.assertEqual(caught.exception.code, 'AI_RESPONSE_TOO_LARGE')

    def test_total_deadline_is_checked_at_each_small_raw_chunk(self):
        clock = [0.0]
        class SlowStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'{'  # A read smaller than any buffered chunk size.
                clock[0] = 2.0
                yield b' '
                raise AssertionError('Reading continued past the operation deadline')
        with patch('library.ai_transport.time.monotonic', side_effect=lambda: clock[0]), self.assertRaises(AIError) as caught:
            self.complete(httpx.Response(200, stream=SlowStream()), timeout=1)
        self.assertEqual(caught.exception.code, 'AI_TIMEOUT')
