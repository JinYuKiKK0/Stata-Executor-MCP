from __future__ import annotations

import re

from stata_executor.contract import ErrorKind

_COMMAND_ECHO_PATTERN = re.compile(r"^\.\s*$|^\.\s+\S")
_COMMAND_LINE_PATTERN = re.compile(r"^\.\s+\S")
_NUMBERED_LINE_PATTERN = re.compile(r"^\s*\d+\.\s")
_CONTINUATION_PATTERN = re.compile(r"^>\s")
_LOG_INFO_PATTERN = re.compile(
    r"^\s*(name:|log:|log type:|opened on:|closed on:|Log file saved to:)",
    re.IGNORECASE,
)
_SEPARATOR_PATTERN = re.compile(r"[-+=]+")
_RC_LINE_PATTERN = re.compile(r"^r\(\d+\);?\s*$")
_DISPLAY_COMMANDS = frozenset({"display", "di", "dis", "disp"})
_PREFIX_COMMANDS = frozenset({"quietly", "qui", "noisily", "noi", "capture", "cap"})


def parse_exit_code(text: str, fallback: int) -> int:
    match = re.findall(r"__AGENT_RC__\s*=\s*(\d+)", text)
    if match:
        return int(match[-1])
    generic = re.findall(r"r\((\d+)\)", text)
    if generic:
        return int(generic[-1])
    return fallback


def classify_execution_failure(text: str, exit_code: int) -> ErrorKind:
    low = text.lower()
    if exit_code in {198, 199}:
        return "stata_parse_or_command_error"
    if "invalid syntax" in low or "unrecognized" in low:
        return "stata_parse_or_command_error"
    return "stata_runtime_error"


def build_execution_summary(text: str, exit_code: int) -> str:
    if exit_code == 0:
        return "Stata do-file completed successfully."

    error_signature = extract_error_signature(text, exit_code)
    if error_signature:
        return f"Stata execution failed with exit_code={exit_code}: {error_signature}"
    return f"Stata execution failed with exit_code={exit_code}."


def build_bootstrap_summary(text: str) -> str:
    stripped = [line.strip() for line in text.splitlines() if line.strip()]
    if stripped:
        return f"Stata subprocess bootstrap failed: {stripped[-1]}"
    return "Stata subprocess bootstrap failed before any execution log was created."


def render_result_text(text: str) -> str:
    if not text:
        return ""
    raw_lines = strip_agent_rc_trailer(text.splitlines())
    blocks = extract_empirical_result_blocks(raw_lines)
    if blocks:
        return "\n\n".join(blocks)
    return _render_filtered_fallback(raw_lines)


def extract_empirical_result_blocks(lines: list[str]) -> list[str]:
    blocks: list[str] = []
    for cmd_word, output in _iter_command_segments(lines):
        cleaned = _clean_output_inner(output)
        extracted = _extract_relevant_block(cleaned, cmd_word)
        if extracted:
            blocks.append("\n".join(extracted))
    return blocks


def _render_filtered_fallback(lines: list[str]) -> str:
    filtered: list[str] = []
    previous_blank = False
    for line in lines:
        if _COMMAND_ECHO_PATTERN.match(line):
            continue
        if _NUMBERED_LINE_PATTERN.match(line):
            continue
        if _CONTINUATION_PATTERN.match(line):
            continue
        if _LOG_INFO_PATTERN.match(line):
            continue
        is_blank = not line.strip()
        if is_blank:
            if previous_blank:
                continue
            filtered.append("")
            previous_blank = True
            continue
        filtered.append(line.rstrip())
        previous_blank = False
    while filtered and not filtered[-1].strip():
        filtered.pop()
    return "\n".join(filtered)


def _iter_command_segments(lines: list[str]):
    n = len(lines)
    i = 0
    pre: list[str] = []
    while i < n and not _COMMAND_LINE_PATTERN.match(lines[i]):
        pre.append(lines[i])
        i += 1
    if pre:
        yield None, pre

    while i < n:
        cmd_text = lines[i][2:].rstrip()
        i += 1
        while i < n and _CONTINUATION_PATTERN.match(lines[i]):
            cmd_text += " " + lines[i][2:].strip()
            i += 1
        cmd_word = _extract_base_command(cmd_text)
        output: list[str] = []
        while i < n and not _COMMAND_LINE_PATTERN.match(lines[i]):
            output.append(lines[i])
            i += 1
        yield cmd_word, output


def _extract_base_command(cmd_text: str) -> str:
    for word in cmd_text.strip().split():
        lowered = word.lower()
        if lowered in _PREFIX_COMMANDS:
            continue
        return lowered
    return ""


