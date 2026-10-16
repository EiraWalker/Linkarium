"""A normal connection burst waits for capacity without poisoning the capture."""
import threading
import time
import unittest
from types import SimpleNamespace

from capture.doh_proxy import ProxyServer


class RequestSocket:
    def __init__(self):
        self.closed = False

    def shutdown(self, _how):
        pass

    def close(self):
        self.closed = True


class ProxyBackpressureTests(unittest.TestCase):
    def server(self, timeout=5):
        owner = SimpleNamespace(stop=threading.Event(), deadline=time.monotonic() + timeout, error=None)
        return ProxyServer(owner)

    def test_thirty_third_connection_waits_without_exceeding_active_limit(self):
        server = self.server()
        release = threading.Event()
        entered = threading.Condition()
        counts = {'active': 0, 'peak': 0, 'total': 0}

        def handle(_request, _address):
            with entered:
                counts['active'] += 1
                counts['total'] += 1
                counts['peak'] = max(counts['peak'], counts['active'])
                entered.notify_all()
            release.wait(4)
            with entered:
                counts['active'] -= 1

        server.finish_request = handle
        pending = RequestSocket()
        waiter = threading.Thread(target=server.process_request, args=(pending, ('127.0.0.1', 1)))
        try:
            for i in range(32):
                server.process_request(RequestSocket(), ('127.0.0.1', i))
            with entered:
                self.assertTrue(entered.wait_for(lambda: counts['active'] == 32, timeout=2))
            waiter.start()
            waiter.join(0.15)
            self.assertIsNone(server.owner.error)
            self.assertTrue(waiter.is_alive())
            self.assertFalse(pending.closed)
            release.set()
            waiter.join(2)
            with entered:
                self.assertTrue(entered.wait_for(lambda: counts['total'] == 33, timeout=2))
            self.assertEqual(counts['peak'], 32)
            self.assertEqual(server.peak_active_handlers, 32)
            self.assertEqual(server.slot_wait_count, 1)
            self.assertGreaterEqual(server.slot_wait_seconds, 0.1)
        finally:
            release.set()
            if waiter.ident:
                waiter.join(2)
            with server.thread_lock:
                handlers = list(server.threads)
            for handler in handlers:
                handler.join(2)
            server.server_close()

    def test_waiting_connection_keeps_original_deadline(self):
        server = self.server(timeout=0.1)
        request = RequestSocket()
        for _ in range(32):
            server.handler_slots.acquire()
        try:
            start = time.monotonic()
            server.process_request(request, ('127.0.0.1', 1))
            self.assertEqual(server.owner.error.code, 'timeout')
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertTrue(request.closed)
            self.assertFalse(server.threads)
        finally:
            server.server_close()

    def test_stopping_releases_waiter_without_creating_handler(self):
        server = self.server()
        request = RequestSocket()
        for _ in range(32):
            server.handler_slots.acquire()
        waiter = threading.Thread(target=server.process_request, args=(request, ('127.0.0.1', 1)))
        try:
            waiter.start()
            time.sleep(0.1)
            server.owner.stop.set()
            waiter.join(0.5)
            self.assertFalse(waiter.is_alive())
            self.assertTrue(request.closed)
            self.assertFalse(server.threads)
            self.assertIsNone(server.owner.error)
        finally:
            server.owner.stop.set()
            waiter.join(2)
            server.server_close()
