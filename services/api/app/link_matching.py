"""Heuristic scores for WeChat / Instagram / LinkedIn account pairing."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import pairwise
from urllib.parse import urlparse

from pypinyin import Style, lazy_pinyin
from rapidfuzz import fuzz
from unidecode import unidecode

SPACE_PATTERN = re.compile(r"[^a-z0-9]+")
COMPACT_PATTERN = re.compile(r"[^a-z0-9]")
REPEATED_CHARACTER_PATTERN = re.compile(r"(.)\1{2,}")
EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"\+?\d[\d\-\s]{7,}\d")
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
LINKEDIN_HOSTS = {"linkedin.com", "www.linkedin.com"}
PROVIDERS = ("wechat", "instagram", "linkedin")
PAIR_LABELS = {
    "instagram-wechat": "Instagram × 微信",
    "linkedin-wechat": "LinkedIn × 微信",
    "instagram-linkedin": "Instagram × LinkedIn",
    "wechat-instagram": "微信 × Instagram",
    "wechat-linkedin": "微信 × LinkedIn",
    "linkedin-instagram": "LinkedIn × Instagram",
}


@lru_cache(maxsize=200_000)
def ascii_text(value: str) -> str:
    return unidecode(unicodedata.normalize("NFKC", value or "")).lower()


@lru_cache(maxsize=200_000)
def words(value: str) -> str:
    return " ".join(SPACE_PATTERN.sub(" ", ascii_text(value)).split())


@lru_cache(maxsize=200_000)
def compact(value: str) -> str:
    return COMPACT_PATTERN.sub("", ascii_text(value))


@lru_cache(maxsize=200_000)
def pinyin_compact(value: str) -> str:
    converted = " ".join(
        lazy_pinyin(
            unicodedata.normalize("NFKC", value or ""),
            style=Style.NORMAL,
            errors=lambda item: list(item),
        )
    )
    return compact(converted)


@lru_cache(maxsize=200_000)
def username_words(value: str) -> str:
    tokens = words(value).split()
    collapsed = [REPEATED_CHARACTER_PATTERN.sub(r"\1", token) for token in tokens]
    return " ".join(token for token in collapsed if len(token) > 1)


def instagram_username(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    if "://" in cleaned:
        parsed = urlparse(cleaned)
        if parsed.hostname not in INSTAGRAM_HOSTS:
            return ""
        cleaned = parsed.path.strip("/").split("/", 1)[0]
    return cleaned.lstrip("@").strip().lower()


def linkedin_handle(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    if "://" in cleaned or cleaned.startswith("www."):
        parsed = urlparse(cleaned if "://" in cleaned else "https://" + cleaned)
        if parsed.hostname not in LINKEDIN_HOSTS:
            return ""
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if parts and parts[0] in {"in", "company"}:
            parts = parts[1:]
        cleaned = parts[0] if parts else ""
    return cleaned.strip().lower()


def populated(values: Iterable[str | None]) -> list[str]:
    return [value.strip() for value in values if value and str(value).strip()]


def field_texts(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            texts.extend(field_texts(item))
        return texts
    if isinstance(value, dict):
        texts: list[str] = []
        for key in ("text", "value", "name", "url", "username", "headline", "alias"):
            texts.extend(field_texts(value.get(key)))
        if not texts:
            for item in value.values():
                if isinstance(item, str):
                    texts.extend(field_texts(item))
        return texts
    return []


@dataclass
class AccountSignals:
    account_id: str
    provider: str
    external_id: str
    display_name: str
    username: str = ""
    profile_url: str = ""
    profile_id: str | None = None
    profile_name: str = ""
    ready: bool = False
    avatar_media_id: str | None = None
    aliases: list[str] = field(default_factory=list)
    handles: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)

    def identity_values(self) -> list[str]:
        return populated(
            [self.display_name, self.profile_name, self.username, self.external_id, *self.aliases]
        )


@dataclass(frozen=True)
class CandidateScore:
    score: int
    reasons: list[str]


def _handle(provider: str, value: str) -> str:
    if provider == "instagram":
        return instagram_username(value)
    if provider == "linkedin":
        return linkedin_handle(value)
    return (value or "").strip().lower()


def score_pair(source: AccountSignals, target: AccountSignals) -> CandidateScore:
    source_handle = _handle(source.provider, source.username or source.external_id or source.profile_url)
    identity_values = target.identity_values()
    score = 0
    reasons: list[str] = []

    if source_handle and source_handle in target.handles:
        score = 100
        reasons.append(f"对方资料已填写相同 {source.provider}")

    shared_emails = source.emails & target.emails
    if shared_emails:
        score = max(score, 99)
        reasons.append("邮箱完全一致")
    shared_phones = source.phones & target.phones
    if shared_phones:
        score = max(score, 98)
        reasons.append("电话完全一致")

    source_handle_compact = compact(source_handle)
    exact_identity = next(
        (
            value
            for value in identity_values
            if compact(value) == source_handle_compact and len(source_handle_compact) >= 4
        ),
        None,
    )
    if exact_identity:
        score = max(score, 97)
        reasons.append(f"账号与「{exact_identity}」完全一致")

    source_name = source.display_name or next(iter(source.aliases), "")
    source_name_compact = compact(source_name)
    name_is_username = (
        bool(source_name_compact)
        and fuzz.ratio(source_name_compact, source_handle_compact) >= 92
    )
    if not name_is_username and len(source_name_compact) >= 4:
        exact_name = next(
            (value for value in identity_values if compact(value) == source_name_compact),
            None,
        )
        if exact_name:
            score = max(score, 94)
            reasons.append(f"姓名与「{exact_name}」完全一致")

    best_name_score = 0
    best_name_value = ""
    best_name_method = ""
    if not name_is_username and len(source_name_compact) >= 4:
        source_name_words = words(source_name)
        source_name_tokens = source_name_words.split()
        for value in identity_values:
            value_words = words(value)
            value_compact = compact(value)
            if len(value_compact) < 4:
                continue
            fuzzy_score = max(
                fuzz.WRatio(source_name_words, value_words),
                fuzz.token_sort_ratio(source_name_words, value_words),
            )
            if len(source_name_tokens) >= 2 and len(value_words.split()) == 1:
                fuzzy_score = min(fuzzy_score, 78)
            if fuzzy_score > best_name_score:
                best_name_score = round(fuzzy_score)
                best_name_value = value
                best_name_method = "姓名"
            candidate_pinyin = pinyin_compact(value)
            source_pinyin = pinyin_compact(source_name)
            if len(candidate_pinyin) >= 4 and len(source_pinyin) >= 4:
                pinyin_score = max(
                    fuzz.ratio(source_name_compact, candidate_pinyin),
                    fuzz.ratio(source_pinyin, candidate_pinyin),
                    fuzz.ratio(source_pinyin, value_compact),
                )
                if len(source_name_tokens) >= 2 and " " not in value_words:
                    pinyin_score = min(pinyin_score, 78)
                if pinyin_score > best_name_score:
                    best_name_score = round(pinyin_score)
                    best_name_value = value
                    best_name_method = "姓名拼音"
    if best_name_score >= 70:
        score = max(score, min(93, best_name_score))
        reasons.append(f"{best_name_method} {best_name_score}% 接近「{best_name_value}」")

    best_username_score = 0
    best_username_value = ""
    source_username_words = username_words(source_handle or source.username)
    if len(source_username_words.replace(" ", "")) >= 5:
        for value in identity_values:
            value_words = words(value)
            if not value_words:
                continue
            username_score = max(
                fuzz.WRatio(source_username_words, value_words),
                fuzz.token_set_ratio(source_username_words, value_words),
            )
            if username_score > best_username_score:
                best_username_score = round(username_score)
                best_username_value = value
    if best_username_score >= 78:
        score = max(score, min(90, best_username_score - 2))
        reasons.append(f"用户名词组 {best_username_score}% 接近「{best_username_value}」")

    if source.provider == "linkedin" and target.provider == "wechat":
        headline = next((alias for alias in source.aliases if " " in alias and len(alias) > 12), "")
        if headline:
            for value in identity_values:
                if compact(value) and compact(value) in compact(headline) and len(compact(value)) >= 4:
                    score = max(score, 76)
                    reasons.append(f"职位简介包含「{value}」")
                    break

    if len(reasons) >= 2 and score < 97:
        score = min(96, score + 3)
    if len(source_name_compact) < 5 and score < 90:
        score = min(score, 72)
    return CandidateScore(score=score, reasons=reasons[:3])


def confidence(score: int) -> str:
    if score >= 95:
        return "very_high"
    if score >= 85:
        return "high"
    if score >= 70:
        return "medium"
    if score >= 55:
        return "low"
    return "weak"


def pair_key(source_provider: str, target_providers: Iterable[str]) -> str:
    preferred = {"instagram": "wechat", "linkedin": "wechat", "wechat": "instagram"}
    target = preferred.get(source_provider)
    available = set(target_providers)
    if target not in available:
        target = next((item for item in PROVIDERS if item != source_provider and item in available), "")
    return f"{source_provider}-{target}" if target else source_provider


@lru_cache(maxsize=200_000)
def _value_keys(value: str) -> frozenset[str]:
    keys: set[str] = set()
    for packed in (compact(value), pinyin_compact(value)):
        if len(packed) >= 4:
            keys.add("c:" + packed)
    for token in (*words(value).split(), *username_words(value).split()):
        if len(token) >= 3:
            keys.add("t:" + token)
    syllables = [compact(item) for item in lazy_pinyin(unicodedata.normalize("NFKC", value or ""), style=Style.NORMAL, errors="ignore")]
    syllables = [item for item in syllables if item]
    for first, second in pairwise(syllables):
        keys.add("t:" + first + second)
    return frozenset(keys)


def candidate_keys(account: AccountSignals) -> set[str]:
    """Index keys shared by any two accounts that ``score_pair`` can rate as candidates.

    Exact identifiers, whole names (plain and pinyin), name and username tokens, and
    adjacent pinyin syllables cover the exact, fuzzy-name, and username signals.
    """
    keys = {"e:" + item for item in account.emails} | {"p:" + item for item in account.phones}
    keys |= {"c:" + compact(item) for item in account.handles if len(compact(item)) >= 4}
    for value in account.identity_values():
        keys |= _value_keys(value)
    return keys
