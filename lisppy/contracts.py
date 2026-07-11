"""Installed access to canonical LisPy compatibility contracts."""

import argparse
import hashlib
import json
import math
import pkgutil
import struct
from pathlib import Path


PROFILE = "lispy-core@1"
RESOURCE_ROOT = "data/contracts/lispy-core@1"
RESOURCE_NAMES = (
    "CONFORMANCE.md",
    "README.md",
    "conformance.json",
    "profile.json",
    "stdlib.lisp",
)
PROFILE_FIELDS = {
    "api",
    "profile",
    "corpus",
    "wire",
    "status",
    "stdlib",
    "host_extensions",
}
CONTRACT_MAX_BYTES = 8_388_608
CONTRACT_MAX_DEPTH = 128
CONTRACT_MAX_NODES = 100_000
ERROR_CATEGORIES = {
    "capability",
    "evaluation",
    "invalid-data",
    "resource-limit",
    "serialization",
    "syntax",
    "unsupported",
    "validation",
}


def contract_bytes(name):
    if name not in RESOURCE_NAMES:
        raise ValueError(f"unknown contract resource: {name}")
    content = pkgutil.get_data("lisppy", f"{RESOURCE_ROOT}/{name}")
    if content is None:
        raise RuntimeError(f"missing contract resource: {name}")
    return content


def load_contract(profile=PROFILE):
    if profile != PROFILE:
        raise ValueError(f"unknown contract profile: {profile}")
    return json.loads(contract_bytes("conformance.json").decode("utf-8"))


def _validate_profile(value):
    if (
        not isinstance(value, dict)
        or set(value) != PROFILE_FIELDS
        or value["api"] != "lispy.contract-profile/v1"
        or value["profile"] != PROFILE
        or value["corpus"] != "lispy-conformance@2"
        or value["wire"] != "lispy-value@1"
        or value["status"] != "implemented-reference"
        or value["stdlib"]
        != {
            "resource": "stdlib.lisp",
            "kind": "portable-core",
            "exports": [
                "identity",
                "constantly",
                "complement",
                "partial",
            ],
        }
        or value["host_extensions"] != "excluded"
    ):
        raise ValueError("invalid contract profile resource")
    return value


def load_profile(profile=PROFILE):
    if profile != PROFILE:
        raise ValueError(f"unknown contract profile: {profile}")
    value = json.loads(contract_bytes("profile.json").decode("utf-8"))
    return _validate_profile(value)


