import hashlib
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Mapping, Optional, Sequence


WEREAD_BASE_URL = "https://weread.qq.com"
WEREAD_COOKIE_DOMAINS = ("weread.qq.com", ".weread.qq.com", "qq.com", ".qq.com")
WEREAD_GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"
WEREAD_SKILL_VERSION = "1.0.4"


class WeReadApiError(RuntimeError):
    pass


class WeReadLoginExpiredError(WeReadApiError):
    pass


class WeReadGatewayUpgradeRequiredError(WeReadApiError):
    pass


@dataclass(frozen=True)
class CookieRefreshResult:
    cookies: Dict[str, str]
    changed_names: List[str]


@dataclass(frozen=True)
class WeReadBook:
    book_id: str
    title: str
    author: str = ""
    cover: str = ""
    book_type: Optional[int] = None
    note_count: int = 0
    review_count: int = 0
    last_read_time: int = 0


@dataclass(frozen=True)
class BookTitleMatch:
    matched: List[WeReadBook]
    missing_titles: List[str]


@dataclass(frozen=True)
class BookMetadata:
    book_id: str
    title: str
    author: str = ""
    translator: str = ""
    publisher: str = ""
    isbn: str = ""
    category: str = ""
    cover: str = ""
    intro: str = ""


@dataclass(frozen=True)
class WeReadProgress:
    progress: int = 0
    reading_minutes: int = 0
    last_read_time: Optional[int] = None
    finish_time: Optional[int] = None


@dataclass(frozen=True)
class WeReadHighlight:
    book_id: str
    note_id: str
    chapter: str
    position: str
    highlight_text: str
    personal_note: str = ""
    note_type: str = "划线"
    create_time: Optional[int] = None


@dataclass(frozen=True)
class WeReadShelfItem:
    metadata: BookMetadata
    progress: WeReadProgress
    finish_reading: bool = False


@dataclass(frozen=True)
class WeReadReadData:
    base_time: int = 0
    total_read_seconds: int = 0
    read_days: int = 0
    daily_read_seconds: Mapping[str, int] = field(default_factory=dict)


def cookies_from_playwright_state(raw_state: str) -> Dict[str, str]:
    payload = json.loads(raw_state)
    cookies = payload.get("cookies", [])
    result: Dict[str, str] = {}

    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            continue
        domain = str(cookie.get("domain", ""))
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", ""))
        if not name or not value:
            continue
        if _is_weread_cookie_domain(domain):
            result[name] = value

    return result


def cookie_header(cookies: Mapping[str, str]) -> str:
    return "; ".join(f"{name}={cookies[name]}" for name in sorted(cookies))


def refresh_cookies_from_set_cookie_headers(
    cookies: Mapping[str, str], set_cookie_headers: Iterable[str]
) -> CookieRefreshResult:
    refreshed = dict(cookies)
    changed_names: List[str] = []

    for raw_header in set_cookie_headers:
        jar = SimpleCookie()
        jar.load(raw_header)
        for name, morsel in jar.items():
            if not name.startswith("wr_") or not morsel.value:
                continue
            if refreshed.get(name) != morsel.value:
                changed_names.append(name)
            refreshed[name] = morsel.value

    return CookieRefreshResult(cookies=refreshed, changed_names=sorted(set(changed_names)))


def parse_notebooks_payload(payload: object) -> List[WeReadBook]:
    if isinstance(payload, Mapping):
        raw_books = payload.get("books", [])
    elif isinstance(payload, list):
        raw_books = payload
    else:
        raw_books = []

    books: List[WeReadBook] = []
    for item in raw_books:
        if not isinstance(item, Mapping):
            continue
        book = item.get("book") if isinstance(item.get("book"), Mapping) else item
        assert isinstance(book, Mapping)
        book_id = str(book.get("bookId") or item.get("bookId") or "").strip()
        title = str(book.get("title") or item.get("title") or "").strip()
        if not book_id or not title:
            continue
        books.append(
            WeReadBook(
                book_id=book_id,
                title=title,
                author=str(book.get("author") or item.get("author") or "").strip(),
                cover=str(book.get("cover") or item.get("cover") or "").strip(),
                book_type=_safe_int(book.get("type") or item.get("bookType")),
                note_count=_safe_int(item.get("noteCount") or book.get("noteCount")),
                review_count=_safe_int(item.get("reviewCount") or book.get("reviewCount")),
                last_read_time=_safe_int(item.get("sort") or item.get("lastReadTime")),
            )
        )

    return books


