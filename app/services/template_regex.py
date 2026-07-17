from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


TEMPLATE_REGEX_TIMEOUT_SECONDS = 2.0
TEMPLATE_REGEX_RESULT_MAX_CHARS = 1_000_000


@dataclass(frozen=True)
class SafeRegexMatch:
    full_match: str
    captured_groups: tuple[str | None, ...]
    named_groups: dict[str, str | None]
    match_start: int
    match_end: int
    lastindex: int | None

    def group(self, selector=0):
        if selector == 0:
            return self.full_match
        if isinstance(selector, str):
            if selector not in self.named_groups:
                raise IndexError(selector)
            return self.named_groups[selector]
        index = int(selector)
        if index < 1 or index > len(self.captured_groups):
            raise IndexError(index)
        return self.captured_groups[index - 1]

    def groupdict(self) -> dict[str, str | None]:
        return dict(self.named_groups)

    def start(self) -> int:
        return self.match_start

    def end(self) -> int:
        return self.match_end


def _match_payload(match: re.Match, remaining_chars: int) -> tuple[dict, int]:
    spans = [match.span(index) for index in range(match.re.groups + 1)]

    def span_length(span: tuple[int, int]) -> int:
        start, end = span
        return 0 if start < 0 or end < 0 else end - start

    character_count = span_length(spans[0])
    character_count += sum(span_length(span) for span in spans[1:])
    character_count += sum(
        len(name) + span_length(spans[index])
        for name, index in match.re.groupindex.items()
    )
    if character_count > remaining_chars:
        raise ValueError("captured regex output exceeds safety limit")
    full_match = match.group(0)
    captured_groups = list(match.groups())
    named_groups = match.groupdict()
    return {
        "full_match": full_match,
        "captured_groups": captured_groups,
        "named_groups": named_groups,
        "match_start": match.start(),
        "match_end": match.end(),
        "lastindex": match.lastindex,
    }, character_count


def _worker_main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        compiled = re.compile(
            str(payload["pattern"]),
            re.IGNORECASE | re.MULTILINE,
        )
        text = str(payload.get("text") or "")
        mode = payload.get("mode")
        if mode == "search":
            match = compiled.search(text)
            match_payload = None
            if match:
                match_payload, _used_chars = _match_payload(
                    match,
                    TEMPLATE_REGEX_RESULT_MAX_CHARS,
                )
            result = {"match": match_payload}
        elif mode == "finditer":
            limit = max(1, int(payload.get("limit") or 1))
            matches = []
            remaining_chars = TEMPLATE_REGEX_RESULT_MAX_CHARS
            for index, match in enumerate(compiled.finditer(text)):
                if index >= limit:
                    break
                match_payload, used_chars = _match_payload(match, remaining_chars)
                matches.append(match_payload)
                remaining_chars -= used_chars
            result = {"matches": matches}
        else:
            raise ValueError("unsupported regex mode")
        sys.stdout.write(json.dumps({"ok": True, **result}, ensure_ascii=False))
        return 0
    except (RecursionError, re.error, TypeError, ValueError) as error:
        sys.stdout.write(
            json.dumps(
                {"ok": False, "error": f"{type(error).__name__}: {error}"},
                ensure_ascii=False,
            )
        )
        return 2


def _run_regex_worker(
    payload: dict,
    *,
    timeout_seconds: float | None = None,
) -> tuple[dict | None, str | None]:
    worker_path = Path(__file__).resolve()
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", str(worker_path), "--worker"],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=max(
                0.05,
                min(timeout_seconds or TEMPLATE_REGEX_TIMEOUT_SECONDS, TEMPLATE_REGEX_TIMEOUT_SECONDS),
            ),
            check=False,
            creationflags=creationflags,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return None, "客户模板正则执行超时，请缩小匹配范围或修改规则。"
    except OSError as error:
        return None, f"客户模板正则隔离执行失败：{error}"

    try:
        result = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return None, "客户模板正则隔离执行未返回有效结果。"
    if completed.returncode != 0 or not result.get("ok"):
        return None, f"客户模板正则执行失败：{result.get('error') or '未知错误'}"
    return result, None


def safe_regex_search(
    pattern: str,
    text: str,
    *,
    timeout_seconds: float | None = None,
) -> tuple[SafeRegexMatch | None, str | None]:
    result, error = _run_regex_worker(
        {"mode": "search", "pattern": pattern, "text": text},
        timeout_seconds=timeout_seconds,
    )
    if error or result is None or result.get("match") is None:
        return None, error
    return SafeRegexMatch(**result["match"]), None


def safe_regex_finditer(
    pattern: str,
    text: str,
    *,
    limit: int,
    timeout_seconds: float | None = None,
) -> tuple[list[SafeRegexMatch], str | None]:
    result, error = _run_regex_worker(
        {"mode": "finditer", "pattern": pattern, "text": text, "limit": limit},
        timeout_seconds=timeout_seconds,
    )
    if error or result is None:
        return [], error
    return [SafeRegexMatch(**match) for match in result.get("matches") or []], None


if __name__ == "__main__" and "--worker" in sys.argv:
    raise SystemExit(_worker_main())
