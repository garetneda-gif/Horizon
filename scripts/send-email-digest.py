#!/usr/bin/env python3
"""Fetch Horizon sources, generate a compact Chinese digest, and email it."""

from __future__ import annotations

import asyncio
import argparse
import contextlib
import html
import hashlib
import json
import logging
import os
import re
import smtplib
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from openai import AsyncOpenAI

from src.mcp.service import HorizonPipelineService
from src.storage.manager import StorageManager

try:
    import markdown
except ImportError:  # pragma: no cover
    markdown = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "scheduled-email-digest"
RECIPIENT_LIMIT = 14
DEFAULT_FETCH_HOURS = 24
EMPTY_FETCH_FALLBACK_HOURS = 72
AI_ATTEMPTS = 3
MAX_SUMMARY_BYTES = 256_000
CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
RAW_HTML_RE = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
MARKDOWN_REFERENCE_IMAGE_RE = re.compile(r"!\[[^\]]*\]\s*\[[^\]]*\]")
PEM_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
H1_RE = re.compile(r"^#(?!#)\s+\S", re.MULTILINE)
H3_RE = re.compile(r"^###(?!#)\s+\S", re.MULTILINE)
EXPECTED_FOOTER = "本邮件由 Horizon + Codex 自动化发送。"
ALLOWED_HTML_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "em",
    "h1",
    "h2",
    "h3",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "ul",
}
VOID_HTML_TAGS = {"br", "hr"}
TRUSTED_CANDIDATES_PATH = PROJECT_ROOT / "data" / ".codex-candidates-latest.json"
TRUSTED_CANDIDATE_MAX_AGE = timedelta(hours=2)


logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("horizon.email_digest")


def _validate_web_url(value: str) -> str:
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Summary link contains whitespace or control characters.")
    parsed = urlsplit(value)
    try:
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError("Summary link has an invalid hostname or port.") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Summary links must use http or https with a hostname.")
    return value


def _validate_summary_markdown(markdown_body: str) -> None:
    if not markdown_body.strip():
        raise ValueError("Summary is empty.")
    if len(markdown_body.encode("utf-8")) > MAX_SUMMARY_BYTES:
        raise ValueError("Summary is too large.")
    if CONTROL_CHARACTER_RE.search(markdown_body):
        raise ValueError("Summary contains control characters.")
    if RAW_HTML_RE.search(markdown_body):
        raise ValueError("Raw HTML is not allowed in summaries.")
    if MARKDOWN_IMAGE_RE.search(markdown_body) or MARKDOWN_REFERENCE_IMAGE_RE.search(markdown_body):
        raise ValueError("Markdown images are not allowed in summaries.")
    if PEM_PRIVATE_KEY_RE.search(markdown_body):
        raise ValueError("Summary contains private key material.")