def parse_book_metadata(payload: Mapping[str, object]) -> BookMetadata:
    return BookMetadata(
        book_id=str(payload.get("bookId") or "").strip(),
        title=str(payload.get("title") or "").strip(),
        author=str(payload.get("author") or "").strip(),
        translator=str(payload.get("translator") or "").strip(),
        publisher=str(payload.get("publisher") or "").strip(),
        isbn=str(payload.get("isbn") or "").strip(),
        category=_stringify_category(payload.get("category") or payload.get("categories")),
        cover=str(payload.get("cover") or "").strip(),
        intro=str(payload.get("intro") or "").strip(),
    )


def parse_progress_payload(payload: object) -> WeReadProgress:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("book"), Mapping):
        return WeReadProgress()
    book = payload["book"]
    reading_seconds = _safe_int(book.get("readingTime") or book.get("recordReadingTime"))
    return WeReadProgress(
        progress=_safe_int(book.get("progress")),
        reading_minutes=round(reading_seconds / 60),
        last_read_time=_optional_int(book.get("updateTime")),
        finish_time=_optional_int(book.get("finishTime")),
    )


def parse_read_data_payload(payload: object) -> WeReadReadData:
    if not isinstance(payload, Mapping):
        return WeReadReadData(daily_read_seconds={})

    raw_daily = payload.get("dailyReadTimes")
    if not isinstance(raw_daily, Mapping) or not raw_daily:
        raw_daily = payload.get("readTimes")
    daily_read_seconds = {}
    if isinstance(raw_daily, Mapping):
        for raw_timestamp, raw_seconds in raw_daily.items():
            try:
                reading_date = datetime.fromtimestamp(int(raw_timestamp)).date().isoformat()
            except (TypeError, ValueError, OSError):
                continue
            daily_read_seconds[reading_date] = _safe_int(raw_seconds)

    return WeReadReadData(
        base_time=_safe_int(payload.get("baseTime")),
        total_read_seconds=_safe_int(payload.get("totalReadTime")),
        read_days=_safe_int(payload.get("readDays")),
        daily_read_seconds=daily_read_seconds,
    )


def parse_shelf_payload(payload: object) -> List[WeReadShelfItem]:
    if not isinstance(payload, Mapping):
        return []

    raw_books = payload.get("books", [])
    if not isinstance(raw_books, list):
        return []

    progress_by_book_id = {}
    raw_progress = payload.get("bookProgress", [])
    if isinstance(raw_progress, list):
        for item in raw_progress:
            if not isinstance(item, Mapping):
                continue
            book_id = str(item.get("bookId") or "").strip()
            if book_id:
                progress_by_book_id[book_id] = item

    items: List[WeReadShelfItem] = []
    for raw_book in raw_books:
        if not isinstance(raw_book, Mapping):
            continue
        metadata = parse_book_metadata(raw_book)
        if not metadata.book_id or not metadata.title:
            continue
        progress_payload = progress_by_book_id.get(metadata.book_id, {})
        progress = _parse_shelf_progress(progress_payload, raw_book)
        items.append(
            WeReadShelfItem(
                metadata=metadata,
                progress=progress,
                finish_reading=_safe_int(raw_book.get("finishReading")) == 1,
            )
        )

    return items


