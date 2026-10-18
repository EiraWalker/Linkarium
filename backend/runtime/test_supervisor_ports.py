"""Exercise restart probes against real socket lifecycle states."""
import os
import socket
import unittest
from unittest.mock import patch

from supervisor import Supervisor


class SupervisorPortTests(unittest.TestCase):
    def start_with_fake_children(self, port):
        owner = Supervisor("127.0.0.1", port)
        with patch("runtime.network_access.web_addresses", return_value=["127.0.0.1"]), \
                patch("supervisor.subprocess.Popen") as spawn:
            owner.start_services()
            self.assertEqual(spawn.call_count, 2)

    @unittest.skipIf(os.name == "nt", "Linux TIME_WAIT bind semantics")
    def test_restart_after_server_actively_closes_a_connection(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.listen()
            with socket.create_connection(("127.0.0.1", port)) as client:
                connection, _ = listener.accept()
                connection.close()  # Server closes first, leaving its port in TIME_WAIT.
                self.assertEqual(client.recv(1), b"")
        self.start_with_fake_children(port)

    def test_another_listener_is_rejected_before_spawning_children(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            owner = Supervisor("127.0.0.1", listener.getsockname()[1])
            with patch("runtime.network_access.web_addresses", return_value=["127.0.0.1"]), \
                    patch("supervisor.subprocess.Popen") as spawn:
                with self.assertRaisesRegex(RuntimeError, "already in use"):
                    owner.start_services()
                spawn.assert_not_called()
