import json
import unittest

from reading_manager.llm import LLMFatalError, LLMRetryableError
from reading_manager.tagging import (
    TaggingNote,
    TaggingParseError,
    TaggingServiceError,
    build_tagging_prompt,
    parse_tagging_response,
    run_api_tagging,
)


class TaggingTests(unittest.TestCase):
    def test_parse_valid_tagging_response(self):
        payload = """
        {
          "book_tags": [
            {"name": "主题甲", "description": "示例说明", "confidence": 0.82}
          ],
          "note_tags": [
            {"note_key": "note_001", "tags": ["主题甲"], "reason": "示例原因"}
          ]
        }
        """

        result = parse_tagging_response(payload, valid_note_keys={"note_001"})

        self.assertEqual(result.book_tags[0].name, "主题甲")
        self.assertEqual(result.note_tags[0].note_key, "note_001")

    def test_rejects_invalid_json(self):
        with self.assertRaises(TaggingParseError):
            parse_tagging_response("not json", valid_note_keys=set())

    def test_filters_unknown_note_keys(self):
        payload = json.dumps(
            {
                "book_tags": [
                    {"name": "主题甲", "description": "", "confidence": 0.8}
                ],
                "note_tags": [
                    {
                        "note_key": "known",
                        "tags": ["主题甲"],
                        "reason": "",
                    },
                    {
                        "note_key": "missing",
                        "tags": ["主题甲"],
                        "reason": "",
                    },
                ],
            },
            ensure_ascii=False,
        )

        result = parse_tagging_response(payload, valid_note_keys={"known"})

        self.assertEqual([tag.note_key for tag in result.note_tags], ["known"])

    def test_rejects_empty_tag_names(self):
        payload = '{"book_tags": [{"name": "", "description": "", "confidence": 0.5}], "note_tags": []}'

        with self.assertRaises(TaggingParseError):
            parse_tagging_response(payload, valid_note_keys=set())

    def test_requires_exact_root_fields(self):
        payloads = [
            {"book_tags": []},
            {"book_tags": [], "note_tags": [], "extra": True},
            {"book_tags": {}, "note_tags": []},
            {"book_tags": [], "note_tags": {}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(TaggingParseError):
                    parse_tagging_response(
                        json.dumps(payload),
                        valid_note_keys=set(),
                    )

    def test_requires_exact_book_tag_fields_and_confidence_range(self):
        invalid_book_tags = [
            {"name": "x", "description": "", "confidence": 0.5, "extra": True},
            {"name": "x", "description": 1, "confidence": 0.5},
            {"name": "x", "description": "", "confidence": "high"},
            {"name": "x", "description": "", "confidence": -0.1},
            {"name": "x", "description": "", "confidence": 1.1},
        ]
        for book_tag in invalid_book_tags:
            with self.subTest(book_tag=book_tag):
                payload = {"book_tags": [book_tag], "note_tags": []}
                with self.assertRaises(TaggingParseError):
                    parse_tagging_response(
                        json.dumps(payload),
                        valid_note_keys=set(),
                    )

    def test_requires_exact_note_tag_fields_and_one_to_three_tags(self):
        invalid_note_tags = [
            {"note_key": "", "tags": ["x"], "reason": ""},
            {"note_key": "note_001", "tags": [], "reason": ""},
            {"note_key": "note_001", "tags": ["a", "b", "c", "d"], "reason": ""},
            {"note_key": "note_001", "tags": "x", "reason": ""},
            {"note_key": "note_001", "tags": ["x"], "reason": 1},
            {"note_key": "note_001", "tags": ["x"], "reason": "", "extra": True},
        ]
        for note_tag in invalid_note_tags:
            with self.subTest(note_tag=note_tag):
                payload = {
                    "book_tags": [
                        {"name": "x", "description": "", "confidence": 0.5}
                    ],
                    "note_tags": [note_tag],
                }
                with self.assertRaises(TaggingParseError):
                    parse_tagging_response(
                        json.dumps(payload),
                        valid_note_keys={"note_001"},
                    )

    def test_rejects_note_tags_not_declared_in_book_tags(self):
        payload = {
            "book_tags": [
                {"name": "主题甲", "description": "", "confidence": 0.8}
            ],
            "note_tags": [
                {
                    "note_key": "note_001",
                    "tags": ["未声明标签"],
                    "reason": "",
                }
            ],
        }

        with self.assertRaisesRegex(TaggingParseError, "book_tags"):
            parse_tagging_response(
                json.dumps(payload, ensure_ascii=False),
                valid_note_keys={"note_001"},
            )

    def test_rejects_empty_results_when_notes_are_present(self):
        payload = '{"book_tags": [], "note_tags": []}'

        with self.assertRaises(TaggingParseError):
            parse_tagging_response(payload, valid_note_keys={"note_001"})

    def test_build_prompt_contains_note_keys_and_json_contract(self):
        prompt = build_tagging_prompt(
            notes=[
                TaggingNote(
                    note_key="note_001",
                    title="示例书籍甲 / 示例章节",
                    text="示例划线内容",
                )
            ],
            existing_tags=["主题甲"],
        )

        self.assertIn("note_001", prompt)
        self.assertIn("book_tags", prompt)
        self.assertIn("书籍级粗标签", prompt)
        self.assertIn("不要生成章节级", prompt)
        self.assertIn("优先复用", prompt)
        self.assertIn("自然科学", prompt)
        self.assertIn("软件工程", prompt)

    def test_run_api_tagging_retries_retryable_failures(self):
        client = _FakeClient(
            [
                LLMRetryableError("temporary"),
                LLMRetryableError("temporary"),
                _valid_response(),
            ]
        )
        sleeps = []

        result = run_api_tagging(
            prompt="private prompt text",
            valid_note_keys={"note_001"},
            client=client,
            max_attempts=3,
            total_timeout_seconds=140,
            sleeper=sleeps.append,
            clock=lambda: 0,
        )

        self.assertEqual(result.book_tags[0].name, "主题乙")
        self.assertEqual(client.calls, 3)
        self.assertEqual(sleeps, [1, 2])

    def test_run_api_tagging_retries_invalid_business_json(self):
        client = _FakeClient(
            [
                '{"book_tags":[],"note_tags":[]}',
                _valid_response(),
            ]
        )

        result = run_api_tagging(
            prompt="private prompt text",
            valid_note_keys={"note_001"},
            client=client,
            max_attempts=3,
            sleeper=lambda delay: None,
            clock=lambda: 0,
        )

        self.assertEqual(result.note_tags[0].note_key, "note_001")
        self.assertEqual(client.calls, 2)

    def test_run_api_tagging_does_not_retry_fatal_error(self):
        client = _FakeClient([LLMFatalError("unauthorized")])

        with self.assertRaisesRegex(TaggingServiceError, "unauthorized"):
            run_api_tagging(
                prompt="private prompt text",
                valid_note_keys={"note_001"},
                client=client,
                max_attempts=3,
                sleeper=lambda delay: None,
                clock=lambda: 0,
            )

        self.assertEqual(client.calls, 1)

    def test_run_api_tagging_stops_at_total_deadline(self):
        client = _FakeClient([LLMRetryableError("temporary")] * 3)
        times = iter([0, 0, 139])

        with self.assertRaises(TaggingServiceError):
            run_api_tagging(
                prompt="private prompt text",
                valid_note_keys={"note_001"},
                client=client,
                max_attempts=3,
                total_timeout_seconds=140,
                sleeper=lambda delay: None,
                clock=lambda: next(times),
            )

        self.assertEqual(client.calls, 1)

    def test_run_api_tagging_does_not_start_request_past_total_deadline(self):
        client = _FakeClient([LLMRetryableError("temporary")] * 3)
        times = iter([0, 0, 21])

        with self.assertRaises(TaggingServiceError):
            run_api_tagging(
                prompt="private prompt text",
                valid_note_keys={"note_001"},
                client=client,
                max_attempts=3,
                request_timeout_seconds=120,
                total_timeout_seconds=140,
                sleeper=lambda delay: None,
                clock=lambda: next(times),
            )

        self.assertEqual(client.calls, 1)

    def test_run_api_tagging_rejects_more_than_three_attempts(self):
        client = _FakeClient([LLMRetryableError("temporary")] * 4)

        with self.assertRaisesRegex(TaggingServiceError, "at most 3"):
            run_api_tagging(
                prompt="private prompt text",
                valid_note_keys={"note_001"},
                client=client,
                max_attempts=4,
                sleeper=lambda delay: None,
                clock=lambda: 0,
            )

        self.assertEqual(client.calls, 0)


class _FakeClient:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def complete_json(self, prompt):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _valid_response():
    return json.dumps(
        {
            "book_tags": [
                {"name": "主题乙", "description": "", "confidence": 0.8}
            ],
            "note_tags": [
                {
                    "note_key": "note_001",
                    "tags": ["主题乙"],
                    "reason": "",
                }
            ],
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":
    unittest.main()