def parse_bookmarks_payload(payload: object) -> List[WeReadHighlight]:
    if not isinstance(payload, Mapping):
        return []
    updated = payload.get("updated", [])
    if not isinstance(updated, list):
        return []
    chapter_by_uid = _chapter_title_by_uid(payload.get("chapters"))

    highlights: List[WeReadHighlight] = []
    for item in updated:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("markText") or "").strip()
        if not text:
            continue
        highlights.append(
            WeReadHighlight(
                book_id=str(item.get("bookId") or "").strip(),
                note_id=str(item.get("bookmarkId") or "").strip(),
                chapter=str(
                    item.get("chapterName")
                    or chapter_by_uid.get(str(item.get("chapterUid") or "").strip())
                    or ""
                ).strip(),
                position=str(item.get("range") or "").strip(),
                highlight_text=text,
                create_time=_optional_int(item.get("createTime")),
            )
        )
    return highlights


def parse_reviews_payload(payload: object) -> List[WeReadHighlight]:
    if not isinstance(payload, Mapping):
        return []
    raw_reviews = payload.get("reviews", [])
    if not isinstance(raw_reviews, list):
        return []

    notes: List[WeReadHighlight] = []
    for item in raw_reviews:
        review = _extract_review_payload(item)
        if not review:
            continue
        content = _clean_review_text(review.get("content") or review.get("htmlContent"))
        if not content:
            continue
        raw_review_id = str(
            review.get("reviewId")
            or (item.get("reviewId") if isinstance(item, Mapping) else "")
            or ""
        ).strip()
        note_id = f"review:{raw_review_id}" if raw_review_id else _fallback_review_note_id(review, content)
        source_text = _clean_review_text(
            review.get("contextAbstract")
            or review.get("abstract")
            or review.get("title")
        )
        if source_text == content:
            source_text = ""
        notes.append(
            WeReadHighlight(
                book_id=str(review.get("bookId") or "").strip(),
                note_id=note_id,
                chapter=str(review.get("chapterName") or review.get("chapterTitle") or "").strip(),
                position=str(review.get("range") or review.get("chapterUid") or "").strip(),
                highlight_text=source_text,
                personal_note=content,
                note_type=_review_note_type(review),
                create_time=_optional_int(review.get("createTime")),
            )
        )

    return notes


def match_books_by_titles(
    books: Sequence[WeReadBook], requested_titles: Iterable[str]
) -> BookTitleMatch:
    matched: List[WeReadBook] = []
    missing: List[str] = []
    used_book_ids = set()

    for requested_title in requested_titles:
        requested = requested_title.strip()
        if not requested:
            continue
        requested_key = _normalize_title(requested)
        match = _find_title_match(books, requested_key, used_book_ids)
        if match is None:
            missing.append(requested)
            continue
        matched.append(match)
        used_book_ids.add(match.book_id)

    return BookTitleMatch(matched=matched, missing_titles=missing)


def count_highlights(payload: object) -> int:
    if not isinstance(payload, Mapping):
        return 0
    updated = payload.get("updated", [])
    return len(updated) if isinstance(updated, list) else 0


def count_reviews(payload: object) -> int:
    if not isinstance(payload, Mapping):
        return 0
    reviews = payload.get("reviews", [])
    return len(reviews) if isinstance(reviews, list) else 0


def progress_percent(payload: object) -> Optional[int]:
    if not isinstance(payload, Mapping):
        return None
    book = payload.get("book")
    if not isinstance(book, Mapping):
        return None
    progress = book.get("progress")
    return _safe_int(progress) if progress is not None else None


