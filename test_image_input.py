import unittest
from pathlib import Path

from src.converter.image_input import (
    ImageInputError,
    normalize_inline_image_data,
    normalize_inline_image_part,
)


JPEG_B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2w=="
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"


class ImageInputNormalizationTests(unittest.TestCase):
    def test_normalizes_jpg_alias_and_strips_data_url_prefix(self):
        inline = normalize_inline_image_data(
            {
                "mimeType": "image/jpg",
                "data": f"data:image/jpg;base64,{JPEG_B64}",
            }
        )

        self.assertEqual(inline["mimeType"], "image/jpeg")
        self.assertEqual(inline["data"], JPEG_B64)

    def test_repairs_generic_mime_type_from_image_magic(self):
        inline = normalize_inline_image_data(
            {
                "mimeType": "application/octet-stream",
                "data": PNG_B64,
            }
        )

        self.assertEqual(inline["mimeType"], "image/png")
        self.assertEqual(inline["data"], PNG_B64)

    def test_accepts_urlsafe_base64_image_data(self):
        inline = normalize_inline_image_data(
            {
                "mimeType": "image/jpeg",
                "data": JPEG_B64.replace("+", "-").replace("/", "_"),
            }
        )

        self.assertEqual(inline["mimeType"], "image/jpeg")
        self.assertEqual(inline["data"], JPEG_B64)

    def test_normalizes_snake_case_inline_data_part(self):
        part = normalize_inline_image_part(
            {
                "inline_data": {
                    "mime_type": "image/jpg",
                    "data": JPEG_B64,
                }
            }
        )

        self.assertIn("inlineData", part)
        self.assertNotIn("inline_data", part)
        self.assertEqual(part["inlineData"]["mimeType"], "image/jpeg")

    def test_leaves_explicit_non_image_inline_data_unchecked(self):
        part = normalize_inline_image_part(
            {
                "inlineData": {
                    "mimeType": "audio/wav",
                    "data": "UklGRg==",
                }
            }
        )

        self.assertEqual(part["inlineData"]["mimeType"], "audio/wav")
        self.assertEqual(part["inlineData"]["data"], "UklGRg==")

    def test_rejects_invalid_base64_before_upstream(self):
        with self.assertRaises(ImageInputError):
            normalize_inline_image_data(
                {
                    "mimeType": "image/jpeg",
                    "data": "data:image/jpeg;base64,not valid base64!!!",
                }
            )


class ImageInputIntegrationSourceTests(unittest.TestCase):
    def test_openai_converter_uses_shared_image_url_normalizer(self):
        source = Path("src/converter/openai2gemini.py").read_text(encoding="utf-8")

        self.assertIn("image_url_to_inline_data", source)
        self.assertIn('parts.append({"inlineData": await image_url_to_inline_data(image_url)})', source)

    def test_gemini_normalizer_cleans_inline_image_parts(self):
        source = Path("src/converter/gemini_fix.py").read_text(encoding="utf-8")

        self.assertIn("normalize_inline_image_part", source)
        self.assertIn("part = normalize_inline_image_part(part)", source)

    def test_routers_return_bad_image_as_400(self):
        router_files = [
            "src/router/geminicli/openai.py",
            "src/router/geminicli/gemini.py",
            "src/router/geminicli/anthropic.py",
            "src/router/antigravity/openai.py",
            "src/router/antigravity/gemini.py",
            "src/router/antigravity/anthropic.py",
            "src/router/vertex/openai.py",
            "src/router/vertex/gemini.py",
        ]

        for router_file in router_files:
            with self.subTest(router_file=router_file):
                source = Path(router_file).read_text(encoding="utf-8")
                self.assertIn("ImageInputError", source)
                self.assertIn("status_code=400", source)


if __name__ == "__main__":
    unittest.main()
