from __future__ import annotations

import re

BOX_DRAWING_CHARS = set("│┆┊┃┗┛┏┓┳┻┣┫╋╠╣═║╚╗╔╝─┼╶╷╸╺╼╾")
OCR_ARTIFACTS = {"Me", "Mе", "№", "[]", "()", "[ ]", "{ }"}


def normalize_whitespace(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(str(text).split()).strip()


def normalize_multiline_input(text: str | None) -> str:
    if text is None:
        return ""
    value = str(text)
    value = value.replace("\r\n", "\n")
    value = value.replace("\r", "\n")
    value = value.replace("\t", " ")
    value = value.replace("\\n", "\n")
    value = value.replace("\\t", " ")
    return value


def safe_excerpt(text: str | None, limit: int = 120) -> str:
    clean = normalize_whitespace(text)
    if not clean:
        return ""
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def chunk_text(text: str | None, chunk_size: int = 3500):
    clean = text or ""
    for i in range(0, len(clean), chunk_size):
        yield clean[i : i + chunk_size]


def remove_noise(text: str | None) -> str:
    clean = normalize_whitespace(text)
    clean = re.sub(r"[\u200b\u200c\u200d]", "", clean)
    return clean.strip()


def _is_ocr_artifact_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True

    if stripped in OCR_ARTIFACTS:
        return True

    letters = sum(ch.isalpha() for ch in stripped)
    digits = sum(ch.isdigit() for ch in stripped)
    alnum = letters + digits
    nonspace = sum(not ch.isspace() for ch in stripped)
    box_count = sum(ch in BOX_DRAWING_CHARS for ch in stripped)
    symbols = sum((not ch.isalnum()) and (not ch.isspace()) for ch in stripped)

    if stripped in {"|", "||", "|||", "||||"}:
        return True

    if nonspace <= 2 and alnum == 0:
        return True

    if letters == 0 and alnum < 2:
        return True

    if box_count and letters == 0 and alnum < 3:
        return True

    if len(stripped) >= 4 and alnum / max(1, len(stripped)) < 0.18:
        return True

    if len(stripped) >= 6 and symbols / max(1, len(stripped)) > 0.55:
        return True

    return False


def normalize_ocr_text(text: str | None) -> str:
    if not text:
        return ""

    text = normalize_multiline_input(text).replace("\r", "\n")
    out_lines: list[str] = []
    last_blank = False

    for raw_line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if not line:
            if out_lines and not last_blank:
                out_lines.append("")
            last_blank = True
            continue

        if _is_ocr_artifact_line(line):
            continue

        line = re.sub(r"(?<!\w)[\|╚╔╩╦╠╣┐┌└┘─]+(?!\w)", "", line).strip()
        if not line:
            continue

        out_lines.append(line)
        last_blank = False

    while out_lines and out_lines[0] == "":
        out_lines.pop(0)
    while out_lines and out_lines[-1] == "":
        out_lines.pop()

    return "\n".join(out_lines)