def _clean_output_inner(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    for line in lines:
        if _NUMBERED_LINE_PATTERN.match(line):
            continue
        if _CONTINUATION_PATTERN.match(line):
            continue
        if _LOG_INFO_PATTERN.match(line):
            continue
        cleaned.append(line)
    return cleaned


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if "|" in line:
        return True
    return bool(_SEPARATOR_PATTERN.fullmatch(stripped))


def _extract_relevant_block(cleaned: list[str], cmd_word: str | None) -> list[str]:
    if not cleaned:
        return []

    table_indices = [i for i, line in enumerate(cleaned) if _is_table_line(line)]
    if len(table_indices) >= 2:
        first_idx = table_indices[0]
        last_idx = table_indices[-1]
        start = 0
        while start < first_idx and not cleaned[start].strip():
            start += 1
        end = last_idx + 1
        while end < len(cleaned) and cleaned[end].strip():
            end += 1
        return _compact_blanks([line.rstrip() for line in cleaned[start:end]])

    for i, line in enumerate(cleaned):
        if _RC_LINE_PATTERN.match(line.strip()):
            start = max(0, i - 5)
            return [cleaned[j].rstrip() for j in range(start, i + 1) if cleaned[j].strip()]

    if cmd_word in _DISPLAY_COMMANDS:
        return [line.rstrip() for line in cleaned if line.strip()]

    return []


def _compact_blanks(lines: list[str]) -> list[str]:
    result: list[str] = []
    previous_blank = False
    for line in lines:
        is_blank = not line.strip()
        if is_blank:
            if previous_blank:
                continue
            result.append("")
            previous_blank = True
        else:
            result.append(line)
            previous_blank = False
    while result and not result[-1].strip():
        result.pop()
    return result


def extract_diagnostics(text: str, exit_code: int) -> tuple[str, str | None, str | None]:
    if not text:
        return "", None, None
    if exit_code == 0:
        return "", None, None

    lines = text.splitlines()
    command_start, failed_command = extract_last_command_block(lines)
    error_index, error_signature = extract_error_signature_with_index(lines, exit_code)

    if command_start is not None and error_index is not None and command_start <= error_index:
        excerpt_start = command_start
    elif error_index is not None:
        excerpt_start = error_index
    elif command_start is not None:
        excerpt_start = command_start
    else:
        excerpt_start = 0

    excerpt_lines = strip_agent_rc_trailer(lines[excerpt_start:])
    excerpt = "\n".join(excerpt_lines).strip()
    return excerpt, error_signature, failed_command


def extract_last_command_block(lines: list[str]) -> tuple[int | None, str | None]:
    block_start: int | None = None
    block_lines: list[str] = []
    blocks: list[tuple[int, str]] = []

    for index, raw_line in enumerate(lines):
        if raw_line.startswith(". "):
            if block_start is not None and block_lines:
                blocks.append((block_start, "\n".join(block_lines).strip()))
            block_start = index
            block_lines = [raw_line[2:].rstrip()]
            continue

        if raw_line.startswith("> ") and block_start is not None:
            block_lines.append(raw_line[2:].rstrip())

    if block_start is not None and block_lines:
        blocks.append((block_start, "\n".join(block_lines).strip()))

    if not blocks:
        return None, None
    return blocks[-1]


def extract_error_signature_with_index(
    lines: list[str], exit_code: int
) -> tuple[int | None, str | None]:
    if exit_code == 0:
        return None, None

    final_rc_index: int | None = None
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip()
        if re.fullmatch(r"r\(\d+\);?", stripped):
            final_rc_index = index
            break

    search_end = final_rc_index if final_rc_index is not None else len(lines)
    for index in range(search_end - 1, -1, -1):
        stripped = lines[index].strip()
        if not stripped:
            continue
        if stripped.startswith("__AGENT_RC__") or stripped.startswith("r("):
            continue
        if lines[index].startswith(". ") or lines[index].startswith("> "):
            continue
        return index, stripped
    return None, None


def extract_error_signature(text: str, exit_code: int) -> str | None:
    _, signature = extract_error_signature_with_index(text.splitlines(), exit_code)
    return signature


def extract_last_meaningful_line(text: str) -> str | None:
    for raw_line in reversed(text.splitlines()):
        stripped = raw_line.strip()
        if stripped:
            return stripped
    return None


def strip_agent_rc_trailer(lines: list[str]) -> list[str]:
    trimmed = list(lines)
    while trimmed and trimmed[-1].strip().startswith("__AGENT_RC__"):
        trimmed.pop()
    return trimmed


def strip_agent_rc_trailer_text(text: str) -> str:
    return "\n".join(strip_agent_rc_trailer(text.splitlines())).strip()
