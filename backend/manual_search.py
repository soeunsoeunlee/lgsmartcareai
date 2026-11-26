import re
from pathlib import Path
from zipfile import ZipFile

import pymupdf
from manual_catalog import catalog_for, catalog_rows


MANUALS = {
    "T25OZ0": ("T25OZ0_WM_KOR_MFL71831239_02_260115_00_OM_WEB.pdf", "MFL71831239"),
    "TR14WK1": ("TR14WK1_WM_KOR_MFL71831208_00_220118_00_WEB.pdf", "MFL71831208"),
    "TR13ML2": ("TR13ML2_WM_KOR_MFL71831210_01_240607_00_OM_WEB.pdf", "MFL71831210"),
    "TR16DV6": ("TR16DV6_WM_KOR_MFL71831233_00_250116_00_OM_WEB.pdf", "MFL71831233"),
}
OE_ENTRY = re.compile(r"^OE\s+배수")
NEXT_CODE = re.compile(r"[A-Za-z][A-Za-z0-9]{1,4}")

# Codes verified in the display-message tables of the four registered PDFs.
# The extracted lock glyph looks like "[L", and LE1/tcL alternate on TR16DV6.
MANUAL_ALERTS = ("OE", "UE", "IE", "dE", "dL", "LE1", "CL", "LE", "FE", "tE", "AE", "FF", "E3", "tcL")
ALERT_PAGES = {"T25OZ0": (45, 46), "TR14WK1": (36, 37), "TR13ML2": (39, 40), "TR16DV6": (35, 36)}

# Summaries follow the verified rows. The common service row does not
# distinguish individual causes, so those codes share the manual's guidance.
SERVICE_ROW = "이 표에서는 개별 원인을 구분하지 않고 LG전자 서비스센터 점검을 안내합니다."
ALERT_SUMMARIES = {
    "OE": ("배수 오류입니다. 물이 제대로 빠지지 않을 때 표시됩니다.", 0),
    "UE": ("탈수 중 세탁물이 한쪽에 치우치거나 뭉쳤을 때 확인하는 코드입니다.", 0),
    "IE": ("급수 오류입니다. 물이 제대로 들어오지 않을 때 표시됩니다.", 0),
    "DE": ("세탁기 문이 완전히 닫히지 않았는지 확인하는 코드입니다.", 1),
    "DL": ("세탁기 문이 닫혀 있어도 잠기지 않아 작동하지 않을 수 있습니다.", 1),
    "LE1": ("세탁통이 잘 돌지 않을 때 표시될 수 있습니다. 과도한 세탁물이나 세탁통에 낀 이물질이 원인일 수 있습니다.", 1),
    "CL": ("버튼 잠금 상태를 알리는 표시입니다.", 1),
    "LE": (SERVICE_ROW, 1), "FE": (SERVICE_ROW, 1), "TE": (SERVICE_ROW, 1),
    "AE": (SERVICE_ROW, 1), "E3": (SERVICE_ROW, 1),
    "FF": ("동결 가능성을 확인하는 코드입니다. 매뉴얼은 겨울철 동결 관리 안내를 참고하도록 합니다.", 1),
    "TCL": ("세탁통을 청소할 시기를 알려주는 알림입니다.", 1),
}


def available_models(archive: Path) -> list[str]:
    try:
        return list(dict.fromkeys([*MANUALS, *catalog_for(archive)]))
    except (OSError, ValueError, BadZipFile):
        return list(MANUALS)


def has_manual(archive: Path, model: str | None) -> bool:
    return bool(model and (model in MANUALS or model in available_models(archive)))


def alerts_for(archive: Path, model: str) -> list[str]:
    if model in MANUALS:
        return list(MANUAL_ALERTS)
    try:
        entry = catalog_for(archive).get(model)
    except (OSError, ValueError, BadZipFile):
        return []
    if not entry:
        return []
    canonical = {label.upper(): label for label in MANUAL_ALERTS}
    return [location[0][1] if re.search(r"[가-힣]", location[0][1]) else canonical.get(code, code)
            for code, location in entry[2].items()]


def source_location(source: dict) -> str:
    if source.get("source_url"):
        return source["source_url"]
    if source.get("pdf_page"):
        return f"PDF {source['pdf_page']}쪽"
    return f"HTML 도움말 {source['html_topic']}"


