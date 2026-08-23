import mimetypes
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Mapping, Optional


class CoverImageError(RuntimeError):
    pass


SUPPORTED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def has_cover_attachment(value) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, Mapping):
            continue
        if not (item.get("file_token") or item.get("token")):
            continue
        item_type = str(item.get("type") or "").lower()
        if item_type in SUPPORTED_IMAGE_CONTENT_TYPES:
            return True
        if _extension_from_url(str(item.get("name") or "")):
            return True
    return False


def extract_cover_url(value) -> str:
    text = str(value or "").strip()
    markdown_match = re.fullmatch(r"\[[^\]]+\]\((https?://[^)]+)\)", text)
    if markdown_match:
        return markdown_match.group(1).strip()
    return text


def cover_attachment_name(
    title: str,
    book_id: str,
    content_type: Optional[str] = None,
    url: Optional[str] = None,
) -> str:
    extension = _extension_from_content_type(content_type) or _extension_from_url(url) or ".jpg"
    safe_book_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(book_id or "").strip()).strip("_")
    if not safe_book_id:
        safe_book_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(title or "cover").strip()).strip("_")
    return f"{safe_book_id or 'cover'}{extension}"


def download_cover_image(url: str, output_dir: Path, title: str, book_id: str) -> Path:
    parsed = urllib.parse.urlparse(extract_cover_url(url))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CoverImageError("cover URL is not a valid http(s) URL")

    request = urllib.request.Request(
        urllib.parse.urlunparse(parsed),
        headers={"User-Agent": "ReadingManager/0.1 (+https://weread.qq.com)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = response.headers.get("Content-Type", "")
            content = response.read()
    except OSError as exc:
        raise CoverImageError(f"failed to download cover: {exc}") from exc

    if not content:
        raise CoverImageError("downloaded cover is empty")

    content_type = infer_image_content_type(content, content_type)
    if not content_type:
        raise CoverImageError("downloaded cover is not a supported image")

    output_dir.mkdir(parents=True, exist_ok=True)
    file_path = output_dir / cover_attachment_name(title, book_id, content_type=content_type, url=url)
    file_path.write_bytes(content)
    return file_path


def infer_image_content_type(content: bytes, declared_content_type: Optional[str] = None) -> str:
    detected = _content_type_from_image_bytes(content)
    if detected:
        return detected
    mime_type = str(declared_content_type or "").split(";", 1)[0].strip().lower()
    if mime_type in SUPPORTED_IMAGE_CONTENT_TYPES:
        return mime_type
    return ""


def _extension_from_content_type(content_type: Optional[str]) -> str:
    if not content_type:
        return ""
    mime_type = str(content_type).split(";", 1)[0].strip().lower()
    if mime_type not in SUPPORTED_IMAGE_CONTENT_TYPES:
        return ""
    extension = mimetypes.guess_extension(mime_type) or ""
    if extension == ".jpe":
        return ".jpg"
    return extension


def _content_type_from_image_bytes(content: bytes) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _extension_from_url(url: Optional[str]) -> str:
    if not url:
        return ""
    suffix = Path(urllib.parse.urlparse(str(url)).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ""
