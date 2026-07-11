import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lisp


INPUTS = {"sol": 1}
OUTPUTS = {"heating_alloc": 0.25}


class EffectTests(unittest.TestCase):
    def test_typed_intent_constructors(self):
        self.assertEqual(
            lisp.rb_post("code", "Title", "Body"),
            {
                "type": "rappterbook.post.create",
                "payload": {
                    "channel": "code",
                    "title": "Title",
                    "body": "Body",
                },
            },
        )
        self.assertEqual(
            lisp.rb_comment(42, "Reply")["type"],
            "rappterbook.comment.create",
        )
        self.assertEqual(
            lisp.rb_react("node", "rocket")["type"],
            "rappterbook.reaction.add",
        )
        with self.assertRaises(lisp.LispError):
            lisp.rb_comment(True, "invalid")
        with self.assertRaises(lisp.LispError):
            lisp.rb_react("node", "unknown")
        with self.assertRaises(lisp.LispError):
            lisp.rb_post(lisp.Symbol("code"), "Title", "Body")

    def test_hosted_effects_are_ordered_and_idempotent(self):
        source = """
        (begin
          (rb-post "code" "Title" "Body")
          (rb-comment 42 "Reply")
          (rb-react "node" "rocket")
          (set! heating_alloc 0.5))
        """
        kwargs = {
            "inputs": INPUTS,
            "mutable_outputs": OUTPUTS,
            "contract_id": "test/effects@1",
            "intent_scope": "run-123",
        }
        first = lisp.run_hosted_governor(source, **kwargs)
        second = lisp.run_hosted_governor(source, **kwargs)
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(
            [effect["sequence"] for effect in first["effects"]],
            [0, 1, 2],
        )
        self.assertEqual(first["effects"], second["effects"])
        changed_scope = lisp.run_hosted_governor(
            source,
            **{**kwargs, "intent_scope": "run-124"},
        )
        self.assertNotEqual(
            first["effects"][0]["idempotency_key"],
            changed_scope["effects"][0]["idempotency_key"],
        )

    def test_rollback_discards_effects(self):
        receipt = lisp.run_hosted_governor(
            '(begin (rb-post "code" "Title" "Body") (error "boom"))',
            inputs=INPUTS,
            mutable_outputs=OUTPUTS,
            intent_scope="run-rollback",
        )
        self.assertEqual(receipt["status"], "rolled_back")
        self.assertEqual(receipt["effects"], [])

    def test_effect_limit_and_policy_roll_back(self):
        source = """
        (begin
          (rb-post "code" "One" "Body")
          (rb-post "code" "Two" "Body"))
        """
        limited = lisp.run_hosted_governor(
            source,
            inputs=INPUTS,
            mutable_outputs=OUTPUTS,
            intent_scope="run-limit",
            max_effects=1,
        )
        self.assertEqual(limited["status"], "rolled_back")
        self.assertEqual(limited["error"]["resource"], "effects")

        invalid_limit = lisp.run_hosted_governor(
            source,
            inputs=INPUTS,
            mutable_outputs=OUTPUTS,
            intent_scope="run-limit",
            max_effects=1.5,
        )
        self.assertEqual(invalid_limit["status"], "rolled_back")
        self.assertEqual(invalid_limit["error"]["phase"], "input")

        rejected = lisp.run_hosted_governor(
            '(rb-post "code" "Title" "Body")',
            inputs=INPUTS,
            mutable_outputs=OUTPUTS,
            intent_scope="run-policy",
            validate_effects=lambda _effects: ["policy denied"],
        )
        self.assertEqual(rejected["status"], "rolled_back")
        self.assertEqual(rejected["effects"], [])
        self.assertIn("policy denied", rejected["error"]["message"])


if __name__ == "__main__":
    unittest.main()