def explain_alert(model: str, code: str, archive: Path | None = None) -> str | None:
    if model not in MANUALS and archive:
        entry = catalog_for(archive).get(model)
        locations = entry[2].get(code) if entry else None
        if not locations:
            return None
        location, label = locations[0]
        reference = f"PDF {location}쪽" if isinstance(location, int) else f"HTML 도움말 {location}"
        return f"{code} · 해당 모델 매뉴얼의 ‘{label}’ 항목에 해당해요. ({entry[1]} {reference})"
    if model not in ALERT_PAGES or code not in ALERT_SUMMARIES:
        return None
    summary, page_index = ALERT_SUMMARIES[code]
    display_code = next(label for label in MANUAL_ALERTS if label.upper() == code)
    page = ALERT_PAGES[model][page_index]
    return f"{display_code} · {summary} (LG 사용설명서 PDF {page}쪽)"


# Search patterns cover only customer-operable steps reviewed in these PDFs.
# Instructions are extracted from the selected model, including wording variants.
REVIEWED_ALERT_STEPS = {
    "UE": (
        ("balance_load", "세탁물이 한쪽으로 치우쳐 있나요?", r"세탁물을 고르게 펴(?:세요|주세요)\."),
        ("separate_bedding", "이불을 두 장 이상 같이 세탁했나요?", r"이불을 크기에 상관없이 한 장씩 세탁하세요\."),
        ("spread_tangled_load", "세탁물이 뭉쳐 있나요?", r"동작/일시정지 버튼을 눌러 UE를 해제(?:한|시킨) 후 세탁물을 고르게 펴고 탈수를 선택하여 제품을 작동(?:하|시키)세요\."),
    ),
    "IE": (
        ("open_tap", "수도꼭지가 잠겨 있나요?", r"세탁기와 연결된 수도꼭지를 여세요\."),
        ("straighten_inlet", "급수 호스가 꺾여 있나요?", r"급수 호스가 꺾이지 않도록 펴세요\."),
    ),
    "DE": (("close_door", "세탁기 문을 완전히 닫지 않은 상태에서 제품을 작동시켰나요?",
            r"세탁기 문을 완전히 닫으세요\."),),
    "DL": (("reclose_door", "세탁기 문이 닫혀있으나 제품이 작동되지 않나요?",
            r"세탁기 문을 완전히 닫으세요\."),),
    "LE1": (("reduce_load", "많은 양의 세탁물을 넣었나요?",
             r"세탁물을 조금 빼낸 후 세탁을 다시 시작하세요\."),),
    "CL": (("unlock_controls", "버튼을 눌러도 버튼이 작동하지 않나요?",
            r"버튼 잠금 기능을 해제하려면 온수세탁 또는 온냉수 버튼과 헹굼 버튼을 동시에 3초간 누르세요\."),),
    "TCL": (("clean_tub", "1~2개월 내에 세탁통을 청소하지 않았나요?",
             r"(?:통살균 또는 )?통세척 코스를 진행하세요\."),),
    "FF": (("warm_drain_hose", "배수 호스가 얼어 있는지 확인할 수 있나요?",
            r"배수 호스가 언 경우 따뜻한 물을 적신 수건을 감싸 녹여?\s*(?:주세요|이세요)\."),),
}
EXTENDED_OE_STEPS = (
    ("lower_hose", "배수 호스가 올려져 있나요?", r"배수 호스를 내리(?:세요|고 다시 작동해 주세요)\.|배수 호스를 내려주세요\."),
    ("straighten_hose", "배수 호스가 꺾여 있거나 문턱이 높아 내부가 막혀 있나요?",
     r"배수 호스가 꺾여 굴곡이 생기면 이물질로 인해서 배수가 되지 않으므로 바닥이 고른 장 ?소에 바르게 놓으세요\."),
    ("lower_hose", "배수 호스가 올려져 있습니까?", r"배수 호스를 내리십시오\."),
    ("straighten_hose", "배수 호스가 꺾여 있거나 문턱이 높아 내부가 막혀 있습니까?",
     r"배수 호스가 꺾여 굴곡이 생기면 이물질로 인 ?해서 배수가 되지 않으므로 바닥이 고른 장소 ?에 바르게 놓으십시오\."),
)
LEGACY_ALERT_STEPS = {
    "IE": (("open_tap", "수도꼭지가 잠겨 있습니까?", r"수도꼭지를 트십시오\."),
           ("straighten_inlet", "급수 호스가 꺾여 있습니까?", r"급수 호스가 꺾이지 않도록 펴십시오\.")),
}
ROW_MARKER = re.compile(r"^(?:UE|IE|OE|dE|dL|LE1|LE~1~LE~1|\[L|LE FE tE AE FF E3|tcL|tc ~ L ~ tc ~ L|증상)$", re.MULTILINE)
ROW_ALIASES = {"[L": "CL", "LE~1~LE~1": "LE1", "tc ~ L ~ tc ~ L": "TCL"}
WINTER_PAGES = {"T25OZ0": 36, "TR14WK1": 28, "TR13ML2": 30, "TR16DV6": 27}


