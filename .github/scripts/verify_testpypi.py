#!/usr/bin/env python3
"""Verify that TestPyPI serves the exact locally tested release files."""

import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


DISTRIBUTION = "rappterbook-lispy-runtime"
CHECKSUM_PATTERN = re.compile(
    r"^([0-9a-f]{64})  dist/([A-Za-z0-9][A-Za-z0-9._+-]*)$"
)
VERSION_PATTERN = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RETRYABLE_HTTP_STATUS = {404, 408, 425, 429}
MAX_ATTEMPTS = 10


class VerificationError(RuntimeError):
    pass


class RetryableVerificationError(VerificationError):
    pass


def release_version(tag):
    if not isinstance(tag, str) or VERSION_PATTERN.fullmatch(tag) is None:
        raise VerificationError("RELEASE_TAG is not a canonical version tag")
    return tag[1:]


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_expected_files(manifest_path=Path("SHA256SUMS"), dist_dir=Path("dist")):
    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    expected = {}
    for line in lines:
        match = CHECKSUM_PATTERN.fullmatch(line)
        if match is None:
            raise VerificationError("SHA256SUMS contains an invalid line")
        digest, filename = match.groups()
        if filename in expected:
            raise VerificationError("SHA256SUMS contains a duplicate file")
        expected[filename] = digest
    if (
        len(expected) != 2
        or sum(name.endswith(".whl") for name in expected) != 1
        or sum(name.endswith(".tar.gz") for name in expected) != 1
    ):
        raise VerificationError(
            "SHA256SUMS must contain one wheel and one source archive"
        )

    actual_names = set()
    for path in dist_dir.iterdir():
        if path.is_symlink() or not path.is_file():
            raise VerificationError("dist contains a non-regular file")
        actual_names.add(path.name)
    if actual_names != set(expected):
        raise VerificationError("dist files do not match SHA256SUMS")
    for filename, digest in expected.items():
        if _sha256(dist_dir / filename) != digest:
            raise VerificationError(f"local digest mismatch: {filename}")
    return expected


def _fetch_json(url):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _remote_digests(payload, version, expected):
    if not isinstance(payload, dict):
        raise RetryableVerificationError("TestPyPI response is not an object")
    info = payload.get("info")
    urls = payload.get("urls")
    if not isinstance(info, dict) or not isinstance(urls, list):
        raise RetryableVerificationError("TestPyPI response is incomplete")
    if (
        info.get("name") != DISTRIBUTION
        or info.get("version") != version
    ):
        raise VerificationError("TestPyPI returned the wrong project or version")

    remote = {}
    for item in urls:
        if not isinstance(item, dict):
            raise RetryableVerificationError("TestPyPI file entry is invalid")
        filename = item.get("filename")
        digests = item.get("digests")
        if not isinstance(filename, str) or not isinstance(digests, dict):
            raise RetryableVerificationError("TestPyPI file entry is incomplete")
        if filename in remote:
            raise VerificationError("TestPyPI returned a duplicate filename")
        if item.get("yanked") is True:
            raise VerificationError(f"TestPyPI file is yanked: {filename}")
        digest = digests.get("sha256")
        if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
            raise VerificationError("TestPyPI returned a malformed SHA-256")
        remote[filename] = digest

    if set(remote) - set(expected):
        raise VerificationError("TestPyPI returned unexpected files")
    if set(remote) != set(expected):
        raise RetryableVerificationError("TestPyPI files are incomplete")
    if remote != expected:
        raise RetryableVerificationError("TestPyPI digest mismatch")
    return remote


def _retry_delay(attempt):
    return min(5 * (2 ** attempt), 30)


def verify_testpypi(expected, version, *, fetch_json=_fetch_json, sleep=time.sleep):
    url = (
        f"https://test.pypi.org/pypi/{DISTRIBUTION}/"
        f"{version}/json"
    )
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            payload = fetch_json(url)
            return _remote_digests(payload, version, expected)
        except urllib.error.HTTPError as exc:
            if (
                exc.code not in RETRYABLE_HTTP_STATUS
                and exc.code < 500
            ):
                raise VerificationError(
                    f"TestPyPI rejected verification with HTTP {exc.code}"
                ) from exc
            last_error = exc
        except (
            http.client.HTTPException,
            json.JSONDecodeError,
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            urllib.error.URLError,
        ) as exc:
            last_error = exc
        except RetryableVerificationError as exc:
            last_error = exc
        if attempt + 1 < MAX_ATTEMPTS:
            sleep(_retry_delay(attempt))
    raise VerificationError(
        f"TestPyPI verification did not converge: {last_error}"
    )


def main():
    try:
        version = release_version(os.environ.get("RELEASE_TAG"))
        expected = load_expected_files()
        verify_testpypi(expected, version)
    except (OSError, VerificationError) as exc:
        print(f"TestPyPI verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"Verified TestPyPI {DISTRIBUTION} {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