class _EmailHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_HTML_TAGS:
            return
        if tag == "a":
            href = next((value for name, value in attrs if name == "href"), None)
            if href is None:
                raise ValueError("Summary link is missing an href.")
            validated_href = _validate_web_url(href)
            self.links.append(validated_href)
            safe_href = html.escape(validated_href, quote=True)
            self.parts.append(f'<a href="{safe_href}">')
            return
        self.parts.append(f"<{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in ALLOWED_HTML_TAGS and tag not in VOID_HTML_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data, quote=False))

    def get_html(self) -> str:
        return "".join(self.parts)


def _sanitize_markdown(markdown_body: str) -> _EmailHTMLSanitizer:
    _validate_summary_markdown(markdown_body)
    if markdown:
        escaped_markdown = html.escape(markdown_body, quote=False)
        rendered = markdown.markdown(escaped_markdown)
    else:
        rendered = f"<pre>{html.escape(markdown_body)}</pre>"

    sanitizer = _EmailHTMLSanitizer()
    sanitizer.feed(rendered)
    sanitizer.close()
    return sanitizer


def _render_safe_html(markdown_body: str) -> str:
    return _sanitize_markdown(markdown_body).get_html()


def _validate_digest_structure(markdown_body: str, allowed_urls: set[str]) -> None:
    _validate_summary_markdown(markdown_body)
    if len(H1_RE.findall(markdown_body)) != 1:
        raise ValueError("Summary must contain exactly one H1 heading.")

    article_sections = re.split(r"(?m)(?=^###(?!#)\s+)", markdown_body)[1:]
    if not 8 <= len(article_sections) <= 12:
        raise ValueError("Summary must contain 8 to 12 H3 article sections.")
    primary_article_urls: list[str] = []
    for section in article_sections:
        section_links = _sanitize_markdown(section).links
        if not section_links:
            raise ValueError("Every article section must contain a safe web link.")
        primary_article_urls.append(section_links[0])
    if len(set(primary_article_urls)) != len(primary_article_urls):
        raise ValueError("Article sections must use unique primary candidate links.")

    summary_links = set(_sanitize_markdown(markdown_body).links)
    if not summary_links <= allowed_urls:
        raise ValueError("Summary contains a link that is not in the trusted candidates.")

    if EXPECTED_FOOTER not in markdown_body:
        raise ValueError("Summary is missing the required automation footer.")
    for forbidden in ("file://", "/Users/"):
        if forbidden in markdown_body:
            raise ValueError("Summary contains forbidden local or credential content.")


def _canonical_candidate_bytes(payload: dict) -> bytes:
    unsigned_payload = dict(payload)
    unsigned_payload.pop("sha256", None)
    return json.dumps(
        unsigned_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _with_candidate_checksum(payload: dict) -> dict:
    checked_payload = dict(payload)
    checked_payload["sha256"] = hashlib.sha256(_canonical_candidate_bytes(payload)).hexdigest()
    return checked_payload


def _write_trusted_candidates(payload: dict) -> dict:
    checked_payload = _with_candidate_checksum(payload)
    TRUSTED_CANDIDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=TRUSTED_CANDIDATES_PATH.parent,
            prefix=".codex-candidates-",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(checked_payload, temp_file, ensure_ascii=False, indent=2, default=str)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, TRUSTED_CANDIDATES_PATH)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return checked_payload


def _load_trusted_candidate_urls(path: Path | None = None) -> set[str]:
    candidate_path = path or TRUSTED_CANDIDATES_PATH
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    expected_sha = payload.get("sha256")
    actual_sha = hashlib.sha256(_canonical_candidate_bytes(payload)).hexdigest()
    if not expected_sha or expected_sha != actual_sha:
        raise ValueError("Trusted candidate snapshot hash does not match.")

    exported_at = datetime.fromisoformat(payload["exported_at"])
    if exported_at.tzinfo is None:
        raise ValueError("Trusted candidate timestamp must include a timezone.")
    age = datetime.now(timezone.utc) - exported_at.astimezone(timezone.utc)
    if age < -timedelta(minutes=5) or age > TRUSTED_CANDIDATE_MAX_AGE:
        raise ValueError("Trusted candidate snapshot is stale or has a future timestamp.")

    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("Trusted candidate snapshot has no items.")
    return {_validate_web_url(str(item["url"])) for item in items}


def _priority(item) -> float:
    meta = item.metadata or {}
    raw_score = meta.get("score") or meta.get("upvotes") or 0
    try:
        score = float(raw_score)
    except Exception:
        score = 0.0
    source_weight = {
        "rss": 30,
        "github": 25,
        "hackernews": 20,
        "reddit": 15,
    }.get(item.source_type.value, 0)
    return source_weight + min(score / 20, 30)


def _item_block(index: int, item) -> str:
    content = (item.content or "").replace("\n", " ").strip()
    if len(content) > 650:
        content = content[:650] + "..."
    meta = item.metadata or {}
    engagement = []
    for key in ("score", "descendants", "subreddit", "repo", "category"):
        if meta.get(key):
            engagement.append(f"{key}={meta.get(key)}")
    return (
        f"[{index}] 标题: {item.title}\n"
        f"来源: {item.source_type.value}; 作者: {item.author or 'Unknown'}; "
        f"{'; '.join(engagement)}\n"
        f"链接: {item.url}\n"
        f"内容: {content}"
    )


def _item_json(index: int, item) -> dict:
    content = (item.content or "").replace("\n", " ").strip()
    if len(content) > 650:
        content = content[:650] + "..."
    return {
        "index": index,
        "title": item.title,
        "url": str(item.url),
        "source": item.source_type.value,
        "author": item.author or "Unknown",
        "content": content,
        "metadata": item.metadata or {},
    }


async def _generate_summary(config, selected, source_counts: dict, fetched: int) -> str:
    if not selected:
        raise ValueError("No candidate articles available for summary generation.")

    blocks = [_item_block(i, item) for i, item in enumerate(selected, 1)]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_prompt = f"""
你是一个技术信息日报编辑。请根据下面抓取到的文章/帖子，写一封中文邮件版日报。
要求：
- 开头先给 4-6 条“今日重点”。
- 然后按条目列出 8-12 篇值得看的文章，每条包含：标题、为什么值得看、链接。
- 不要编造链接，不要加入输入之外的事实。
- 语气直接、简洁，适合发到邮箱阅读。
- Markdown 格式。

抓取统计：{source_counts}
抓取时间：{now}

候选内容：

{chr(10).join(blocks)}
""".strip()

    api_key = os.environ.get(config.ai.api_key_env)
    if not api_key:
        raise RuntimeError(f"Missing AI API key env: {config.ai.api_key_env}")

    client = AsyncOpenAI(api_key=api_key, base_url=config.ai.base_url)
    last_error: Exception | None = None
    for attempt in range(1, AI_ATTEMPTS + 1):
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=config.ai.model,
                    messages=[
                        {
                            "role": "system",
                            "content": "你只根据用户提供的候选内容写中文技术日报，避免编造。",
                        },
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.3,
                    max_tokens=3200,
                ),
                timeout=120,
            )
            break
        except Exception as exc:
            last_error = exc
            logger.warning(
                "AI summary attempt %s/%s failed: %s",
                attempt,
                AI_ATTEMPTS,
                exc,
            )
            if attempt < AI_ATTEMPTS:
                await asyncio.sleep(2 * attempt)
    else:
        raise RuntimeError(f"AI summary failed after {AI_ATTEMPTS} attempts: {last_error}") from last_error

    summary = (response.choices[0].message.content or "").strip()
    return (
        summary
        + "\n\n---\n\n"
        + f"本邮件由 Horizon 发送。共抓取 {fetched} 条，来源统计：{source_counts}。\n"
    )