def reviewed_alert_steps(archive: Path, model: str, code: str) -> list[dict]:
    if model not in MANUALS:
        entry = catalog_for(archive).get(model)
        if not entry:
            return []
        rules = EXTENDED_OE_STEPS if code == "OE" else (*REVIEWED_ALERT_STEPS.get(code, ()), *LEGACY_ALERT_STEPS.get(code, ()))
        actions = {}
        for location, _, body in catalog_rows(archive, entry, code):
            text = " ".join(body.split())
            for action_id, question, pattern in rules:
                _, marker, remainder = text.partition(question)
                # A rule can be used only if its question and reviewed action occur in the same row.
                match = re.search(pattern.replace("UE를", "(?:UE|에러)를"), remainder.partition("?")[0]) if marker else None
                if not match:
                    continue
                source = {"manual_name": f"LG 세탁기 사용설명서 {model}", "filename": entry[0],
                          "document_id": entry[1], "excerpt": f"{question}\n{match.group()}"}
                source["pdf_page" if isinstance(location, int) else "html_topic"] = location
                actions.setdefault(action_id, {"action_id": action_id, "condition": question,
                                               "instruction": match.group(), "source": source})
        return list(actions.values())
    rules = REVIEWED_ALERT_STEPS.get(code)
    if not rules:
        return []
    filename, document_id = MANUALS[model]
    page = WINTER_PAGES[model] if code == "FF" else ALERT_PAGES[model][ALERT_SUMMARIES[code][1]]
    with ZipFile(archive) as manuals:
        pdf = manuals.read(filename)
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        text = "\n".join(line.strip() for line in document[page - 1].get_text().splitlines())
    if code != "FF":
        markers = list(ROW_MARKER.finditer(text))
        for index, marker in enumerate(markers):
            label = marker.group()
            if ROW_ALIASES.get(label, label.upper()) == code:
                end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
                text = text[marker.end():end]
                break
        else:
            return []
    actions = []
    text = " ".join(text.split())
    for action_id, question, pattern in rules:
        if code == "FF":
            section = text
        else:
            _, separator, after = text.partition(question)
            if not separator:
                continue
            # Stop at the next question so a different condition cannot supply this step.
            section = after.partition("?")[0]
        match = re.search(pattern, section)
        if match:
            actions.append({
                "action_id": action_id, "condition": question, "instruction": match.group(),
                "source": {"manual_name": f"LG 세탁기 사용설명서 {model}", "filename": filename,
                           "document_id": document_id, "pdf_page": page, "excerpt": match.group()},
            })
    return actions


def search_oe(archive: Path, model: str) -> tuple[str, list[tuple[int | str, str]]]:
    if model not in MANUALS:
        entry = catalog_for(archive)[model]
        return entry[1], [(location, f"{label}\n{body}") for location, label, body in catalog_rows(archive, entry, "OE")]
    filename, document_id = MANUALS[model]
    with ZipFile(archive) as manuals:
        pdf = manuals.read(filename)
    results = []
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        for page in document:
            lines = [line.strip() for line in page.get_text().splitlines()]
            for index, line in enumerate(lines):
                if not OE_ENTRY.match(" ".join(lines[index:index + 2])):
                    continue
                # The symptom suffix confirms the OE row. Keep the complete row
                # on each page while excluding the next error-code row.
                end = index + 1
                while end < len(lines) and not NEXT_CODE.fullmatch(lines[end]):
                    end += 1
                results.append((page.number + 1, "\n".join(lines[index:end]).strip()))
                break
    return document_id, results