def contract_manifest(profile=PROFILE):
    if profile != PROFILE:
        raise ValueError(f"unknown contract profile: {profile}")
    files = []
    for name in RESOURCE_NAMES:
        content = contract_bytes(name)
        files.append(
            {
                "path": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    corpus = load_contract(profile)
    profile_data = load_profile(profile)
    if (
        corpus["profile"] != profile_data["profile"]
        or corpus["schema"] != profile_data["corpus"]
        or corpus["wire"] != profile_data["wire"]
    ):
        raise ValueError("contract profile and corpus identities differ")
    payload = {
        "api": "lispy.contract-manifest/v1",
        "profile": profile,
        "schema": corpus["schema"],
        "wire": corpus["wire"],
        "case_count": len(corpus["cases"]),
        "files": files,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **payload,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def contract_bundle(profile=PROFILE):
    return {
        "api": "lispy.contract-pack/v1",
        "manifest": contract_manifest(profile),
        "conformance": load_contract(profile),
    }


def contract_bundle_v2(profile=PROFILE):
    manifest = contract_manifest(profile)
    resources = []
    for item in manifest["files"]:
        content = contract_bytes(item["path"])
        resources.append(
            {
                **item,
                "encoding": "utf-8",
                "content": content.decode("utf-8"),
            }
        )
    return {
        "api": "lispy.contract-pack/v2",
        "manifest": manifest,
        "resources": resources,
    }


def verify_contract_bundle_v2(bundle, *, expected_manifest_sha256=None):
    if not isinstance(bundle, dict) or set(bundle) != {
        "api",
        "manifest",
        "resources",
    }:
        raise ValueError("invalid contract pack v2 fields")
    if bundle["api"] != "lispy.contract-pack/v2":
        raise ValueError("invalid contract pack v2 identity")
    manifest = bundle["manifest"]
    if not isinstance(manifest, dict) or set(manifest) != {
        "api",
        "profile",
        "schema",
        "wire",
        "case_count",
        "files",
        "sha256",
    }:
        raise ValueError("invalid contract manifest fields")
    if (
        manifest["api"] != "lispy.contract-manifest/v1"
        or manifest["profile"] != PROFILE
    ):
        raise ValueError("invalid contract manifest identity")
    if expected_manifest_sha256 is not None and (
        not isinstance(expected_manifest_sha256, str)
        or expected_manifest_sha256 != manifest["sha256"]
    ):
        raise ValueError("external contract manifest pin mismatch")
    resources = bundle["resources"]
    if not isinstance(resources, list):
        raise ValueError("contract resources must be a list")
    paths = [resource.get("path") for resource in resources if isinstance(resource, dict)]
    if paths != list(RESOURCE_NAMES) or len(paths) != len(set(paths)):
        raise ValueError("contract resources are not closed and canonical")
    files = []
    content_by_path = {}
    for resource in resources:
        if set(resource) != {
            "path",
            "size",
            "sha256",
            "encoding",
            "content",
        }:
            raise ValueError("invalid contract resource fields")
        if resource["encoding"] != "utf-8":
            raise ValueError("unsupported contract resource encoding")
        if not isinstance(resource["content"], str):
            raise ValueError("contract resource content must be text")
        content = resource["content"].encode("utf-8")
        if resource["size"] != len(content):
            raise ValueError("contract resource size mismatch")
        if resource["sha256"] != hashlib.sha256(content).hexdigest():
            raise ValueError("contract resource digest mismatch")
        files.append(
            {
                "path": resource["path"],
                "size": resource["size"],
                "sha256": resource["sha256"],
            }
        )
        content_by_path[resource["path"]] = content
    if manifest["files"] != files:
        raise ValueError("contract manifest resource list mismatch")
    payload = {
        key: manifest[key]
        for key in (
            "api",
            "profile",
            "schema",
            "wire",
            "case_count",
            "files",
        )
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if manifest["sha256"] != hashlib.sha256(encoded).hexdigest():
        raise ValueError("contract manifest digest mismatch")
    profile = _validate_profile(
        json.loads(content_by_path["profile.json"].decode("utf-8"))
    )
    corpus = json.loads(
        content_by_path["conformance.json"].decode("utf-8")
    )
    _validate_conformance(corpus)
    if (
        corpus.get("profile") != profile["profile"]
        or corpus.get("schema") != profile["corpus"]
        or corpus.get("wire") != profile["wire"]
        or manifest["schema"] != corpus["schema"]
        or manifest["wire"] != corpus["wire"]
        or manifest["case_count"] != len(corpus.get("cases", []))
    ):
        raise ValueError("contract semantic identities differ")
    return bundle


def _validate_conformance(corpus):
    if not isinstance(corpus, dict) or set(corpus) != {
        "schema",
        "wire",
        "profile",
        "cases",
    }:
        raise ValueError("invalid conformance manifest fields")
    cases = corpus["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("conformance cases must be a non-empty list")
    seen = set()
    for case in cases:
        if (
            not isinstance(case, dict)
            or set(case) != {"id", "source", "expect"}
            or not isinstance(case["id"], str)
            or not case["id"]
            or case["id"] in seen
            or not isinstance(case["source"], str)
            or not isinstance(case["expect"], dict)
        ):
            raise ValueError("invalid conformance case")
        seen.add(case["id"])
        expectation = case["expect"]
        outcomes = {"value", "error"} & set(expectation)
        if (
            not isinstance(expectation.get("stdout"), str)
            or len(outcomes) != 1
            or set(expectation) != outcomes | {"stdout"}
        ):
            raise ValueError("invalid conformance expectation")
        if "value" in expectation:
            _validate_wire(expectation["value"])
        else:
            error = expectation["error"]
            if (
                not isinstance(error, dict)
                or set(error) != {"category"}
                or error["category"] not in ERROR_CATEGORIES
            ):
                raise ValueError("invalid portable error")


def _validate_wire(value):
    stack = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > CONTRACT_MAX_NODES or depth > CONTRACT_MAX_DEPTH:
            raise ValueError("portable value exceeds complexity limit")
        if not isinstance(current, dict) or "tag" not in current:
            raise ValueError("wire value must be tagged")
        tag = current["tag"]
        fields = set(current)
        if tag == "nil":
            expected = {"tag"}
        elif tag == "boolean":
            expected = {"tag", "value"}
            if type(current.get("value")) is not bool:
                raise ValueError("invalid boolean wire value")
        elif tag == "integer":
            expected = {"tag", "value"}
            text = current.get("value")
            if (
                not isinstance(text, str)
                or not text
                or str(int(text)) != text
            ):
                raise ValueError("invalid integer wire value")
        elif tag == "float64":
            expected = {"tag", "bits"}
            bits = current.get("bits")
            if (
                not isinstance(bits, str)
                or len(bits) != 16
                or any(char not in "0123456789abcdef" for char in bits)
            ):
                raise ValueError("invalid float wire value")
            if not math.isfinite(struct.unpack(">d", bytes.fromhex(bits))[0]):
                raise ValueError("non-finite float wire value")
        elif tag in ("string", "symbol"):
            expected = {"tag", "value"}
            if not isinstance(current.get("value"), str):
                raise ValueError("invalid text wire value")
        elif tag == "list":
            expected = {"tag", "items"}
            items = current.get("items")
            if not isinstance(items, list):
                raise ValueError("invalid list wire value")
            stack.extend((item, depth + 1) for item in items)
        elif tag == "pair":
            expected = {"tag", "car", "cdr"}
            stack.append((current.get("car"), depth + 1))
            stack.append((current.get("cdr"), depth + 1))
        elif tag == "map":
            expected = {"tag", "entries"}
            entries = current.get("entries")
            if not isinstance(entries, list):
                raise ValueError("invalid map wire value")
            canonical = []
            for entry in entries:
                if not isinstance(entry, list) or len(entry) != 2:
                    raise ValueError("invalid map wire entry")
                stack.append((entry[0], depth + 1))
                stack.append((entry[1], depth + 1))
                canonical.append(
                    json.dumps(
                        entry[0],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            if canonical != sorted(canonical) or len(canonical) != len(set(canonical)):
                raise ValueError("map wire keys are not canonical")
        else:
            raise ValueError("unknown wire tag")
        if fields != expected:
            raise ValueError("invalid wire value fields")


def _strict_json_loads(source):
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    value = json.loads(
        source,
        object_pairs_hook=object_pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"invalid JSON constant: {value}")
        ),
    )
    stack = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > CONTRACT_MAX_NODES or depth > CONTRACT_MAX_DEPTH:
            raise ValueError("contract JSON exceeds complexity limit")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return value


def _argument_parser():
    parser = argparse.ArgumentParser(
        prog="python -m lisppy.contracts",
        description="Export a canonical installed LisPy contract pack.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--format",
        choices=("v1", "v2"),
        default="v2",
        help="export legacy parsed v1 or self-verifying resource v2",
    )
    parser.add_argument(
        "--verify",
        metavar="PATH",
        help="verify a self-contained contract-pack v2 JSON file",
    )
    parser.add_argument(
        "--expect-manifest",
        metavar="SHA256",
        help="require an externally delivered manifest SHA-256",
    )
    return parser


def main(argv=None):
    args = _argument_parser().parse_args(argv)
    if args.expect_manifest is not None and args.verify is None:
        _argument_parser().error("--expect-manifest requires --verify")
    if args.verify is not None:
        if args.format != "v2":
            _argument_parser().error("--verify cannot be combined with --format v1")
        try:
            with Path(args.verify).open("rb") as file:
                raw = file.read(CONTRACT_MAX_BYTES + 1)
            if len(raw) > CONTRACT_MAX_BYTES:
                raise ValueError("contract pack exceeds byte limit")
            bundle = _strict_json_loads(
                raw.decode("utf-8")
            )
            verified = verify_contract_bundle_v2(
                bundle,
                expected_manifest_sha256=args.expect_manifest,
            )
        except (
            AttributeError,
            OSError,
            RecursionError,
            TypeError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
        ):
            print(
                json.dumps(
                    {
                        "api": "lispy.error/v1",
                        "ok": False,
                        "error": {
                            "category": "contract",
                            "code": "contract_verification_failed",
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 1
        print(
            json.dumps(
                {
                    "api": "lispy.contract-verification/v1",
                    "ok": True,
                    "manifest_sha256": verified["manifest"]["sha256"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    bundle = contract_bundle() if args.format == "v1" else contract_bundle_v2()
    print(
        json.dumps(
            bundle,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
