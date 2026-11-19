import copy
import json
import unittest

from src.converter.anthropic2gemini import clean_json_schema
from src.converter.antigravity_fix import _clean_parameters_json_schema
from src.converter.openai2gemini import (
    _clean_schema_for_parameters_json_schema,
    extract_tool_calls_from_parts,
)


class ToolArgumentTests(unittest.TestCase):
    def test_nullable_required_fields_survive_all_converters(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": ["string", "null"]},
                "count": {"type": "integer", "nullable": True},
                "nested": {"type": "object", "properties": {
                    "value": {"type": ["string", "null"]}}, "required": ["value"]},
            },
            "required": ["name", "count", "nested", "name", "missing"],
        }
        for clean in (clean_json_schema, _clean_parameters_json_schema,
                      _clean_schema_for_parameters_json_schema):
            with self.subTest(converter=clean.__name__):
                result = clean(copy.deepcopy(schema))
                self.assertEqual(result["required"], ["name", "count", "nested"])
                self.assertEqual(result["properties"]["nested"]["required"], ["value"])

    def test_tool_argument_strings_and_invalid_values(self):
        for raw, expected in [('{"path":"demo.txt"}', {"path": "demo.txt"}),
                              ({"path": "demo.txt"}, {"path": "demo.txt"}),
                              ('broken', {}), ('[]', {}), ('null', {}), (None, {})]:
            for streaming in (False, True):
                with self.subTest(raw=raw, streaming=streaming):
                    calls, _ = extract_tool_calls_from_parts(
                        [{"functionCall": {"name": "read_file", "args": raw}}], streaming)
                    self.assertEqual(json.loads(calls[0]["function"]["arguments"]), expected)


if __name__ == '__main__':
    unittest.main()
