from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from gridzilla.endpoints import load_endpoint, validate_padded_size
from gridzilla.models import ValidationError, load_profile, plan_profile


ROOT = Path(__file__).resolve().parents[1]


class GenericEndpointTests(unittest.TestCase):
    def _config(self, root: Path, **overrides):
        value = {
            "id": "local-json-api",
            "name": "Local JSON API",
            "url": "http://127.0.0.1:8081/v1/complete",
            "requestTemplate": {"prompt": {"content": "ok"}, "count": 3},
            "padJsonPointer": "/prompt/content",
            "expectedStatuses": [200],
        }
        value.update(overrides)
        path = root / "local.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_exact_sizes_count_serialized_json_utf8_and_preserve_fields(self) -> None:
        with TemporaryDirectory() as temporary:
            endpoint = load_endpoint(self._config(Path(temporary)))
            for target in (16 * 1024, 64 * 1024, 256 * 1024):
                serialized, actual = endpoint.body_template(target)
                self.assertEqual(actual, target)
                self.assertEqual(len(serialized.encode("utf-8")), target)
                parsed = json.loads(serialized)
                self.assertEqual(parsed["count"], 3)
                self.assertTrue(parsed["prompt"]["content"].startswith("ok"))
                self.assertEqual(parsed["prompt"]["content"], "ok" + "x" * (target - len(endpoint.body_template()[0].encode("utf-8"))))

    def test_generic_json_does_not_require_openai_fields(self) -> None:
        with TemporaryDirectory() as temporary:
            endpoint = load_endpoint(self._config(Path(temporary), requestTemplate={"query": "hello", "top": 2},
                                                  padJsonPointer="/query"))
            serialized, size = endpoint.body_template()
            self.assertEqual(json.loads(serialized), {"query": "hello", "top": 2})
            self.assertEqual(size, len(serialized.encode("utf-8")))

    def test_json_pointer_can_pad_openai_message_content_inside_an_array(self) -> None:
        template = {"model": "model-a", "messages": [{"role": "user", "content": "hello"}]}
        for target in (16384, 65536, 262144):
            self.assertEqual(validate_padded_size(template, "/messages/0/content", target), target)

    def test_rejects_template_too_large_for_requested_size_and_unsupported_pointer(self) -> None:
        with self.assertRaisesRegex(ValidationError, "too small"):
            validate_padded_size({"text": "this is already longer"}, "/text", 4)
        with self.assertRaisesRegex(ValidationError, "string"):
            validate_padded_size({"model": "m", "messages": []}, "/messages", 100)

    def test_server_endpoint_configuration_rejects_embedded_credentials_and_query(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValidationError, "credentials"):
                load_endpoint(self._config(root, url="https://user:secret@example.test/api"))
            with self.assertRaisesRegex(ValidationError, "query"):
                load_endpoint(self._config(root, url="https://example.test/api?token=secret"))

    def test_size_sweep_planner_separates_generic_and_grid_phases(self) -> None:
        profile = load_profile(ROOT / "profiles" / "body-sizes.json")
        generic = plan_profile(profile, scenario="generic", has_reference=False)
        grid = plan_profile(profile, scenario="grid", has_reference=True)
        self.assertEqual(generic["measurementWindowCount"], 9)
        self.assertEqual(generic["durationSeconds"], 150)
        self.assertEqual(grid["measurementWindowCount"], 36)
        self.assertEqual(grid["durationSeconds"], 600)
        self.assertEqual(grid["requestBodySizesBytes"], [16384, 65536, 262144])


if __name__ == "__main__":
    unittest.main()
