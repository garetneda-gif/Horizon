from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import json
import pytest
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.models import ContentItem, SourceType


def load_email_digest_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "send-email-digest.py"
    spec = importlib.util.spec_from_file_location("send_email_digest", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_item(item_id: str = "item-1") -> ContentItem:
    return ContentItem(
        id=item_id,
        source_type=SourceType.HACKERNEWS,
        title="Useful HN Story",
        url="https://example.com/story",
        content="A useful story body",
        author="tester",
        published_at=datetime.now(timezone.utc),
        metadata={"score": 120},
    )


def make_valid_digest() -> str:
    lines = ["# Horizon", "", "## 今日重点", "", "- 安全测试", ""]
    for index in range(1, 9):
        lines.extend([
            f"### Article {index}",
            "",
            "为什么值得看：测试。",
            "",
            f"[阅读原文](https://example.com/story-{index})",
            "",
        ])
    lines.append("本邮件由 Horizon + Codex 自动化发送。共抓取 8 条，来源统计：rss=8。")
    return "\n".join(lines)


def make_candidate_payload() -> dict:
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "items": [
            {"url": f"https://example.com/story-{index}"}
            for index in range(1, 9)
        ],
    }


def write_trusted_candidates(module, path: Path) -> None:
    payload = make_candidate_payload()
    path.write_text(
        json.dumps(module._with_candidate_checksum(payload)),
        encoding="utf-8",
    )


def test_empty_fetch_summary_helper_is_not_available() -> None:
    module = load_email_digest_module()

    assert not hasattr(module, "_empty_fetch_summary")


def test_fallback_summary_keeps_candidate_articles() -> None:
    module = load_email_digest_module()

    summary = module._fallback_summary(
        [make_item()],
        source_counts={"hackernews": 1},
        fetched=1,
        error=RuntimeError("Connection error."),
    )

    assert "模型摘要生成失败" in summary
    assert "[Useful HN Story](https://example.com/story)" in summary
    assert "Connection error." in summary


def test_item_json_keeps_codex_candidate_fields() -> None:
    module = load_email_digest_module()

    item = make_item()
    payload = module._item_json(1, item)

    assert payload["index"] == 1
    assert payload["title"] == "Useful HN Story"
    assert payload["url"] == "https://example.com/story"
    assert payload["source"] == "hackernews"
    assert payload["metadata"]["score"] == 120


def test_fetch_with_empty_retry_expands_window() -> None:
    module = load_email_digest_module()

    class FakeService:
        def __init__(self) -> None:
            self.calls: list[int] = []
            self.current_items: list[ContentItem] = []

        async def fetch_items(self, hours: int, run_id: str):
            self.calls.append(hours)
            self.current_items = [] if hours == module.DEFAULT_FETCH_HOURS else [make_item()]
            return {
                "run_id": run_id,
                "fetched": len(self.current_items),
                "source_counts": {"hackernews": len(self.current_items)},
            }

        def _load_stage_items(self, **kwargs):
            return self.current_items, object()

    service = FakeService()
    fetch_result, items, source_counts, used_hours = asyncio.run(
        module._fetch_with_empty_retry(service)
    )

    assert service.calls == [module.DEFAULT_FETCH_HOURS, module.EMPTY_FETCH_FALLBACK_HOURS]
    assert fetch_result["fetched"] == 1
    assert len(items) == 1
    assert source_counts == {"hackernews": 1}
    assert used_hours == module.EMPTY_FETCH_FALLBACK_HOURS


def test_send_existing_summary_reuses_email_sender(tmp_path, monkeypatch) -> None:
    module = load_email_digest_module()
    summary_file = tmp_path / "summary.md"
    summary = make_valid_digest()
    summary_file.write_text(summary, encoding="utf-8")
    candidates_file = tmp_path / "candidates.json"
    write_trusted_candidates(module, candidates_file)
    sent = {}

    class FakeStorage:
        def load_config(self):
            return SimpleNamespace(email=SimpleNamespace(enabled=True))

        def load_subscribers(self):
            return ["rjk2021@163.com"]

        def save_daily_summary(self, today: str, summary: str, language: str = "zh"):
            sent["saved"] = (today, summary, language)
            return Path("data/summaries/test.md")

    def fake_send_email(config, subscribers, subject, markdown_body):
        sent["email"] = (config, subscribers, subject, markdown_body)

    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module, "StorageManager", FakeStorage)
    monkeypatch.setattr(module, "TRUSTED_CANDIDATES_PATH", candidates_file)
    monkeypatch.setattr(module, "load_dotenv", lambda path: None)
    monkeypatch.setattr(module, "_send_email", fake_send_email)

    module.send_existing_summary(str(summary_file))

    assert sent["saved"][1] == summary
    assert sent["saved"][2] == "zh"
    assert sent["email"][1] == ["rjk2021@163.com"]
    assert sent["email"][3] == summary


