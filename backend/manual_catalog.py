"""Read-only indexing of model-specific PDF and Korean HTML troubleshooting rows."""

import csv
import html
import re
from collections import defaultdict
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from threading import Lock
from zipfile import BadZipFile, ZipFile

import pymupdf


LABEL_CODES = {
    "문 열림": "DE", "문 잠기지 않음": "DL", "급수 안 됨": "IE", "급수 안됨": "IE",
    "배수 안 됨": "OE", "배수 안됨": "OE", "탈수 안 됨": "UE", "탈수 안됨": "UE",
    "동결 감지": "FF", "물 높이 감지 안됨": "PE", "모터 회전 이상": "LE1",
    "물 높이 높음": "FE", "클러치 작동 이상": "AE", "온도 감지 안됨": "TE",
    "배수 모터 동작 이상": "E3", "버튼 잠금": "CL", "통세척 알림": "TCL",
    "탈수가 안됩니다.": "UE", "탈수가 진행되지 않을 경우": "UE",
    "급수가 안 되거나 물이 약하게 나옵니다.": "IE", "급수가 안되거나 물이 약하게 나올 경우": "IE",
    "배수가 안되거나 물이 조금씩 빠집니다.": "OE", "배수가 안되거나 물이 조금씩 빠질 경우": "OE",
    "세탁통이 회전하지 않습니다.": "LE1",
    "[L": "CL", "LE~1~LE~1": "LE1", "tc ~ L ~ tc ~ L": "TCL",
}
ERROR_CODES = {"OE", "UE", "IE", "DE", "DL", "LE1", "CL", "LE", "FE", "TE", "AE", "FF", "E3", "TCL", "PE"}
MODEL_NAME = re.compile(r"^[A-Z][A-Z0-9.-]*[0-9][A-Z0-9.-]*$")
DOC_ID = re.compile(r"MFLR?\d{7,}", re.I)
TITLE_TAG = re.compile(r'<div\b[^>]*class="troubleshooting-title"[^>]*>(.*?)</div>', re.S | re.I)
TAGS = re.compile(r"<[^>]+>")
CATALOG_LOCK = Lock()


def labels_for(heading: str) -> list[str]:
    cleaned = heading.strip()
    if cleaned in LABEL_CODES:
        return [LABEL_CODES[cleaned]]
    cleaned = re.sub(r"\s*\(.*\)$", "", cleaned)
    words = re.split(r"\s*[,/]\s*|\s+", cleaned)
    return [word.upper() for word in words] if words and all(word.upper() in ERROR_CODES for word in words) else []


def pdf_sections(text: str):
    lines = [line.strip() for line in text.splitlines()]
    if not any(line in {"고장 신고 전 확인하기", "고장 신고 전 확인 사항", "고장신고 전 확인하기", "표시부 알림"}
               for line in lines):
        return
    headings = []
    for i, line in enumerate(lines):
        label = line
        extra = re.sub(r"\s+", "", line + lines[i + 1]).strip() if i + 1 < len(lines) else ""
        if not labels_for(label) and extra:
            label = LEGACY_LINE_WRAPS.get(extra, line)
        codes = labels_for(label)
        if codes or (i > 2 and line in {"증상", "급수 호스 연결부에 물이 샙니다."}):
            headings.append((i, codes, label, 1 if label == line else 2))
    for index, (start, codes, label, consumed) in enumerate(headings):
        if codes:
            end = headings[index + 1][0] if index + 1 < len(headings) else len(lines)
            yield codes, label, "\n".join(lines[start + consumed:end])


LEGACY_LINE_WRAPS = {
    re.sub(r"\s+", "", label): label for label in LABEL_CODES if label.endswith(".")
}


def html_text(fragment: str) -> str:
    return " ".join(html.unescape(TAGS.sub(" ", fragment)).split())


def html_sections(text: str):
    headings = list(TITLE_TAG.finditer(text))
    for i, heading in enumerate(headings):
        label = html_text(heading[1])
        codes = labels_for(label)
        if codes:
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            yield codes, label, html_text(text[heading.end():end])


