"""첨부파일 텍스트 추출.

공공기관 채용공고는 본문 HTML이 비어 있고 실제 자격 요건('나무의사 자격
소지자' 등)이 첨부파일 안에만 있는 경우가 매우 흔하다. 실측 예: 서울시
일자리포털 공공일자리 상세 페이지는 상세요강 필드가 대부분 공란이고 내용이
전부 .hwpx 에 들어 있었다. 따라서 첨부 파싱은 선택이 아니라 필수다.

지원 형식
  .hwpx : ZIP + OWPML XML. 표준 라이브러리로 완전 추출 가능.
  .pdf  : pypdf
  .hwp  : 구형 바이너리(OLE). PrvText 스트림(미리보기 텍스트, UTF-16LE)만
          추출한다. 전문이 아니라 앞부분 일부이므로 truncated=True 로 표시하고
          호출측이 '첨부 전문 확인 필요'로 안내한다.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024   # 20MB 초과 첨부는 건너뜀
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Extracted:
    text: str = ""
    kind: str = ""
    truncated: bool = False
    error: str = ""


def _zip_xml(data: bytes, kind: str) -> Extracted:
    """ZIP+XML 문서(.hwpx / OOXML)에서 텍스트를 추출한다."""
    out: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".xml")]
        # .hwpx 는 Contents/section*.xml 에 본문이 있다. 있으면 그것만.
        content = [n for n in names if n.startswith("Contents/")]
        # OOXML 은 word/document.xml, xl/sharedStrings.xml, ppt/slides/*.xml
        if not content:
            content = [n for n in names
                       if re.match(r"(word/document|word/footnotes|xl/sharedStrings"
                                   r"|xl/worksheets/|ppt/slides/)", n)]
        for n in sorted(content or names[:40]):
            raw = z.read(n).decode("utf-8", "replace")
            # 단락 종료 태그를 줄바꿈으로 바꿔 단어가 붙는 것을 막는다
            raw = re.sub(r"</(?:hp:|w:|a:)?(?:p|t|si)>", " \n", raw)
            out.append(_TAG.sub(" ", raw))
    text = re.sub(r"[ \t]+", " ", "\n".join(out))
    return Extracted(text=text, kind=kind)


def _pdf(data: bytes) -> Extracted:
    try:
        from pypdf import PdfReader
    except ImportError:
        return Extracted(kind="pdf", error="pypdf 미설치")
    reader = PdfReader(io.BytesIO(data))
    pages = reader.pages[:30]           # 30쪽까지만
    text = "\n".join((p.extract_text() or "") for p in pages)
    return Extracted(text=text, kind="pdf",
                     truncated=len(reader.pages) > 30)


def _hwp(data: bytes) -> Extracted:
    """구형 .hwp: OLE PrvText(미리보기) 스트림만 추출 → 부분 텍스트."""
    try:
        import olefile
    except ImportError:
        return Extracted(kind="hwp", error="olefile 미설치")
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
        if not ole.exists("PrvText"):
            return Extracted(kind="hwp", truncated=True,
                             error="PrvText 없음 — 본문 추출 불가")
        raw = ole.openstream("PrvText").read()
        return Extracted(text=raw.decode("utf-16-le", "replace"),
                         kind="hwp", truncated=True)
    except Exception as e:                       # noqa: BLE001
        return Extracted(kind="hwp", error=f"{type(e).__name__}: {e}")


def extract(data: bytes, filename: str) -> Extracted:
    """확장자에 따라 텍스트를 뽑는다. 실패해도 예외를 올리지 않는다."""
    name = (filename or "").lower()
    if len(data) > MAX_BYTES:
        return Extracted(error=f"용량 초과({len(data)//1024//1024}MB)")
    try:
        if name.endswith(".hwpx"):
            return _zip_xml(data, "hwpx")
        if name.endswith(".pdf"):
            return _pdf(data)
        if name.endswith(".hwp"):
            return _hwp(data)
        if name.endswith((".txt", ".csv")):
            return Extracted(text=data.decode("utf-8", "replace"), kind="text")
        if name.endswith((".docx", ".xlsx", ".pptx", ".zip")):
            return _zip_xml(data, name.rsplit(".", 1)[-1])
    except Exception as e:                       # noqa: BLE001
        return Extracted(error=f"{type(e).__name__}: {e}")
    return Extracted(error=f"미지원 형식: {name.rsplit('.', 1)[-1][:8]}")