@pytest.mark.parametrize(
    "summary",
    [
        "# Horizon\n\n<script>alert(1)</script>",
        "# Horizon\n\n![tracking](https://tracker.example/pixel.png)",
        "# Horizon\n\n![tracking][pixel]\n\n[pixel]: https://tracker.example/pixel.png",
        "# Horizon\n\nunsafe\x00text",
        "# Horizon\n\n-----BEGIN PRIVATE KEY-----",
    ],
)
def test_summary_validation_rejects_active_content(summary: str) -> None:
    module = load_email_digest_module()

    with pytest.raises(ValueError):
        module._validate_summary_markdown(summary)


def test_safe_html_renderer_keeps_only_safe_links() -> None:
    module = load_email_digest_module()

    rendered = module._render_safe_html(
        "# Horizon\n\n[Safe](https://example.com/story?a=1&b=2)"
    )

    assert '<a href="https://example.com/story?a=1&amp;b=2">Safe</a>' in rendered
    assert "<script" not in rendered
    assert "<img" not in rendered


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://example.com/path\nBcc:attacker@example.com",
        "https:///missing-host",
        "https://user@example.com/story",
        "https://example.com:invalid/story",
    ],
)
def test_safe_html_renderer_rejects_unsafe_links(url: str) -> None:
    module = load_email_digest_module()

    with pytest.raises(ValueError):
        module._render_safe_html(f"# Horizon\n\n[Unsafe]({url})")


def test_digest_structure_requires_eight_articles() -> None:
    module = load_email_digest_module()

    with pytest.raises(ValueError, match="8 to 12"):
        module._validate_digest_structure(
            "# Horizon\n\n### Only one\n\n[Link](https://example.com)\n\n"
            "本邮件由 Horizon + Codex 自动化发送。",
            {"https://example.com"},
        )


def test_digest_structure_requires_safe_link_in_every_article() -> None:
    module = load_email_digest_module()
    summary = make_valid_digest().replace(
        "[阅读原文](https://example.com/story-8)",
        "没有链接",
    )

    with pytest.raises(ValueError, match="safe web link"):
        module._validate_digest_structure(
            summary,
            {f"https://example.com/story-{index}" for index in range(1, 9)},
        )


def test_digest_structure_rejects_link_outside_candidates() -> None:
    module = load_email_digest_module()
    summary = make_valid_digest().replace(
        "https://example.com/story-8",
        "https://attacker.example/phish",
    )

    with pytest.raises(ValueError, match="not in the trusted candidates"):
        module._validate_digest_structure(
            summary,
            {f"https://example.com/story-{index}" for index in range(1, 9)},
        )


def test_digest_structure_rejects_duplicate_primary_links() -> None:
    module = load_email_digest_module()
    summary = make_valid_digest()
    for index in range(2, 9):
        summary = summary.replace(
            f"https://example.com/story-{index}",
            "https://example.com/story-1",
        )

    with pytest.raises(ValueError, match="unique primary candidate links"):
        module._validate_digest_structure(
            summary,
            {"https://example.com/story-1"},
        )


def test_trusted_candidate_snapshot_rejects_hash_mismatch(tmp_path) -> None:
    module = load_email_digest_module()
    candidates_file = tmp_path / "candidates.json"
    write_trusted_candidates(module, candidates_file)
    payload = json.loads(candidates_file.read_text(encoding="utf-8"))
    payload["items"][0]["url"] = "https://attacker.example/phish"
    candidates_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="hash does not match"):
        module._load_trusted_candidate_urls(candidates_file)


def test_trusted_candidate_snapshot_is_private_and_atomic(tmp_path, monkeypatch) -> None:
    module = load_email_digest_module()
    candidates_file = tmp_path / "candidates.json"
    monkeypatch.setattr(module, "TRUSTED_CANDIDATES_PATH", candidates_file)

    module._write_trusted_candidates(make_candidate_payload())

    assert stat.S_IMODE(candidates_file.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".codex-candidates-*.tmp"))
    assert len(module._load_trusted_candidate_urls(candidates_file)) == 8


def test_concurrent_candidate_writes_leave_valid_snapshot(tmp_path, monkeypatch) -> None:
    module = load_email_digest_module()
    candidates_file = tmp_path / "candidates.json"
    monkeypatch.setattr(module, "TRUSTED_CANDIDATES_PATH", candidates_file)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(module._write_trusted_candidates, [
            make_candidate_payload(),
            make_candidate_payload(),
        ]))

    assert len(results) == 2
    assert len(module._load_trusted_candidate_urls(candidates_file)) == 8
    assert not list(tmp_path.glob(".codex-candidates-*.tmp"))
