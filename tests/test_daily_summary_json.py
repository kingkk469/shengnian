from __future__ import annotations

import json

import pytest

from daily_summary import _parse_json_object


def test_parse_json_object_accepts_code_fence_and_surrounding_text() -> None:
    raw = """说明如下：
```json
{"review_md":"# 复盘\\n正文","open_questions":[]}
```
"""
    assert _parse_json_object(raw) == {
        "review_md": "# 复盘\n正文",
        "open_questions": [],
    }


def test_parse_json_object_repairs_missing_comma_between_review_fields() -> None:
    raw = '{"review_md":"# 复盘\\n正文"  "open_questions":[]}'
    assert _parse_json_object(raw)["review_md"] == "# 复盘\n正文"


def test_parse_json_object_rejects_non_object_json() -> None:
    with pytest.raises(ValueError, match="顶层不是对象"):
        _parse_json_object('["not", "an", "object"]')


def test_parse_json_object_preserves_markdown_verbatim() -> None:
    markdown = '# 昨日复盘 2026-07-30\n\n## 做了什么\n- 保留“原话”和 **格式**'
    raw = json.dumps({"review_md": markdown, "open_questions": []}, ensure_ascii=False)
    assert _parse_json_object(raw)["review_md"] == markdown