class WeReadClient:
    def __init__(self, cookies: Mapping[str, str], on_cookies_refreshed=None):
        if not cookies:
            raise WeReadApiError("WeRead cookies are missing")
        self._cookies = dict(cookies)
        self._on_cookies_refreshed = on_cookies_refreshed

    def get_notebooks(self) -> List[WeReadBook]:
        payload = self._get_json("/api/user/notebook")
        return parse_notebooks_payload(payload)

    def get_shelf(self) -> List[WeReadShelfItem]:
        payload = self._get_json("/web/shelf/sync", {"synckey": "0", "lectureSynckey": "0"})
        return parse_shelf_payload(payload)

    def get_book(self, book_id: str) -> object:
        return self._get_json("/web/book/info", {"bookId": book_id})

    def get_progress(self, book_id: str) -> object:
        return self._get_json("/web/book/getProgress", {"bookId": book_id})

    def get_bookmarks(self, book_id: str) -> object:
        return self._get_json("/web/book/bookmarklist", {"bookId": book_id})

    def get_reviews(self, book_id: str) -> object:
        return self._get_json(
            "/web/review/list",
            {"bookId": book_id, "listType": "11", "mine": "1", "synckey": "0"},
        )

    def get_read_data_detail(self, mode: str = "monthly", base_time: int = 0) -> WeReadReadData:
        payload = self._get_json("/web/readdata/detail", {"mode": mode, "baseTime": str(base_time)})
        return parse_read_data_payload(payload)

    def _get_json(self, path: str, params: Optional[Mapping[str, str]] = None) -> object:
        url = _build_url(path, params)
        for attempt in range(2):
            body = self._read_url_with_retry(url)

            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                raise WeReadApiError("WeRead response is not valid JSON") from exc
            try:
                _raise_for_weread_error(payload)
            except WeReadLoginExpiredError:
                if attempt == 0 and self.refresh_login_cookies().changed_names:
                    continue
                raise
            return payload

        raise WeReadApiError("WeRead request failed after login refresh")

    def _read_url_with_retry(self, url: str, attempts: int = 2) -> str:
        last_error: Optional[BaseException] = None
        for _ in range(max(attempts, 1)):
            request = urllib.request.Request(url, headers=self._headers(), method="GET")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                raise WeReadApiError(f"WeRead request failed with HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
            except (TimeoutError, socket.timeout) as exc:
                last_error = exc
        assert last_error is not None
        reason = last_error.reason if isinstance(last_error, urllib.error.URLError) else str(last_error)
        raise WeReadApiError(f"WeRead request failed: {reason}") from last_error

    def refresh_login_cookies(self) -> CookieRefreshResult:
        request = urllib.request.Request(WEREAD_BASE_URL + "/", headers=self._headers(), method="HEAD")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                set_cookie_headers = response.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as exc:
            raise WeReadApiError(f"WeRead cookie refresh failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise WeReadApiError(f"WeRead cookie refresh failed: {exc.reason}") from exc

        result = refresh_cookies_from_set_cookie_headers(self._cookies, set_cookie_headers)
        if result.changed_names:
            self._cookies = result.cookies
            if self._on_cookies_refreshed:
                self._on_cookies_refreshed(dict(self._cookies), result.changed_names)
        return result

    def _headers(self) -> Dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cookie": cookie_header(self._cookies),
        }


class WeReadGatewayClient:
    def __init__(self, api_key: str, skill_version: str = WEREAD_SKILL_VERSION):
        api_key = str(api_key or "").strip()
        if not api_key:
            raise WeReadApiError("WeRead API key is missing")
        self._api_key = api_key
        self._skill_version = skill_version

    def get_notebooks(self) -> List[WeReadBook]:
        books = []
        last_sort = None
        for _ in range(100):
            params = {"count": 200}
            if last_sort is not None:
                params["lastSort"] = last_sort
            payload = self._post("/user/notebooks", params)
            page_books = payload.get("books") if isinstance(payload, Mapping) else None
            if not isinstance(page_books, list) or not page_books:
                break
            books.extend(page_books)
            last_item = page_books[-1]
            last_sort = last_item.get("sort") if isinstance(last_item, Mapping) else None
            if not payload.get("hasMore") or last_sort is None:
                break
        return parse_notebooks_payload({"books": books})

    def get_shelf(self) -> List[WeReadShelfItem]:
        return parse_shelf_payload(self._post("/shelf/sync"))

    def get_book(self, book_id: str) -> object:
        return self._post("/book/info", {"bookId": book_id})

    def get_progress(self, book_id: str) -> object:
        return self._post("/book/getprogress", {"bookId": book_id})

    def get_bookmarks(self, book_id: str) -> object:
        return _merge_bookmark_payloads(
            [
                self._post("/book/bookmarklist", {"bookId": book_id, "type": 0}),
                self._post("/book/bookmarklist", {"bookId": book_id, "type": 1}),
            ]
        )

    def get_reviews(self, book_id: str) -> object:
        reviews = []
        synckey = None
        for _ in range(100):
            params = {"bookid": book_id, "count": 200}
            if synckey is not None:
                params["synckey"] = synckey
            payload = self._post("/review/list/mine", params)
            page_reviews = payload.get("reviews") if isinstance(payload, Mapping) else None
            if isinstance(page_reviews, list):
                reviews.extend(page_reviews)
            synckey = payload.get("synckey") if isinstance(payload, Mapping) else None
            if not isinstance(payload, Mapping) or not payload.get("hasMore") or synckey is None:
                break
        return {"reviews": reviews}

    def get_read_data_detail(self, mode: str = "monthly", base_time: int = 0) -> WeReadReadData:
        return parse_read_data_payload(
            self._post("/readdata/detail", {"mode": mode, "baseTime": base_time})
        )

    def _post(self, api_name: str, params: Optional[Mapping[str, object]] = None) -> object:
        body = {"api_name": api_name, "skill_version": self._skill_version}
        if params:
            body.update(dict(params))
        request = urllib.request.Request(
            WEREAD_GATEWAY_URL,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        response_body = self._read_request_with_retry(request)
        try:
            payload = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise WeReadApiError("WeRead gateway response is not valid JSON") from exc
        if isinstance(payload, Mapping) and payload.get("upgrade_info"):
            raise WeReadGatewayUpgradeRequiredError("WeRead official skill requires an upgrade")
        _raise_for_weread_error(payload)
        return payload

    def _read_request_with_retry(self, request, attempts: int = 2) -> str:
        last_error: Optional[BaseException] = None
        for _ in range(max(attempts, 1)):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                raise WeReadApiError(f"WeRead gateway request failed with HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
            except (TimeoutError, socket.timeout) as exc:
                last_error = exc
        assert last_error is not None
        reason = last_error.reason if isinstance(last_error, urllib.error.URLError) else str(last_error)
        raise WeReadApiError(f"WeRead gateway request failed: {reason}") from last_error


def _build_url(path: str, params: Optional[Mapping[str, str]]) -> str:
    url = WEREAD_BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url


def _raise_for_weread_error(payload: object) -> None:
    if not isinstance(payload, Mapping):
        return
    code = payload.get("errcode")
    if code is None:
        code = payload.get("errCode")
    if code in (None, 0, "0"):
        return
    message = str(payload.get("errmsg") or payload.get("errMsg") or "WeRead returned an error")
    if _safe_int(code) == -2012:
        raise WeReadLoginExpiredError("WeRead login has expired")
    raise WeReadApiError(f"WeRead returned error {code}: {message}")


def _parse_shelf_progress(progress_payload: Mapping[str, object], book_payload: Mapping[str, object]) -> WeReadProgress:
    last_read_time = _optional_int(progress_payload.get("updateTime"))
    if last_read_time is None:
        last_read_time = _optional_int(book_payload.get("readUpdateTime") or book_payload.get("updateTime"))
    return WeReadProgress(
        progress=_safe_int(progress_payload.get("progress")),
        reading_minutes=round(_safe_int(progress_payload.get("readingTime")) / 60),
        last_read_time=last_read_time,
        finish_time=None,
    )


def _find_title_match(
    books: Sequence[WeReadBook], requested_key: str, used_book_ids: set
) -> Optional[WeReadBook]:
    candidates = [
        book
        for book in books
        if book.book_id not in used_book_ids
        and (
            _normalize_title(book.title) == requested_key
            or requested_key in _normalize_title(book.title)
            or _normalize_title(book.title) in requested_key
        )
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda book: (book.note_count, book.review_count), reverse=True)[0]


def _normalize_title(title: str) -> str:
    return "".join(title.split()).lower()


def _is_weread_cookie_domain(domain: str) -> bool:
    normalized = domain.lstrip(".")
    return any(normalized == allowed.lstrip(".") for allowed in WEREAD_COOKIE_DOMAINS)


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _optional_int(value: object) -> Optional[int]:
    if value is None:
        return None
    parsed = _safe_int(value)
    return parsed if parsed else None


def _stringify_category(value: object) -> str:
    if isinstance(value, list):
        return " / ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _merge_bookmark_payloads(payloads: Sequence[object]) -> dict:
    result: Dict[str, object] = {"updated": [], "removed": [], "chapters": []}
    seen_bookmarks = set()
    seen_chapters = set()
    seen_removed = set()

    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        for key in ("book", "synckey"):
            if key not in result and payload.get(key) is not None:
                result[key] = payload.get(key)

        updated = payload.get("updated")
        if isinstance(updated, list):
            for item in updated:
                if not isinstance(item, Mapping):
                    continue
                dedupe_key = str(item.get("bookmarkId") or "").strip()
                if not dedupe_key:
                    dedupe_key = "|".join(
                        str(item.get(field) or "") for field in ("bookId", "chapterUid", "range", "markText")
                    )
                if dedupe_key in seen_bookmarks:
                    continue
                seen_bookmarks.add(dedupe_key)
                result["updated"].append(item)

        removed = payload.get("removed")
        if isinstance(removed, list):
            for item in removed:
                dedupe_key = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if dedupe_key in seen_removed:
                    continue
                seen_removed.add(dedupe_key)
                result["removed"].append(item)

        chapters = payload.get("chapters")
        chapter_values = chapters.values() if isinstance(chapters, Mapping) else chapters
        if isinstance(chapter_values, Iterable) and not isinstance(chapter_values, (str, bytes)):
            for chapter in chapter_values:
                if not isinstance(chapter, Mapping):
                    continue
                chapter_uid = str(chapter.get("chapterUid") or "").strip()
                dedupe_key = chapter_uid or json.dumps(chapter, sort_keys=True, ensure_ascii=False)
                if dedupe_key in seen_chapters:
                    continue
                seen_chapters.add(dedupe_key)
                result["chapters"].append(chapter)

    return result


def _chapter_title_by_uid(value: object) -> Dict[str, str]:
    chapters = value.values() if isinstance(value, Mapping) else value
    if not isinstance(chapters, Iterable) or isinstance(chapters, (str, bytes)):
        return {}
    result: Dict[str, str] = {}
    for chapter in chapters:
        if not isinstance(chapter, Mapping):
            continue
        chapter_uid = str(chapter.get("chapterUid") or "").strip()
        title = str(chapter.get("title") or "").strip()
        if chapter_uid and title:
            result[chapter_uid] = title
    return result


def _extract_review_payload(item: object) -> Mapping[str, object]:
    if not isinstance(item, Mapping):
        return {}
    review = item.get("review")
    if isinstance(review, Mapping):
        nested = review.get("review")
        if isinstance(nested, Mapping):
            return nested
        return review
    return item


def _review_note_type(review: Mapping[str, object]) -> str:
    if review.get("star") is not None or review.get("newRatingLevel") is not None:
        return "书评"
    return "想法"


def _clean_review_text(value: object) -> str:
    return str(value or "").strip()


def _fallback_review_note_id(review: Mapping[str, object], content: str) -> str:
    raw = "|".join(
        [
            str(review.get("bookId") or ""),
            str(review.get("chapterUid") or ""),
            str(review.get("range") or ""),
            content,
        ]
    )
    return "review:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
