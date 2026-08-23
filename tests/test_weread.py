import json
import socket
import urllib.error
import unittest
from datetime import datetime
from unittest.mock import patch

from reading_manager.weread import (
    WeReadApiError,
    WeReadClient,
    WeReadGatewayClient,
    WeReadGatewayUpgradeRequiredError,
    cookie_header,
    cookies_from_playwright_state,
    count_highlights,
    count_reviews,
    match_books_by_titles,
    parse_book_metadata,
    parse_bookmarks_payload,
    parse_notebooks_payload,
    parse_progress_payload,
    parse_read_data_payload,
    parse_reviews_payload,
    parse_shelf_payload,
    progress_percent,
    refresh_cookies_from_set_cookie_headers,
    _raise_for_weread_error,
)


class WereadTests(unittest.TestCase):
    def test_extracts_only_weread_cookies_from_playwright_state(self):
        state = {
            "cookies": [
                {"name": "wr_vid", "value": "123", "domain": ".weread.qq.com"},
                {"name": "wr_skey", "value": "secret", "domain": "weread.qq.com"},
                {"name": "other", "value": "ignored", "domain": "example.com"},
                {"name": "empty", "value": "", "domain": ".weread.qq.com"},
            ]
        }

        cookies = cookies_from_playwright_state(json.dumps(state))

        self.assertEqual(cookies, {"wr_vid": "123", "wr_skey": "secret"})

    def test_cookie_header_is_stable(self):
        self.assertEqual(cookie_header({"b": "2", "a": "1"}), "a=1; b=2")

    def test_parses_notebook_payload(self):
        payload = {
            "books": [
                {
                    "book": {
                        "bookId": "book-1",
                        "title": "示例书籍甲",
                        "author": "示例作者",
                        "cover": "https://example.test/cover.jpg",
                        "type": 0,
                    },
                    "noteCount": 12,
                    "reviewCount": 3,
                    "sort": 1710000000,
                }
            ]
        }

        books = parse_notebooks_payload(payload)

        self.assertEqual(books[0].book_id, "book-1")
        self.assertEqual(books[0].title, "示例书籍甲")
        self.assertEqual(books[0].note_count, 12)

    def test_matches_books_by_requested_titles(self):
        books = parse_notebooks_payload(
            {
                "books": [
                    {"book": {"bookId": "1", "title": "示例书籍甲：副标题"}},
                    {"book": {"bookId": "2", "title": "示例书籍乙：副标题"}},
                    {"book": {"bookId": "3", "title": "别的书"}},
                ]
            }
        )

        result = match_books_by_titles(books, ["示例书籍甲", "示例书籍乙"])

        self.assertEqual([book.book_id for book in result.matched], ["1", "2"])
        self.assertEqual(result.missing_titles, [])

    def test_counts_highlights_and_reviews(self):
        self.assertEqual(count_highlights({"updated": [{"id": 1}, {"id": 2}]}), 2)
        self.assertEqual(count_reviews({"reviews": [{"id": 1}]}), 1)

    def test_reads_progress_percent(self):
        self.assertEqual(progress_percent({"book": {"progress": 37}}), 37)
        self.assertIsNone(progress_percent({}))

    def test_parses_book_metadata(self):
        metadata = parse_book_metadata(
            {
                "bookId": "book-1",
                "title": "示例书籍甲",
                "author": "示例作者",
                "translator": "译者",
                "publisher": "示例出版社",
                "isbn": "9780000000000",
                "category": "示例分类",
                "cover": "https://example.test/cover.jpg",
                "intro": "简介",
            }
        )

        self.assertEqual(metadata.book_id, "book-1")
        self.assertEqual(metadata.title, "示例书籍甲")
        self.assertEqual(metadata.publisher, "示例出版社")

    def test_parses_progress_payload(self):
        progress = parse_progress_payload(
            {
                "book": {
                    "progress": 99,
                    "readingTime": 3661,
                    "updateTime": 1700000000,
                    "finishTime": 1700000100,
                }
            }
        )

        self.assertEqual(progress.progress, 99)
        self.assertEqual(progress.reading_minutes, 61)
        self.assertEqual(progress.last_read_time, 1700000000)
        self.assertEqual(progress.finish_time, 1700000100)

    def test_parses_official_progress_payload_reading_time(self):
        progress = parse_progress_payload(
            {
                "book": {
                    "progress": 45,
                    "recordReadingTime": 3600,
                    "updateTime": 1700000000,
                }
            }
        )

        self.assertEqual(progress.progress, 45)
        self.assertEqual(progress.reading_minutes, 60)

    def test_parses_read_data_payload_units_as_seconds(self):
        data = parse_read_data_payload(
            {
                "baseTime": 1893427200,
                "totalReadTime": 1500,
                "readDays": 1,
                "readTimes": {
                    "1893513600": 1500,
                    "1893600000": 0,
                },
            }
        )

        self.assertEqual(data.total_read_seconds, 1500)
        self.assertEqual(data.read_days, 1)
        expected_day = datetime.fromtimestamp(1893513600).date().isoformat()
        self.assertEqual(data.daily_read_seconds[expected_day], 1500)

    def test_parses_shelf_payload_with_progress_and_finished_flag(self):
        items = parse_shelf_payload(
            {
                "books": [
                    {
                        "bookId": "book-1",
                        "title": "读完的书",
                        "author": "作者",
                        "cover": "https://example.test/cover.jpg",
                        "finishReading": 1,
                        "finished": 1,
                    },
                    {
                        "bookId": "book-2",
                        "title": "普通书",
                        "finished": 1,
                    },
                ],
                "bookProgress": [
                    {
                        "bookId": "book-1",
                        "progress": 100,
                        "readingTime": 3661,
                        "updateTime": 1700000000,
                    }
                ],
            }
        )

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].metadata.book_id, "book-1")
        self.assertTrue(items[0].finish_reading)
        self.assertEqual(items[0].progress.progress, 100)
        self.assertEqual(items[0].progress.reading_minutes, 61)
        self.assertFalse(items[1].finish_reading)

    def test_parses_highlights_without_losing_full_text(self):
        highlights = parse_bookmarks_payload(
            {
                "updated": [
                    {
                        "bookmarkId": "bookmark-1",
                        "bookId": "book-1",
                        "chapterName": "第一章",
                        "range": "1-2",
                        "markText": "完整划线原文",
                        "createTime": 1700000000,
                    }
                ]
            }
        )

        self.assertEqual(highlights[0].note_id, "bookmark-1")
        self.assertEqual(highlights[0].highlight_text, "完整划线原文")
        self.assertEqual(highlights[0].chapter, "第一章")

    def test_parses_official_highlight_chapter_uid(self):
        highlights = parse_bookmarks_payload(
            {
                "chapters": [{"chapterUid": 123, "title": "官方章节"}],
                "updated": [
                    {
                        "bookmarkId": "bookmark-1",
                        "bookId": "book-1",
                        "chapterUid": 123,
                        "range": "1-2",
                        "markText": "完整划线原文",
                    }
                ],
            }
        )

        self.assertEqual(highlights[0].chapter, "官方章节")

    def test_parses_personal_reviews_as_notes(self):
        notes = parse_reviews_payload(
            {
                "reviews": [
                    {
                        "reviewId": "review-1",
                        "review": {
                            "reviewId": "review-1",
                            "bookId": "book-1",
                            "chapterName": "第一章",
                            "range": "1-2",
                            "contextAbstract": "关联的原文",
                            "content": "我的想法",
                            "createTime": 1700000000,
                        },
                    },
                    {
                        "reviewId": "review-2",
                        "review": {
                            "reviewId": "review-2",
                            "bookId": "book-1",
                            "content": "整本书书评",
                            "star": 100,
                        },
                    },
                ]
            }
        )

        self.assertEqual(notes[0].note_id, "review:review-1")
        self.assertEqual(notes[0].note_type, "想法")
        self.assertEqual(notes[0].highlight_text, "关联的原文")
        self.assertEqual(notes[0].personal_note, "我的想法")
        self.assertEqual(notes[0].chapter, "第一章")
        self.assertEqual(notes[0].create_time, 1700000000)
        self.assertEqual(notes[1].note_type, "书评")
        self.assertEqual(notes[1].personal_note, "整本书书评")

    def test_detects_login_expired_with_camel_case_error(self):
        with self.assertRaisesRegex(WeReadApiError, "login has expired"):
            _raise_for_weread_error({"errCode": -2012, "errMsg": "登录超时"})

    def test_detects_login_expired_with_string_error_code(self):
        with self.assertRaisesRegex(WeReadApiError, "login has expired"):
            _raise_for_weread_error({"errcode": "-2012", "errmsg": "登录超时"})

    def test_detects_non_login_weread_error(self):
        with self.assertRaisesRegex(WeReadApiError, "WeRead returned error 123"):
            _raise_for_weread_error({"errCode": 123, "errMsg": "接口错误"})

    def test_refreshes_only_weread_cookies_from_set_cookie_headers(self):
        skey_cookie = "wr_" + "skey"
        refreshed = refresh_cookies_from_set_cookie_headers(
            {"wr_vid": "old-vid", "wr_skey": "old-key"},
            [
                f"{skey_cookie}=new-key; Path=/; Secure; HttpOnly",
                "other_cookie=ignored; Path=/",
                "wr_gid=123; Path=/",
            ],
        )

        self.assertEqual(refreshed.changed_names, ["wr_gid", "wr_skey"])
        self.assertEqual(
            refreshed.cookies,
            {"wr_vid": "old-vid", "wr_skey": "new-key", "wr_gid": "123"},
        )

    @patch("reading_manager.weread.urllib.request.urlopen")
    def test_client_refreshes_cookies_and_retries_once_when_login_expired(self, urlopen):
        requests = []
        saved = []
        skey_cookie = "wr_" + "skey"

        def fake_urlopen(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                return _FakeResponse({"errCode": -2012, "errMsg": "登录超时"})
            if len(requests) == 2:
                return _FakeResponse(
                    {},
                    set_cookie_headers=[f"{skey_cookie}=new-key; Path=/; Secure; HttpOnly"],
                )
            return _FakeResponse({"books": [{"book": {"bookId": "1", "title": "示例书籍甲"}}]})

        urlopen.side_effect = fake_urlopen
        client = WeReadClient(
            {"wr_vid": "vid", "wr_skey": "old-key"},
            on_cookies_refreshed=lambda cookies, changed_names: saved.append((cookies, changed_names)),
        )

        books = client.get_notebooks()

        self.assertEqual(books[0].book_id, "1")
        self.assertEqual(saved, [({"wr_vid": "vid", "wr_skey": "new-key"}, ["wr_skey"])])
        self.assertIn(f"{skey_cookie}=new-key", requests[2].headers["Cookie"])

    @patch("reading_manager.weread.urllib.request.urlopen")
    def test_client_retries_transient_network_error_once(self, urlopen):
        urlopen.side_effect = [
            urllib.error.URLError(ConnectionResetError(54, "Connection reset by peer")),
            _FakeResponse({"books": [{"book": {"bookId": "1", "title": "示例书籍甲"}}]}),
        ]
        client = WeReadClient({"wr_vid": "vid", "wr_skey": "key"})

        books = client.get_notebooks()

        self.assertEqual(books[0].title, "示例书籍甲")
        self.assertEqual(urlopen.call_count, 2)

    @patch("reading_manager.weread.urllib.request.urlopen")
    def test_client_retries_socket_timeout_once(self, urlopen):
        urlopen.side_effect = [
            socket.timeout("The read operation timed out"),
            _FakeResponse({"books": [{"book": {"bookId": "1", "title": "示例书籍甲"}}]}),
        ]
        client = WeReadClient({"wr_vid": "vid", "wr_skey": "key"})

        books = client.get_notebooks()

        self.assertEqual(books[0].title, "示例书籍甲")
        self.assertEqual(urlopen.call_count, 2)

    @patch("reading_manager.weread.urllib.request.urlopen")
    def test_gateway_client_sends_flattened_official_request(self, urlopen):
        captured_bodies = []

        def fake_urlopen(request, timeout):
            captured_bodies.append(json.loads(request.data.decode("utf-8")))
            self.assertEqual(request.get_header("Authorization"), "Bearer api-key")
            return _FakeResponse({"errcode": 0, "bookId": "book-1", "title": "示例书籍甲"})

        urlopen.side_effect = fake_urlopen
        client = WeReadGatewayClient("api-key")

        payload = client.get_book("book-1")

        self.assertEqual(payload["title"], "示例书籍甲")
        self.assertEqual(captured_bodies[0]["api_name"], "/book/info")
        self.assertEqual(captured_bodies[0]["skill_version"], "1.0.4")
        self.assertEqual(captured_bodies[0]["bookId"], "book-1")
        self.assertNotIn("params", captured_bodies[0])

    @patch("reading_manager.weread.urllib.request.urlopen")
    def test_gateway_client_paginates_notebooks(self, urlopen):
        captured_bodies = []

        def fake_urlopen(request, timeout):
            body = json.loads(request.data.decode("utf-8"))
            captured_bodies.append(body)
            if len(captured_bodies) == 1:
                return _FakeResponse(
                    {
                        "errcode": 0,
                        "hasMore": 1,
                        "books": [
                            {
                                "book": {"bookId": "1", "title": "第一本"},
                                "sort": 100,
                            }
                        ],
                    }
                )
            return _FakeResponse(
                {
                    "errcode": 0,
                    "hasMore": 0,
                    "books": [
                        {
                            "book": {"bookId": "2", "title": "第二本"},
                            "sort": 90,
                        }
                    ],
                }
            )

        urlopen.side_effect = fake_urlopen
        client = WeReadGatewayClient("api-key")

        books = client.get_notebooks()

        self.assertEqual([book.title for book in books], ["第一本", "第二本"])
        self.assertNotIn("lastSort", captured_bodies[0])
        self.assertEqual(captured_bodies[1]["lastSort"], 100)

    @patch("reading_manager.weread.urllib.request.urlopen")
    def test_gateway_client_detects_skill_upgrade_message(self, urlopen):
        urlopen.return_value = _FakeResponse({"errcode": 0, "upgrade_info": {"version": "next"}})
        client = WeReadGatewayClient("api-key")

        with self.assertRaises(WeReadGatewayUpgradeRequiredError):
            client.get_shelf()

    @patch("reading_manager.weread.urllib.request.urlopen")
    def test_gateway_reviews_use_official_lowercase_bookid(self, urlopen):
        captured_bodies = []

        def fake_urlopen(request, timeout):
            captured_bodies.append(json.loads(request.data.decode("utf-8")))
            return _FakeResponse({"errcode": 0, "hasMore": 0, "reviews": []})

        urlopen.side_effect = fake_urlopen
        client = WeReadGatewayClient("api-key")

        client.get_reviews("book-1")

        self.assertEqual(captured_bodies[0]["api_name"], "/review/list/mine")
        self.assertEqual(captured_bodies[0]["bookid"], "book-1")
        self.assertNotIn("bookId", captured_bodies[0])

    @patch("reading_manager.weread.urllib.request.urlopen")
    def test_gateway_bookmarks_merge_official_type_zero_and_one(self, urlopen):
        captured_bodies = []

        def fake_urlopen(request, timeout):
            body = json.loads(request.data.decode("utf-8"))
            captured_bodies.append(body)
            if body.get("type") == 0:
                return _FakeResponse(
                    {
                        "errcode": 0,
                        "updated": [
                            {
                                "bookmarkId": "bookmark-0",
                                "bookId": "book-1",
                                "chapterUid": 1,
                                "markText": "type zero text",
                            }
                        ],
                        "chapters": [{"chapterUid": 1, "title": "第一章"}],
                    }
                )
            return _FakeResponse(
                {
                    "errcode": 0,
                    "updated": [
                        {
                            "bookmarkId": "bookmark-1",
                            "bookId": "book-1",
                            "chapterUid": 2,
                            "markText": "type one text",
                        }
                    ],
                    "chapters": [{"chapterUid": 2, "title": "第二章"}],
                }
            )

        urlopen.side_effect = fake_urlopen
        client = WeReadGatewayClient("api-key")

        highlights = parse_bookmarks_payload(client.get_bookmarks("book-1"))

        self.assertEqual([body["type"] for body in captured_bodies], [0, 1])
        self.assertEqual([highlight.highlight_text for highlight in highlights], ["type zero text", "type one text"])
        self.assertEqual([highlight.chapter for highlight in highlights], ["第一章", "第二章"])

    @patch("reading_manager.weread.urllib.request.urlopen")
    def test_gateway_read_data_uses_official_readdata_detail(self, urlopen):
        captured_bodies = []

        def fake_urlopen(request, timeout):
            captured_bodies.append(json.loads(request.data.decode("utf-8")))
            return _FakeResponse(
                {
                    "errcode": 0,
                    "baseTime": 1893427200,
                    "totalReadTime": 1500,
                    "readDays": 1,
                    "readTimes": {"1893513600": 1500},
                }
            )

        urlopen.side_effect = fake_urlopen
        client = WeReadGatewayClient("api-key")

        data = client.get_read_data_detail(mode="monthly", base_time=1893427200)

        self.assertEqual(captured_bodies[0]["api_name"], "/readdata/detail")
        self.assertEqual(captured_bodies[0]["mode"], "monthly")
        self.assertEqual(captured_bodies[0]["baseTime"], 1893427200)
        expected_day = datetime.fromtimestamp(1893513600).date().isoformat()
        self.assertEqual(data.daily_read_seconds[expected_day], 1500)


class _FakeHeaders:
    def __init__(self, set_cookie_headers=None):
        self._set_cookie_headers = set_cookie_headers or []

    def get_all(self, name):
        return self._set_cookie_headers if name.lower() == "set-cookie" else []


class _FakeResponse:
    def __init__(self, payload, set_cookie_headers=None):
        self._payload = payload
        self.headers = _FakeHeaders(set_cookie_headers)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
