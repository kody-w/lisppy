import hashlib
import http.client
import importlib.util
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "verify_testpypi.py"
SPEC = importlib.util.spec_from_file_location("verify_testpypi", SCRIPT)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def payload(version, expected, *, extra=None, yanked=False):
    urls = [
        {
            "filename": filename,
            "digests": {"sha256": value},
            "yanked": yanked,
        }
        for filename, value in expected.items()
    ]
    if extra is not None:
        urls.append(extra)
    return {
        "info": {
            "name": verify.DISTRIBUTION,
            "version": version,
        },
        "urls": urls,
    }


class TestPyPIVerifierTests(unittest.TestCase):
    def test_manifest_requires_exact_regular_release_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dist = root / "dist"
            dist.mkdir()
            files = {
                "package-1.0.0-py3-none-any.whl": b"wheel",
                "package-1.0.0.tar.gz": b"source",
            }
            for filename, content in files.items():
                (dist / filename).write_bytes(content)
            manifest = root / "SHA256SUMS"
            manifest.write_text(
                "".join(
                    f"{digest(content)}  dist/{filename}\n"
                    for filename, content in sorted(files.items())
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                verify.load_expected_files(manifest, dist),
                {
                    filename: digest(content)
                    for filename, content in files.items()
                },
            )
            (dist / "extra.txt").write_text("extra", encoding="utf-8")
            with self.assertRaises(verify.VerificationError):
                verify.load_expected_files(manifest, dist)

    def test_release_tag_is_strict(self):
        self.assertEqual(verify.release_version("v1.2.3"), "1.2.3")
        for tag in ("1.2.3", "v01.2.3", "v1.2", "v1.2.3rc1", None):
            with self.subTest(tag=tag):
                with self.assertRaises(verify.VerificationError):
                    verify.release_version(tag)

    def test_retry_sequence_converges_on_exact_remote_files(self):
        expected = {
            "package.whl": "a" * 64,
            "package.tar.gz": "b" * 64,
        }
        responses = [
            urllib.error.HTTPError("url", 404, "missing", {}, None),
            {
                "info": {
                    "name": verify.DISTRIBUTION,
                    "version": "1.2.3",
                },
                "urls": [],
            },
            payload("1.2.3", expected),
        ]
        delays = []

        def fetch(_url):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        self.assertEqual(
            verify.verify_testpypi(
                expected,
                "1.2.3",
                fetch_json=fetch,
                sleep=delays.append,
            ),
            expected,
        )
        self.assertEqual(delays, [5, 10])

    def test_transport_interruptions_are_retried(self):
        expected = {
            "package.whl": "a" * 64,
            "package.tar.gz": "b" * 64,
        }
        responses = [
            ConnectionResetError("reset"),
            http.client.IncompleteRead(b"partial"),
            payload("1.2.3", expected),
        ]
        delays = []

        def fetch(_url):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        self.assertEqual(
            verify.verify_testpypi(
                expected,
                "1.2.3",
                fetch_json=fetch,
                sleep=delays.append,
            ),
            expected,
        )
        self.assertEqual(delays, [5, 10])

    def test_wrong_extra_yanked_and_forbidden_responses_fail(self):
        expected = {
            "package.whl": "a" * 64,
            "package.tar.gz": "b" * 64,
        }
        cases = [
            payload("9.9.9", expected),
            payload(
                "1.2.3",
                expected,
                extra={
                    "filename": "extra.zip",
                    "digests": {"sha256": "c" * 64},
                    "yanked": False,
                },
            ),
            payload("1.2.3", expected, yanked=True),
        ]
        for response in cases:
            with self.subTest(response=response):
                with self.assertRaises(verify.VerificationError):
                    verify.verify_testpypi(
                        expected,
                        "1.2.3",
                        fetch_json=lambda _url, value=response: value,
                        sleep=lambda _delay: None,
                    )

        calls = []
        delays = []

        def forbidden(_url):
            calls.append("fetch")
            raise urllib.error.HTTPError(
                "url",
                403,
                "forbidden",
                {},
                None,
            )

        with self.assertRaises(verify.VerificationError):
            verify.verify_testpypi(
                expected,
                "1.2.3",
                fetch_json=forbidden,
                sleep=delays.append,
            )
        self.assertEqual(calls, ["fetch"])
        self.assertEqual(delays, [])


if __name__ == "__main__":
    unittest.main()
