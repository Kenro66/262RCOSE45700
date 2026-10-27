from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Literal, NoReturn

import pymupdf
from lingua import Language, LanguageDetectorBuilder

from app.contracts import ParsedPage

PARSER_VERSION = "pymupdf-1"
logger = logging.getLogger(__name__)
UPLOAD_FAILURE_MESSAGE = "上传失败,请重试!"
LANGUAGE_CODES = {
    Language.KOREAN: "ko",
    Language.CHINESE: "zh",
    Language.ENGLISH: "en",
}
LANGUAGE_DETECTOR = LanguageDetectorBuilder.from_languages(*LANGUAGE_CODES.keys()).build()

ERROR_CANNOT_OPEN = "文件无法打开或需要密码才能打开"
ERROR_NOT_PDF = "文件不是 PDF 文件"
ERROR_NO_TEXT = "所有页面均无可提取文字"
ERROR_TOO_MANY_EMPTY_PAGES = "无可提取文字的页数占总页数超过50%"
ERROR_PAGE_EXTRACTION = "页面读取或文字提取报错"


class PdfParseError(ValueError):
    """整份解析失败；异常消息写入 error_message。"""

def _failed(detail: str) -> NoReturn:
    """详细原因只写日志，对外异常只包含统一提示。"""
    logger.warning("PDF parsing failed: %s", detail)
    raise PdfParseError(UPLOAD_FAILURE_MESSAGE) from None


def detect_language(text: str) -> Literal["ko", "zh", "en"] | None:
    """
    判断文件中文字的语言
    语言能否识别不影响解析是否成功,整份文件照常判定。
    """
    try:
        if not text.strip():
            return None

        confidence_values = LANGUAGE_DETECTOR.compute_language_confidence_values(text)
        if not confidence_values:
            return None

        best = confidence_values[0]
        second_value = confidence_values[1].value if len(confidence_values) > 1 else 0.0
        if best.value < 0.55 or best.value - second_value < 0.10:
            return None
        return LANGUAGE_CODES.get(best.language)
    except Exception:
        return None


def _is_pdf_file(path: Path) -> bool:
    """
    初步检查文件是否是PDF类型
    """
    try:
        with path.open("rb") as handle:
            return b"%PDF-" in handle.read(1024)
    except OSError:
        return False


def parse_pdf(pdf_path: str | Path) -> list[ParsedPage]:
    """
    使用 PyMuPDF 打开并逐页解析文字
    """
    pdf_path = Path(pdf_path)
    document_id = pdf_path.stem
    if not pdf_path.is_file():
        _failed(ERROR_CANNOT_OPEN)
    if not _is_pdf_file(pdf_path):
        _failed(ERROR_NOT_PDF)
    try:
        document = pymupdf.open(pdf_path)
    except Exception:
        _failed(ERROR_CANNOT_OPEN)

    try:
        if document.needs_pass:
            _failed(ERROR_CANNOT_OPEN)

        if document.page_count == 0:
            _failed(ERROR_NO_TEXT)

        pages: list[ParsedPage] = []
        empty_page_count = 0

        for page_index in range(document.page_count):
            page_number = page_index + 1
            try:
                page = document.load_page(page_index)
                text = page.get_text("text").strip()
            except Exception as exc:
                _failed(f"{ERROR_PAGE_EXTRACTION}（第 {page_number} 页）：{exc}")

            if not text:
                empty_page_count += 1

            pages.append(
                ParsedPage(
                    document_id=document_id,
                    page_index=page_index,
                    page_number=page_number,
                    text=text,
                    language=detect_language(text),
                    content_hash=(
                        "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
                    ),
                    char_count=len(text),
                    parser_version=PARSER_VERSION,
                    blocks=None,
                )
            )

        if empty_page_count == document.page_count:
            _failed(ERROR_NO_TEXT)

        if empty_page_count / document.page_count > 0.5:
            _failed(ERROR_TOO_MANY_EMPTY_PAGES)

        return pages
    except PdfParseError:
        raise
    except Exception as exc:
        _failed(f"{ERROR_PAGE_EXTRACTION}：{exc}")
    finally:
        document.close()
