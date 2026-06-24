import base64
import binascii
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse


class ImageInputError(ValueError):
    pass


SUPPORTED_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
}

MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/x-png": "image/png",
}

GENERIC_MIME_TYPES = {
    "",
    "application/octet-stream",
    "binary/octet-stream",
    "image/*",
}

MAX_REMOTE_IMAGE_BYTES = 20 * 1024 * 1024

DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;,]+)?(?P<params>(?:;[^,]*)*),(?P<data>.*)$", re.DOTALL)


def normalize_mime_type(mime_type: Optional[str]) -> str:
    value = (mime_type or "").split(";", 1)[0].strip().lower()
    return MIME_ALIASES.get(value, value)


def split_data_url(value: str) -> Tuple[Optional[str], str]:
    match = DATA_URL_RE.match(value.strip())
    if not match:
        return None, value

    params = (match.group("params") or "").lower().split(";")
    if "base64" not in params:
        raise ImageInputError("Image data URL must use base64 encoding")

    return match.group("mime"), match.group("data")


def clean_base64_image_data(value: Any) -> Tuple[str, bytes]:
    if not isinstance(value, str) or not value.strip():
        raise ImageInputError("Image data must be a non-empty base64 string")

    cleaned = re.sub(r"\s+", "", value)
    padding = (-len(cleaned)) % 4
    if padding:
        cleaned += "=" * padding

    try:
        decoded = base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageInputError("Image data is not valid base64") from exc

    if not decoded:
        raise ImageInputError("Image data decoded to empty bytes")

    return cleaned, decoded


def detect_image_mime(data: bytes) -> Optional[str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12].decode("latin1", errors="ignore").lower()
        compatible = data[12:64].decode("latin1", errors="ignore").lower()
        if brand in {"heic", "heix", "hevc", "hevx"} or any(
            tag in compatible for tag in ("heic", "heix", "hevc", "hevx")
        ):
            return "image/heic"
        if brand in {"heif", "heis", "mif1", "msf1"} or any(
            tag in compatible for tag in ("heif", "heis", "mif1", "msf1")
        ):
            return "image/heif"
    return None


def choose_image_mime(explicit_mime: Optional[str], detected_mime: Optional[str]) -> str:
    normalized = normalize_mime_type(explicit_mime)

    if detected_mime and (
        not normalized
        or normalized in GENERIC_MIME_TYPES
        or normalized not in SUPPORTED_IMAGE_MIME_TYPES
        or normalized != detected_mime
    ):
        normalized = detected_mime

    if normalized in GENERIC_MIME_TYPES:
        raise ImageInputError("Image MIME type is missing")

    if normalized not in SUPPORTED_IMAGE_MIME_TYPES:
        raise ImageInputError(f"Unsupported MIME type: {normalized}")

    return normalized


def normalize_inline_image_data(inline_data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(inline_data, dict):
        raise ImageInputError("inlineData must be an object")

    raw_data = inline_data.get("data")
    data_url_mime, raw_data = split_data_url(raw_data or "")
    explicit_mime = inline_data.get("mimeType") or inline_data.get("mime_type") or data_url_mime
    if data_url_mime and (
        not explicit_mime or normalize_mime_type(explicit_mime) in GENERIC_MIME_TYPES
    ):
        explicit_mime = data_url_mime

    cleaned_data, decoded = clean_base64_image_data(raw_data)
    detected_mime = detect_image_mime(decoded)
    mime_type = choose_image_mime(explicit_mime, detected_mime)

    normalized = {
        key: value
        for key, value in inline_data.items()
        if key not in {"mimeType", "mime_type", "data"}
    }
    normalized["mimeType"] = mime_type
    normalized["data"] = cleaned_data
    return normalized


def normalize_inline_image_part(part: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(part)
    inline = normalized.get("inlineData")
    if inline is None and "inline_data" in normalized:
        inline = normalized.pop("inline_data")

    if inline is not None:
        normalized["inlineData"] = normalize_inline_image_data(inline)

    return normalized


async def image_url_to_inline_data(image_url: str) -> Dict[str, str]:
    if not isinstance(image_url, str) or not image_url.strip():
        raise ImageInputError("image_url.url must be a non-empty string")

    image_url = image_url.strip()
    if image_url.startswith("data:"):
        return normalize_inline_image_data({"data": image_url})

    parsed = urlparse(image_url)
    if parsed.scheme not in {"http", "https"}:
        raise ImageInputError("image_url.url must be a data URL or http(s) URL")

    return await download_image_url_to_inline_data(image_url)


async def download_image_url_to_inline_data(image_url: str) -> Dict[str, str]:
    try:
        import httpx
    except ImportError as exc:
        raise ImageInputError("httpx is required to fetch remote image URLs") from exc

    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        response = await client.get(image_url, headers={"Accept": "image/*"})

    if response.status_code >= 400:
        raise ImageInputError(f"Failed to fetch image URL: HTTP {response.status_code}")

    content = response.content or b""
    if not content:
        raise ImageInputError("Fetched image URL returned empty content")
    if len(content) > MAX_REMOTE_IMAGE_BYTES:
        raise ImageInputError("Fetched image is too large")

    content_type = response.headers.get("content-type", "")
    encoded = base64.b64encode(content).decode("ascii")
    return normalize_inline_image_data({"mimeType": content_type, "data": encoded})
