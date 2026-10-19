from __future__ import annotations

from pathlib import Path

try:
    from PIL import Image, ImageFilter, ImageOps
except Exception:  # pragma: no cover
    Image = None
    ImageFilter = None
    ImageOps = None

try:
    import pytesseract
    from pytesseract import Output
except Exception:  # pragma: no cover
    pytesseract = None
    Output = None

from src.utils.text_cleaner import normalize_ocr_text


class OCRService:
    LANG_PRESETS = {
        # A single combined language model is preferable for mixed screenshots:
        # trying rus and eng separately can turn Latin text into Cyrillic lookalikes.
        "auto": ["rus+eng"],
        "mixed": ["rus+eng"],
        "rus": ["rus"],
        "eng": ["eng"],
    }

    def __init__(self, mode: str = "auto"):
        self.mode = (mode or "auto").lower()
        self.availability_error: str | None = None
        self.available = self._check_availability()

    def _check_availability(self) -> bool:
        if pytesseract is None or Image is None or ImageFilter is None or ImageOps is None:
            self.availability_error = "Не установлены Python-зависимости Pillow/pytesseract."
            return False
        try:
            pytesseract.get_tesseract_version()
        except Exception:
            self.availability_error = "Не найден системный Tesseract OCR. Проверь сборку Render."
            return False
        try:
            languages = set(pytesseract.get_languages(config=""))
        except Exception:
            self.availability_error = "Tesseract запущен, но не удалось прочитать языковые данные."
            return False

        required = {"rus", "eng"} if self.mode in {"auto", "mixed"} else {self.mode}
        missing = sorted(required - languages)
        if missing:
            self.availability_error = "В Tesseract не хватает языковых пакетов: " + ", ".join(missing) + "."
            return False
        return True

    def _prepare_image(self, path: str | Path):
        if Image is None or ImageFilter is None or ImageOps is None:
            raise RuntimeError("Pillow is not installed")
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
        if img.mode != "L":
            img = img.convert("L")
        img = ImageOps.autocontrast(img)
        if img.width < 1600:
            img = img.resize((img.width * 2, img.height * 2))
        # Median filtering removes thin strokes from small UI text and album labels.
        # Keep the source detail and apply only a light sharpen filter.
        img = img.filter(ImageFilter.SHARPEN)
        return img

    @staticmethod
    def _safe_index(seq, idx: int, default=0):
        try:
            return seq[idx]
        except Exception:
            return default

    @staticmethod
    def _normalize_conf(value) -> float:
        try:
            return float(value)
        except Exception:
            return -1.0

    @staticmethod
    def _meaningful_token(text: str) -> bool:
        if not text:
            return False
        stripped = text.strip()
        if not stripped:
            return False
        if stripped in {"|", "||", "|||", "||||", "[]", "()"}:
            return False
        letters = sum(ch.isalpha() for ch in stripped)
        digits = sum(ch.isdigit() for ch in stripped)
        nonspace = sum(not ch.isspace() for ch in stripped)
        if nonspace <= 2 and letters == 0 and digits == 0:
            return False
        if letters == 0 and digits == 0:
            return False
        return True

    def _language_candidates(self) -> list[str]:
        return self.LANG_PRESETS.get(self.mode, self.LANG_PRESETS["auto"])

    def _reconstruct_from_data(self, img: Image.Image, lang: str, psm: int) -> tuple[str, float]:
        if pytesseract is None or Output is None:
            return "", 0.0

        try:
            data = pytesseract.image_to_data(
                img,
                lang=lang,
                config=f"--psm {psm} --oem 3",
                output_type=Output.DICT,
                timeout=12,
            )
        except Exception:
            return "", 0.0

        texts = data.get("text", [])
        if not texts:
            return "", 0.0

        lines: dict[tuple[int, int, int], list[tuple[int, str, float]]] = {}
        confidences: list[float] = []
        for idx, raw_text in enumerate(texts):
            text = (raw_text or "").strip()
            if not self._meaningful_token(text):
                continue

            conf = self._normalize_conf(self._safe_index(data.get("conf", []), idx, -1))
            if 0 <= conf < 40:
                continue
            if conf >= 0:
                confidences.append(conf)

            key = (
                int(self._safe_index(data.get("block_num", []), idx, 0)),
                int(self._safe_index(data.get("par_num", []), idx, 0)),
                int(self._safe_index(data.get("line_num", []), idx, 0)),
            )
            left = int(self._safe_index(data.get("left", []), idx, 0))
            lines.setdefault(key, []).append((left, text, conf))

        ordered_lines: list[str] = []
        for key in sorted(lines.keys()):
            words = [word for _, word, conf in sorted(lines[key], key=lambda item: item[0]) if conf < 0 or conf >= 40]
            line = " ".join(words).strip()
            if not line:
                continue
            ordered_lines.append(line)

        text = normalize_ocr_text("\n".join(ordered_lines))
        if not text:
            return "", 0.0

        # Prefer a useful, confidently recognized sparse-text result without
        # allowing a few isolated high-confidence words to beat a fuller reading.
        confidence = sum(confidences) / len(confidences) if confidences else 0.0
        word_count = sum(len(line.split()) for line in ordered_lines)
        score = confidence + min(word_count, 12) * 1.5
        return text, score

    def extract_text(self, path: str | Path) -> str | None:
        if not self.available:
            return None

        img = self._prepare_image(path)
        langs = self._language_candidates()
        candidates: list[tuple[float, str]] = []
        for lang in langs:
            # PSM 11 handles screenshots with separated labels better than PSM 6,
            # while PSM 6/4 remain useful for conventional text blocks.
            for psm in (11, 6, 4):
                text, score = self._reconstruct_from_data(img, lang, psm)
                if text:
                    candidates.append((score, text))

        if not candidates:
            return None
        return max(candidates, key=lambda candidate: candidate[0])[1]
