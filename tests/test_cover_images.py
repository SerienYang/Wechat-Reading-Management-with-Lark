from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reading_manager.cover_images import (
    download_cover_image,
    cover_attachment_name,
    extract_cover_url,
    has_cover_attachment,
    infer_image_content_type,
)


class CoverImageTests(unittest.TestCase):
    def test_cover_attachment_name_uses_book_id_and_jpg_by_default(self):
        self.assertEqual(
            cover_attachment_name(title="示例书籍甲：副标题", book_id="book-cover-a"),
            "book-cover-a.jpg",
        )

    def test_cover_attachment_name_uses_content_type_extension(self):
        self.assertEqual(
            cover_attachment_name(title="示例书籍乙", book_id="book-cover-b", content_type="image/png"),
            "book-cover-b.png",
        )

    def test_infers_jpeg_when_weread_returns_octet_stream(self):
        content = b"\xff\xd8\xff\xe1" + b"jpeg body"

        self.assertEqual(infer_image_content_type(content, "application/octet-stream"), "image/jpeg")

    def test_download_cover_image_names_octet_stream_jpeg_as_jpg(self):
        class FakeResponse:
            headers = {"Content-Type": "application/octet-stream"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b"\xff\xd8\xff\xe0" + b"jpeg body"

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("urllib.request.urlopen", return_value=FakeResponse()):
                path = download_cover_image(
                    "https://example.test/cover-without-extension",
                    Path(temp_dir),
                    title="示例书籍戊",
                    book_id="book-cover-c",
                )

        self.assertEqual(path.name, "book-cover-c.jpg")

    def test_has_cover_attachment_detects_existing_files(self):
        self.assertTrue(has_cover_attachment([{"file_token": "tok", "name": "cover.jpg"}]))
        self.assertFalse(has_cover_attachment([]))
        self.assertFalse(has_cover_attachment(None))

    def test_has_cover_attachment_ignores_non_image_file_names(self):
        self.assertFalse(has_cover_attachment([{"file_token": "tok", "name": "parsecover"}]))
        self.assertFalse(has_cover_attachment([{"file_token": "tok", "name": "cover.bin"}]))

    def test_extract_cover_url_from_markdown_link_value(self):
        self.assertEqual(
            extract_cover_url("[https://example.test/cover.jpg](https://example.test/cover.jpg)"),
            "https://example.test/cover.jpg",
        )


if __name__ == "__main__":
    unittest.main()