def _fallback_summary(selected, source_counts: dict, fetched: int, error: Exception) -> str:
    error_text = str(error) or error.__class__.__name__
    lines = [
        "# Horizon 今日文章",
        "",
        "模型摘要生成失败，以下是抓取到的候选文章列表。",
        "",
        f"错误：{error_text}",
        "",
    ]
    for item in selected:
        lines.append(f"- [{item.title}]({item.url}) - {item.source_type.value}")
    lines.extend([
        "",
        "---",
        "",
        f"本邮件由 Horizon 发送。共抓取 {fetched} 条，来源统计：{source_counts}。",
        "",
    ])
    return "\n".join(lines)


async def _fetch_with_empty_retry(service: HorizonPipelineService) -> tuple[dict, list, dict, int]:
    fetch_result = await service.fetch_items(hours=DEFAULT_FETCH_HOURS, run_id=RUN_ID)
    used_hours = DEFAULT_FETCH_HOURS
    items, _ctx = service._load_stage_items(
        run_id=fetch_result["run_id"],
        stage="raw",
        horizon_path=None,
        config_path=None,
    )

    if items:
        return fetch_result, items, fetch_result.get("source_counts", {}), used_hours

    logger.warning(
        "Initial %sh fetch returned no items; retrying with %sh window.",
        DEFAULT_FETCH_HOURS,
        EMPTY_FETCH_FALLBACK_HOURS,
    )
    fetch_result = await service.fetch_items(
        hours=EMPTY_FETCH_FALLBACK_HOURS,
        run_id=f"{RUN_ID}-fallback",
    )
    used_hours = EMPTY_FETCH_FALLBACK_HOURS
    items, _ctx = service._load_stage_items(
        run_id=fetch_result["run_id"],
        stage="raw",
        horizon_path=None,
        config_path=None,
    )
    return fetch_result, items, fetch_result.get("source_counts", {}), used_hours


