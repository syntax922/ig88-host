#!/usr/bin/env python3
"""Tests for the param-proxy sampling clamp (`normalize_qwen_request`).

Run on ig88 (or anywhere with httpx installed) with the stdlib runner — there
is no pytest on the host:

    python3 scripts/test_param_proxy_clamp.py

Importing the proxy module is side-effect-free apart from constructing a
long-lived httpx.Client; no socket is opened until a request is made, and no
server is bound (the listener only starts under `if __name__ == "__main__"`).
"""

import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "param_proxy", os.path.join(_HERE, "lmstudio-param-proxy.py")
)
param_proxy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(param_proxy)

QWEN = "qwen3.5-122b-a10b"          # in QWEN3_MODELS
NOT_QWEN = "some-other-model"       # not in QWEN3_MODELS

JSON_SCHEMA_RF = {
    "type": "json_schema",
    "json_schema": {
        "name": "ParsedActionPlan",
        "schema": {"type": "object", "properties": {"action_type": {"type": "string"}}},
    },
}


def norm(**body):
    return param_proxy.normalize_qwen_request(dict(body))


class WantsJson(unittest.TestCase):
    def test_json_schema(self):
        self.assertTrue(param_proxy.wants_json({"response_format": JSON_SCHEMA_RF}))

    def test_json_object(self):
        self.assertTrue(
            param_proxy.wants_json({"response_format": {"type": "json_object"}})
        )

    def test_absent(self):
        self.assertFalse(param_proxy.wants_json({}))

    def test_text_response_format(self):
        self.assertFalse(param_proxy.wants_json({"response_format": {"type": "text"}}))

    def test_malformed_response_format_is_not_json(self):
        # A non-dict response_format must not raise — fail closed to "prose".
        self.assertFalse(param_proxy.wants_json({"response_format": "json_schema"}))
        self.assertFalse(param_proxy.wants_json({"response_format": None}))


class GreedyFloorExemption(unittest.TestCase):
    def test_structured_output_is_exempt(self):
        self.assertEqual(
            param_proxy.greedy_floor_exemption({"response_format": JSON_SCHEMA_RF}),
            "structured-output",
        )

    def test_explicit_zero_is_exempt(self):
        self.assertEqual(
            param_proxy.greedy_floor_exemption({"temperature": 0.0}), "explicit-temp-0"
        )
        self.assertEqual(
            param_proxy.greedy_floor_exemption({"temperature": 0}), "explicit-temp-0"
        )

    def test_prose_request_is_not_exempt(self):
        self.assertIsNone(param_proxy.greedy_floor_exemption({"temperature": 0.3}))
        self.assertIsNone(param_proxy.greedy_floor_exemption({}))

    def test_false_is_not_a_zero_temperature(self):
        # bool is a subclass of int; `temperature: false` is malformed, not "0".
        self.assertIsNone(param_proxy.greedy_floor_exemption({"temperature": False}))


class ClampBehaviour(unittest.TestCase):
    """The regression this file exists for: dungeonadventures#1367 / Kluster#186."""

    def test_da_action_parser_payload_is_not_refloored(self):
        out = norm(
            model=QWEN,
            temperature=0.0,
            top_p=0.1,
            top_k=10,
            presence_penalty=0.0,
            response_format=JSON_SCHEMA_RF,
            chat_template_kwargs={"enable_thinking": False},
        )
        self.assertEqual(out["temperature"], 0.0)
        self.assertEqual(out["top_p"], 0.1)
        self.assertEqual(out["top_k"], 10)

    def test_explicit_temp_zero_without_json_is_honored(self):
        out = norm(model=QWEN, temperature=0.0)
        self.assertEqual(out["temperature"], 0.0)

    def test_json_request_at_low_but_nonzero_temp_is_honored(self):
        # 0.2 < MIN_TEMPERATURE but the request is structured output.
        out = norm(model=QWEN, temperature=0.2, response_format=JSON_SCHEMA_RF)
        self.assertEqual(out["temperature"], 0.2)

    def test_prose_sub_floor_temp_is_still_floored(self):
        # The clamp's original purpose, intact.
        out = norm(model=QWEN, temperature=0.3)
        self.assertEqual(out["temperature"], param_proxy.MIN_TEMPERATURE)

    def test_prose_defaults_still_fill_when_omitted(self):
        out = norm(model=QWEN, chat_template_kwargs={"enable_thinking": False})
        self.assertEqual(out["temperature"], 0.7)
        self.assertEqual(out["top_p"], 0.80)
        self.assertEqual(out["top_k"], 20)
        self.assertEqual(out["presence_penalty"], 1.5)

    def test_thinking_defaults_unchanged(self):
        out = norm(model=QWEN)
        self.assertEqual(out["temperature"], 1.0)
        self.assertEqual(out["top_p"], 0.95)

    def test_exemption_does_not_leak_to_omitted_temperature(self):
        # A JSON request that sends no temperature still gets the Qwen default;
        # the exemption suppresses RAISING an explicit value, nothing else.
        out = norm(model=QWEN, response_format=JSON_SCHEMA_RF)
        self.assertEqual(out["temperature"], 1.0)

    def test_non_allowlisted_model_untouched(self):
        out = norm(model=NOT_QWEN, temperature=0.0)
        self.assertEqual(out["temperature"], 0.0)
        self.assertNotIn("top_p", out)

    def test_adding_a_model_to_the_allowlist_cannot_refloor_json(self):
        """The tripwire test: every allowlisted model must honor temp-0 JSON."""
        for model in sorted(param_proxy.QWEN3_MODELS):
            out = norm(model=model, temperature=0.0, response_format=JSON_SCHEMA_RF)
            self.assertEqual(out["temperature"], 0.0, "re-floored for %s" % model)


if __name__ == "__main__":
    unittest.main(verbosity=2)
