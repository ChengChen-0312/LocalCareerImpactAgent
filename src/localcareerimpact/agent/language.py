"""Deterministic response-language selection from user-authored prose only."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from .contracts import ReportLanguage


_CONFIRMATIONS = frozenset(
    {
        "confirm",
        "confirm profile",
        "confirm and analyse",
        "confirm and analyze",
        "确认",
        "確認",
        "确认资料",
        "確認資料",
        "确认并分析",
        "確認並分析",
    }
)
_ATTACHMENT_PLACEHOLDER = "shared candidate material for profile extraction."
_DIRECTIVE = re.compile(
    r"(?ix)"
    r"(?P<no_en>\b(?:do\s+not|don't|avoid)\s+"
    r"(?:reply|respond|write|answer|report|output|use)?\s*(?:in\s+)?english\b|"
    r"(?:不要|不再|不|別|别|勿)(?:用|使用|以)?(?:英文|英语|英語))"
    r"|(?P<no_zh>\b(?:do\s+not|don't|avoid)\s+"
    r"(?:reply|respond|write|answer|report|output|use)?\s*"
    r"(?:in\s+)?(?:chinese|mandarin)\b|"
    r"(?:不要|不再|不|別|别|勿)(?:用|使用|以)?"
    r"(?:中文|汉语|漢語|华语|華語))"
    r"|(?P<en>\bin\s+english\b|\benglish\s+(?:please|response|report|answer)\b|"
    r"\b(?:reply|respond|write|answer|report|output|use)\s+(?:to\s+me\s+)?"
    r"(?:in\s+)?english\b|(?:请|請)?(?:用|使用|以)(?:英文|英语|英語)"
    r"(?:回复|回覆|回答|输出|輸出|报告|報告)?|"
    r"(?:英文|英语|英語)(?:回复|回覆|回答|输出|輸出)|"
    r"(?:报告|報告)?(?:语言|語言)\s*(?:[:：]|为|為|是)?\s*"
    r"(?:英文|英语|英語)|(?:改为|改為|改成|切换为|切換為)\s*"
    r"(?:英文|英语|英語))"
    r"|(?P<zh>\bin\s+(?:chinese|mandarin)\b|"
    r"\b(?:reply|respond|write|answer|report|output|use)\s+(?:to\s+me\s+)?"
    r"(?:in\s+)?(?:chinese|mandarin)\b|(?:请|請)?(?:用|使用|以)"
    r"(?:中文|汉语|漢語|华语|華語)"
    r"(?:回复|回覆|回答|输出|輸出|报告|報告)?|"
    r"(?:中文|汉语|漢語|华语|華語)(?:回复|回覆|回答|输出|輸出)|"
    r"(?:报告|報告)?(?:语言|語言)\s*(?:[:：]|为|為|是)?\s*"
    r"(?:中文|汉语|漢語|华语|華語)|(?:改为|改為|改成|切换为|切換為)\s*"
    r"(?:中文|汉语|漢語|华语|華語))"
)
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_FENCED_CODE = re.compile(r"(?s)```.*?```")
_INLINE_CODE = re.compile(r"`[^`]*`")
_IDENTIFIER = re.compile(
    r"(?<!\w)(?:[A-Za-z]:[\\/]|[./~][\w./\\-]+|[A-Za-z][\w-]*(?:::|[/\\_.])[\w./\\:-]+)(?!\w)"
)
_ENGLISH_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_TECHNICAL_WORDS = frozenset(
    {
        "ai", "api", "aws", "azure", "c", "cli", "cloud", "css", "csv",
        "docker", "gcp", "git", "github", "gpt", "html", "http", "https",
        "java", "javascript", "json", "kubernetes", "llm", "ml", "mlx", "npm",
        "pdf", "python", "qwen", "react", "sql", "typescript", "url", "xml",
    }
)


def is_substantive_user_text(text: str) -> bool:
    compact = " ".join(text.split())
    if not compact:
        return False
    folded = compact.casefold()
    return folded not in _CONFIRMATIONS and folded != _ATTACHMENT_PLACEHOLDER


def select_response_language(latest_first_user_texts: Iterable[str]) -> ReportLanguage:
    """Use the latest substantive user text; callers must not pass attachment text."""

    for text in latest_first_user_texts:
        if not is_substantive_user_text(text):
            continue
        explicit = _explicit_language(text)
        if explicit is not None:
            return explicit
        return classify_narrative_language(text) or "zh"
    return "zh"


def classify_narrative_language(text: str) -> Literal["en", "zh"] | None:
    """Classify natural prose while treating code, URLs, and common tech terms as neutral."""

    cleaned = _FENCED_CODE.sub(" ", text)
    cleaned = _INLINE_CODE.sub(" ", cleaned)
    cleaned = _URL.sub(" ", cleaned)
    cleaned = _IDENTIFIER.sub(" ", cleaned)
    english_words = 0
    for match in _ENGLISH_WORD.finditer(cleaned):
        word = match.group(0)
        if word.casefold() in _TECHNICAL_WORDS:
            continue
        if len(word) > 1 and word.isupper():
            continue
        english_words += 1
    han_characters = sum("\u3400" <= character <= "\u9fff" for character in cleaned)
    if english_words == 0 and han_characters == 0:
        return None
    return "en" if english_words > han_characters else "zh"


def _explicit_language(text: str) -> ReportLanguage | None:
    selected: ReportLanguage | None = None
    for match in _DIRECTIVE.finditer(text):
        if match.lastgroup in {"en", "no_zh"}:
            selected = "en"
        elif match.lastgroup in {"zh", "no_en"}:
            selected = "zh"
    return selected


__all__ = [
    "classify_narrative_language",
    "is_substantive_user_text",
    "select_response_language",
]