def exact_downloads(archive: Path) -> dict:
    log = archive.with_name("manuals_log.csv")
    if not log.is_file():
        return {}
    exact = defaultdict(set)
    with log.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            model = row.get("모델명", "")
            if row.get("상태") != "ok" or not MODEL_NAME.fullmatch(model):
                continue
            for filename in row.get("파일", "").split(" | "):
                if filename.split("_", 1)[0] == model:
                    exact[filename.removesuffix(" (중복)")].add(model)
    return exact


def cover_models(text: str, filename_model: str) -> set[str]:
    models = set()
    match = re.search(r"모델명\s*[:：]\s*([^\r\n]+)", text)
    if match:
        for token in re.findall(r"[A-Z][A-Z0-9.*()-]*", match[1]):
            if MODEL_NAME.fullmatch(token):
                models.add(token)
            elif "*" in token:
                pattern = re.escape(token).replace(r"\(\*\)", "[A-Z0-9]*").replace(r"\*", "[A-Z0-9]*")
                if re.fullmatch(pattern, filename_model):
                    models.add(filename_model)
    if re.search(rf"(?<![A-Z0-9]){re.escape(filename_model)}(?![A-Z0-9])", text):
        models.add(filename_model)
    return models


@lru_cache(maxsize=4)
def archive_catalog(archive: Path, fingerprint: tuple[int, ...]) -> dict:
    if not archive.is_file():
        return {}
    downloads = exact_downloads(archive)
    entries = {}
    with ZipFile(archive) as source:
        # Prefer a model-named PDF, then HTML, then an explicitly listed PDF family alias.
        for filename in sorted(source.namelist(), reverse=True):
            document = DOC_ID.search(filename)
            model = filename.split("_", 1)[0]
            if not document or not MODEL_NAME.fullmatch(model):
                continue
            locators = defaultdict(list)
            models = set(downloads.get(filename, ()))
            try:
                if filename.lower().endswith(".pdf"):
                    with pymupdf.open(stream=source.read(filename), filetype="pdf") as pdf:
                        cover = pdf[0].get_text() if len(pdf) else ""
                        models |= cover_models(cover, model)
                        if not models:
                            continue
                        for page in pdf:
                            for codes, label, _ in pdf_sections(page.get_text()):
                                for code in codes:
                                    locators[code].append((page.number + 1, label))
                elif filename.lower().endswith(".zip") and models:
                    with ZipFile(BytesIO(source.read(filename))) as package:
                        for topic in package.namelist():
                            if not topic.startswith("ko-kr/") or not topic.endswith(".html"):
                                continue
                            for codes, label, _ in html_sections(package.read(topic).decode("utf-8", errors="replace")):
                                for code in codes:
                                    locators[code].append((topic, label))
                else:
                    continue
            except (BadZipFile, OSError, pymupdf.FileDataError):
                continue
            if not locators:
                continue
            for candidate in models:
                rank = 3 if filename.lower().endswith(".pdf") and candidate == model else 2 if candidate == model else 1
                if candidate not in entries or rank > entries[candidate][0]:
                    entries[candidate] = (rank, filename, document[0], dict(locators))
    return {model: entry[1:] for model, entry in sorted(entries.items())}


def catalog_for(archive: Path) -> dict:
    archive = archive.resolve()
    paths = (archive, archive.with_name("manuals_log.csv"))
    fingerprint = tuple(path.stat().st_mtime_ns if path.is_file() else 0 for path in paths)
    with CATALOG_LOCK:
        return archive_catalog(archive, fingerprint)


def catalog_rows(archive: Path, entry, code: str):
    filename, _, locators = entry
    requested = dict.fromkeys(location for location, _ in locators.get(code, ()))
    if not requested:
        return
    with ZipFile(archive) as source:
        data = source.read(filename)
    if filename.lower().endswith(".pdf"):
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            for page in requested:
                for codes, label, body in pdf_sections(pdf[page - 1].get_text()):
                    if code in codes:
                        yield page, label, body
    else:
        with ZipFile(BytesIO(data)) as package:
            for topic in requested:
                for codes, label, body in html_sections(package.read(topic).decode("utf-8", errors="replace")):
                    if code in codes:
                        yield topic, label, body
