"""Final logical bytes are withheld until integrity is known (stdlib only)."""
import gzip
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from library import artifact_storage as storage


class ArtifactStreamingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def fixture(self, data, encoding, corruption="none"):
        digest = hashlib.sha256(data).hexdigest()
        canonical = self.root / encoding / corruption / (digest + ".html")
        canonical.parent.mkdir(parents=True, exist_ok=True)
        logical = (b"b" * len(data) if corruption == "wrong-sha" else
                   data + b"!" if corruption == "overlimit" else
                   data[:-1] if corruption == "short" else data)
        payload = logical if encoding == "raw" else gzip.compress(logical, mtime=0)
        if encoding == "members":
            middle = len(logical) // 2
            payload = gzip.compress(logical[:middle], mtime=0) + gzip.compress(logical[middle:], mtime=0)
        if corruption == "truncated-footer":
            payload = payload[:-5]
        elif corruption == "crc":
            payload = bytearray(payload)
            payload[-8] ^= 1
        physical = canonical if encoding == "raw" else Path(str(canonical) + ".gz")
        physical.write_bytes(payload)
        return canonical

    def test_corruption_never_releases_the_complete_declared_body(self):
        for length in (1, 65535, 65536, 65537, 131072):
            data = b"a" * length
            for encoding in ("raw", "gzip"):
                corruptions = ("wrong-sha", "overlimit", "short")
                if encoding == "gzip":
                    corruptions += ("truncated-footer", "crc")
                for corruption in corruptions:
                    canonical = self.fixture(data, encoding, corruption)
                    for expected in (None, length):
                        with self.subTest(length=length, encoding=encoding,
                                          corruption=corruption, expected=expected):
                            # None must also enforce the global logical-byte budget.
                            with patch.object(storage, "MAX_CAPTURE_BYTES", length):
                                reader = storage.open_artifact(canonical, expected_size=expected)
                                delivered = 0
                                try:
                                    with self.assertRaises(storage.ArtifactStorageError):
                                        for chunk in reader:
                                            delivered += len(chunk)
                                            self.assertLess(delivered, length,
                                                "Complete HTTP Content-Length escaped before validation")
                                    self.assertTrue(reader.closed)
                                    self.assertTrue(reader._file.closed)
                                    self.assertTrue(reader._stream.closed)
                                finally:
                                    reader.close()

    def test_valid_read_sizes_and_readinto_preserve_every_byte(self):
        for length in (0, 1, 65535, 65536, 65537, 131072):
            data = (bytes(range(256)) * (length // 256 + 1))[:length]
            for encoding in ("raw", "gzip", "members"):
                canonical = self.fixture(data, encoding)
                for expected in (None, length):
                    for read_size in (1, 7, 65536, 65537):
                        with self.subTest(length=length, encoding=encoding,
                                          expected=expected, read_size=read_size):
                            with storage.open_artifact(canonical, expected_size=expected) as reader:
                                received = bytearray()
                                while len(received) < length:
                                    self.assertEqual(reader.read(0), b"")
                                    count = min(read_size, length - len(received))
                                    if len(received) % 2:
                                        buffer = bytearray(count)
                                        self.assertEqual(reader.readinto(buffer), count)
                                        chunk = bytes(buffer)
                                    else:
                                        chunk = reader.read(count)
                                    self.assertEqual(len(chunk), count)
                                    received.extend(chunk)
                                self.assertEqual(received, data)
                                if length:
                                    self.assertTrue(reader._verified, "Final nonempty read must verify EOF")
                                self.assertEqual(reader.read(1), b"")
                                self.assertTrue(reader._verified)
                                self.assertEqual(reader.read(), b"")
                                self.assertEqual(reader.readinto(bytearray(1)), 0)

    def test_small_reads_cannot_bypass_integrity_or_size_checks(self):
        for length in (1, 65535, 65536, 65537, 131072):
            for encoding in ("raw", "gzip"):
                corruptions = ("wrong-sha", "short", "overlimit")
                if encoding == "gzip":
                    corruptions += ("truncated-footer", "crc")
                for corruption in corruptions:
                    canonical = self.fixture(b"a" * length, encoding, corruption)
                    for expected in (None, 0, length):
                        with self.subTest(length=length, encoding=encoding,
                                          corruption=corruption, expected=expected):
                            with patch.object(storage, "MAX_CAPTURE_BYTES", length):
                                reader = storage.open_artifact(canonical, expected_size=expected)
                                delivered = 0
                                try:
                                    with self.assertRaises(storage.ArtifactStorageError):
                                        while chunk := reader.read(7):
                                            delivered += len(chunk)
                                            self.assertLess(delivered, length)
                                    self.assertTrue(reader.closed)
                                finally:
                                    reader.close()

    def test_read_all_includes_pending_byte_and_verifies(self):
        for encoding in ("raw", "gzip", "members"):
            data = b"a" * 131072
            canonical = self.fixture(data, encoding)
            for size in (None, -1):
                with self.subTest(encoding=encoding, size=size):
                    with storage.open_artifact(canonical) as reader:
                        self.assertEqual(reader.read(1) + reader.read(size), data)
                        self.assertTrue(reader._verified)

    def test_lookahead_is_bounded_and_early_close_does_not_drain(self):
        for encoding in ("raw", "gzip"):
            canonical = self.fixture(b"a" * 131072, encoding)
            for count in (0, 1, 7, 65536):
                with self.subTest(encoding=encoding, count=count):
                    reader = storage.open_artifact(canonical)
                    with patch.object(reader._stream, "read", wraps=reader._stream.read) as read:
                        self.assertEqual(reader.read(count), b"a" * count)
                        self.assertEqual(sum(call.args[0] for call in read.call_args_list),
                                         count + 1 if count else 0)
                        self.assertLessEqual(len(reader._pending), 1)
                        self.assertFalse(reader._verified)
                        read.side_effect = AssertionError("Early close must not drain")
                        reader.close()
                    self.assertTrue(reader.closed)
                    self.assertTrue(reader._file.closed)
                    self.assertTrue(reader._stream.closed)
                    reader.close()
                    with self.assertRaises(ValueError):
                        reader.read(1)


if __name__ == "__main__":
    unittest.main()