def _send_email(config, subscribers: list[str], subject: str, markdown_body: str) -> None:
    if not subscribers:
        raise RuntimeError("No subscribers configured in data/subscribers.json")
    if not config.email or not config.email.enabled:
        raise RuntimeError("Email is not enabled in data/config.json")

    email_config = config.email
    password = os.environ.get(email_config.password_env)
    if not password:
        raise RuntimeError(f"Missing email password env: {email_config.password_env}")

    html_content = _render_safe_html(markdown_body)
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.65;color:#222;max-width:760px;margin:0 auto;padding:24px">
{html_content}
<hr style="border:none;border-top:1px solid #eee;margin:32px 0 12px">
<p style="color:#888;font-size:12px">Sent by {email_config.sender_name}</p>
</body></html>"""

    with smtplib.SMTP_SSL(email_config.smtp_server, email_config.smtp_port, timeout=20) as server:
        server.login(email_config.email_address, password)
        for recipient in subscribers:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = f"{email_config.sender_name} <{email_config.email_address}>"
            msg["To"] = recipient
            msg.attach(MIMEText(markdown_body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))
            server.send_message(msg)


async def export_candidates() -> None:
    os.chdir(PROJECT_ROOT)
    load_dotenv(PROJECT_ROOT / ".env")

    service = HorizonPipelineService()
    with contextlib.redirect_stdout(sys.stderr):
        fetch_result, items, source_counts, used_hours = await _fetch_with_empty_retry(service)
    selected = sorted(items, key=_priority, reverse=True)[:RECIPIENT_LIMIT]

    payload = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "checked_hours": used_hours,
        "fetched": fetch_result["fetched"],
        "source_counts": source_counts,
        "items": [_item_json(i, item) for i, item in enumerate(selected, 1)],
    }
    signed_payload = _write_trusted_candidates(payload)
    print(json.dumps(signed_payload, ensure_ascii=False, indent=2, default=str))


def send_existing_summary(summary_file: str) -> None:
    os.chdir(PROJECT_ROOT)
    load_dotenv(PROJECT_ROOT / ".env")

    storage = StorageManager()
    config = storage.load_config()
    subscribers = storage.load_subscribers()
    summary = Path(summary_file).read_text(encoding="utf-8")
    allowed_urls = _load_trusted_candidate_urls()
    _validate_digest_structure(summary, allowed_urls)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    saved_path = storage.save_daily_summary(today, summary, language="zh")
    subject = f"【Horizon】今日文章速递 {datetime.now().strftime('%Y-%m-%d')}"
    _send_email(config, subscribers, subject, summary)
    print(json.dumps({
        "sent_to": subscribers,
        "summary_path": str(saved_path),
    }, ensure_ascii=False))


async def main() -> None:
    os.chdir(PROJECT_ROOT)
    load_dotenv(PROJECT_ROOT / ".env")

    storage = StorageManager()
    config = storage.load_config()
    service = HorizonPipelineService()

    fetch_result, items, source_counts, used_hours = await _fetch_with_empty_retry(service)
    selected = sorted(items, key=_priority, reverse=True)[:RECIPIENT_LIMIT]

    if not selected:
        raise RuntimeError(
            "No candidate articles fetched; skipping email "
            f"(checked_hours={used_hours}, fetched={fetch_result['fetched']}, "
            f"source_counts={source_counts})"
        )

    try:
        summary = await _generate_summary(
            config,
            selected,
            source_counts=source_counts,
            fetched=fetch_result["fetched"],
        )
    except Exception as exc:
        summary = _fallback_summary(
            selected,
            source_counts=source_counts,
            fetched=fetch_result["fetched"],
            error=exc,
        )

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _render_safe_html(summary)
    summary_path = storage.save_daily_summary(today, summary, language="zh")
    subject = f"【Horizon】今日文章速递 {datetime.now().strftime('%Y-%m-%d')}"
    subscribers = storage.load_subscribers()
    _send_email(config, subscribers, subject, summary)
    print({
        "sent_to": subscribers,
        "fetched": fetch_result["fetched"],
        "source_counts": source_counts,
        "summary_path": str(summary_path),
    })


async def cli_main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-candidates", action="store_true")
    parser.add_argument("--send-summary")
    args = parser.parse_args(argv)

    if args.export_candidates:
        await export_candidates()
        return
    if args.send_summary:
        send_existing_summary(args.send_summary)
        return
    await main()


if __name__ == "__main__":
    asyncio.run(cli_main())
