import contextlib
import io
import json
import math
import struct
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lisp
from lisppy.contracts import load_contract


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


def validate_wire(value):
    if not isinstance(value, dict) or "tag" not in value:
        raise ValueError("wire value must be a tagged object")
    tag = value["tag"]
    fields = set(value)
    if tag == "nil":
        expected = {"tag"}
    elif tag == "boolean":
        expected = {"tag", "value"}
        if type(value.get("value")) is not bool:
            raise ValueError("boolean value must be bool")
    elif tag == "integer":
        expected = {"tag", "value"}
        text = value.get("value")
        if not isinstance(text, str) or str(int(text)) != text:
            raise ValueError("integer must use canonical decimal text")
    elif tag == "float64":
        expected = {"tag", "bits"}
        bits = value.get("bits")
        if (
            not isinstance(bits, str)
            or len(bits) != 16
            or any(ch not in "0123456789abcdef" for ch in bits)
        ):
            raise ValueError("float64 must use 16 lowercase hex digits")
        if not math.isfinite(struct.unpack(">d", bytes.fromhex(bits))[0]):
            raise ValueError("float64 must be finite")
    elif tag in ("string", "symbol"):
        expected = {"tag", "value"}
        if not isinstance(value.get("value"), str):
            raise ValueError(f"{tag} value must be text")
    elif tag == "list":
        expected = {"tag", "items"}
        if not isinstance(value.get("items"), list):
            raise ValueError("list items must be an array")
        for item in value["items"]:
            validate_wire(item)
    elif tag == "pair":
        expected = {"tag", "car", "cdr"}
        validate_wire(value.get("car"))
        validate_wire(value.get("cdr"))
    elif tag == "map":
        expected = {"tag", "entries"}
        entries = value.get("entries")
        if not isinstance(entries, list):
            raise ValueError("map entries must be an array")
        canonical_keys = []
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 2:
                raise ValueError("map entry must contain key and value")
            validate_wire(entry[0])
            validate_wire(entry[1])
            canonical_keys.append(
                json.dumps(
                    entry[0],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        if canonical_keys != sorted(canonical_keys):
            raise ValueError("map entries must use canonical key order")
        if len(canonical_keys) != len(set(canonical_keys)):
            raise ValueError("map keys must be unique")
    else:
        raise ValueError(f"unknown wire tag: {tag}")
    if fields != expected:
        raise ValueError(f"invalid fields for {tag}: {sorted(fields)}")


def validate_manifest(manifest):
    if set(manifest) != {"schema", "wire", "profile", "cases"}:
        raise ValueError("invalid conformance manifest fields")
    if manifest["schema"] != "lispy-conformance@2":
        raise ValueError("unsupported conformance schema")
    if manifest["wire"] != "lispy-value@1":
        raise ValueError("unsupported value wire")
    seen = set()
    for case in manifest["cases"]:
        if set(case) != {"id", "source", "expect"}:
            raise ValueError("invalid conformance case fields")
        if case["id"] in seen:
            raise ValueError(f"duplicate case id: {case['id']}")
        seen.add(case["id"])
        expect = case["expect"]
        if "stdout" not in expect or not isinstance(expect["stdout"], str):
            raise ValueError("every expectation requires stdout")
        outcomes = {"value", "error"} & set(expect)
        if len(outcomes) != 1 or set(expect) != outcomes | {"stdout"}:
            raise ValueError("expectation requires exactly one outcome")
        if "value" in expect:
            validate_wire(expect["value"])
        else:
            error = expect["error"]
            if (
                set(error) != {"category"}
                or error["category"] not in ERROR_CATEGORIES
            ):
                raise ValueError("invalid portable error category")


def run_case(case):
    leaked = io.StringIO()
    with contextlib.redirect_stdout(leaked):
        result = lisp.LispyVM(profile="core").execute(case["source"])
    if leaked.getvalue():
        raise AssertionError("conformance execution leaked process stdout")
    actual = {"stdout": result.output}
    if result.ok:
        actual["value"] = result.as_wire_dict()["value"]
    else:
        actual["error"] = {"category": result.error["category"]}
    return actual


class ConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_contract()

    def test_manifest_matches_runtime(self):
        validate_manifest(self.manifest)
        self.assertEqual(self.manifest["profile"], lisp.LANGUAGE_PROFILE)

    def test_cases(self):
        for case in self.manifest["cases"]:
            with self.subTest(case=case["id"]):
                first = run_case(case)
                second = run_case(case)
                self.assertEqual(first, second)
                self.assertEqual(first, case["expect"])
                json.dumps(first, allow_nan=False)

    def test_validator_rejects_ambiguous_expectation(self):
        invalid = {
            "schema": "lispy-conformance@2",
            "wire": "lispy-value@1",
            "profile": "lispy-core@1",
            "cases": [
                {
                    "id": "bad",
                    "source": "1",
                    "expect": {
                        "value": {"tag": "integer", "value": "1"},
                        "error": {"category": "evaluation"},
                        "stdout": "",
                    },
                }
            ],
        }
        with self.assertRaises(ValueError):
            validate_manifest(invalid)


if __name__ == "__main__":
    unittest.main()
