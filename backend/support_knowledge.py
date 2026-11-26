"""Find vetted LG support steps without sending customer or PDF data to OpenAI."""

import csv
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import pymupdf

from manual_catalog import catalog_for
from manual_search import MANUALS


# Only reviewed passages may add instructions. Raw crawl rows can be unverified,
# include different products, and contain procedures we do not guide in chat.
TOP_CODES = "solutions-20152965504689"
# Display labels in LG's general top-loader code guide, including status notices.
TOP_LOADER_DISPLAY_CODES = (
    "OE", "UE", "IE", "dE", "dL", "LE1", "CL", "LE", "FE", "tE",
    "AE", "FF", "E3", "tCL", "tC", "PE", "PF", "vS", "uS",
)
TOP_NOISE = "solutions-20150465968160"
TOP_FILL = "solutions-1779735"
FRONT_DRAIN = "solutions-1780183"
FRONT_FILL = "solutions-1779828"
TOWER_BALANCE = "solutions-20153937067714"
TOWER_DRAIN = "solutions-20153937049168"
MINI_DRAIN = "solutions-20150167603419"
MINI_DOOR = "solutions-20150161964224"
V6_PRODUCTS = {"top_loader": "통돌이", "front_loader": "드럼세탁기",
               "wash_tower": "워시타워", "mini_washer": "미니세탁기"}
RULES = {
    "top_drain_position": ("top_loader", "OE", TOP_CODES, "cs-tabpanel-03", "배수 호스가 눌리거나 꺾여",
        "세탁기 뒤에서 배수 호스가 본체나 벽에 눌려 있나요?",
        "작동을 멈춘 뒤, 손이 닿는 범위에서 배수 호스가 눌리거나 꺾인 부분을 펴 주세요. 호스를 분리하거나 세탁기를 옮겨야 한다면 작업을 중단하고 점검을 요청해 주세요."),
    "top_fill_supply": ("top_loader", "IE", TOP_FILL, "main", "단수",
        "세탁기뿐 아니라 집의 다른 수도에서도 물이 나오지 않나요?",
        "다른 수도에서도 물이 나오지 않으면 단수나 건물 급수 문제인지 먼저 확인해 주세요. 급수가 정상으로 돌아온 다음 세탁기 작동을 다시 확인해 주세요."),
    "top_fill_sensing": ("top_loader", "IE", TOP_FILL, "main", "무게 감지",
        "시작 직후 빨래판이 잠깐 움직이고 이후 급수가 시작되나요?",
        "세탁물 무게를 감지하는 동안 잠시 물이 나오지 않을 수 있어요. LG 통돌이 지원 안내는 이 과정이 약 40초 걸릴 수 있다고 설명합니다. 감지가 끝난 뒤에도 물이 들어오지 않는지 확인해 주세요."),
    "top_spin_load": ("top_loader", "SPIN_NOISE", TOP_NOISE, "main", "빨랫감이 한쪽으로 쏠려",
        "탈수를 멈춘 뒤 확인했을 때 빨랫감이 한쪽에 몰려 있나요?",
        "일시정지 후 세탁통이 완전히 멈춘 것을 확인하세요. 문이 열리는 상태에서 빨랫감을 고르게 펼친 다음, 문을 닫고 탈수를 다시 시작해 소리가 줄었는지 확인해 주세요."),
    "top_spin_contact": ("top_loader", "SPIN_NOISE", TOP_NOISE, "main", "선반이나 수납장이",
        "탈수할 때 세탁기 옆 선반이나 수납장에 부딪치는 소리가 들리나요?",
        "세탁기를 멈춘 뒤 주변에 부딪칠 만한 가벼운 물건을 치워 주세요. 본체나 고정 가구를 옮겨야 공간이 확보된다면 기사 점검을 요청해 주세요."),
    "top_spin_net": ("top_loader", "UE", TOP_CODES, "cs-tabpanel-04", "세탁망을 사용하셨나요?",
        "세탁망에 넣은 빨랫감만 단독으로 탈수하고 있었나요?",
        "세탁통이 완전히 멈춘 뒤 빨랫감이 한쪽에 몰리지 않도록 고르게 펴 주세요. 세탁망만 단독으로 넣지 말고 일반 의류와 함께 균형 있게 배치해 주세요."),
    "front_drain_position": ("front_loader", "OE", FRONT_DRAIN, "main", "배수호스를 확인해 보셨나요?",
        "드럼세탁기 뒤에서 배수 호스가 벽이나 본체에 눌려 있나요?",
        "작동을 멈춘 뒤, 손이 닿는 범위에서 배수 호스에 눌리거나 꺾인 곳이 있는지 보고 완만하게 펴 주세요. 본체를 움직이거나 호스를 분리해야 한다면 기사 점검을 요청해 주세요."),
    "front_fill_tap": ("front_loader", "IE", FRONT_FILL, "main", "수도꼭지가 제대로 열려",
        "드럼세탁기에 연결된 수도꼭지가 잠겨 있나요?",
        "급수 호스가 연결된 수도꼭지를 부드럽게 열어 주세요. 손으로 쉽게 열리지 않으면 무리해서 돌리지 말고 점검을 요청해 주세요. 정상적으로 열었다면 급수가 시작되는지 확인해 주세요."),
    "front_fill_hose": ("front_loader", "IE", FRONT_FILL, "main", "급수 호스를 펴서",
        "드럼세탁기의 급수 호스에서 눈에 보이는 부분이 눌리거나 꺾여 있나요?",
        "세탁기를 멈춘 뒤 손이 쉽게 닿는 호스의 꺾인 부분을 펴 주세요. 기기를 옮기거나 호스를 분리해야 한다면 기사 점검을 요청해 주세요."),
    "tower_spin_load": ("wash_tower", "UE", TOWER_BALANCE, "main", "고르게 퍼지도록 정리",
        "워시타워 아래쪽 세탁기의 빨랫감이 한쪽에 뭉쳐 있나요?",
        "세탁기를 일시 정지한 뒤 세탁통이 완전히 멈추고 문 잠금이 풀리면, 빨랫감을 꺼내 뭉친 부분을 풀고 고르게 넣어 주세요. 문을 닫고 다시 탈수해 같은 증상이 나타나는지 확인해 주세요."),
    "tower_spin_amount": ("wash_tower", "UE", TOWER_BALANCE, "main", "빨랫감의 양을 조절",
        "워시타워 아래쪽 세탁기의 세탁통이 가득 찼거나 빨랫감이 아주 적나요?",
        "세탁통이 완전히 멈추고 문 잠금이 풀린 뒤 빨랫감이 가득 찼으면 일부를 덜어 주세요. 너무 적으면 함께 세탁할 수 있는 옷을 더해 균형을 맞추고 다시 탈수해 주세요."),
    "tower_drain_hose": ("wash_tower", "OE", TOWER_DRAIN, "main", "배수 호스를 펴서",
        "워시타워 아래쪽 세탁기에서 물이 빠지지 않고, 그 세탁기의 배수 호스가 눈에 보이는 곳에서 꺾여 있나요?",
        "작동을 멈춘 뒤 손이 쉽게 닿는 호스의 꺾인 부분을 펴 주세요. 기기를 움직이거나 호스를 분리해야 한다면 기사 점검을 요청해 주세요."),
    "mini_drain_hose": ("mini_washer", "OE", MINI_DRAIN, "main", "배수 호스 꼬임 및 꺾임 확인하기",
        "미니세탁기 옆이나 뒤에 보이는 배수 호스가 눌리거나 꼬여 있나요?",
        "세탁기를 멈춘 뒤 손이 쉽게 닿는 호스의 꺾인 부분을 펴 주세요. 기기를 옮기거나 호스를 분리해야 한다면 기사 점검을 요청해 주세요."),
    "mini_door_cooldown": ("mini_washer", "DOOR_LOCKED", MINI_DOOR, "main", "높은 수온으로 인한 안전상의 문제",
        "소량 삶음이나 통살균처럼 물을 데우는 코스를 사용한 뒤 미니세탁기 문이 열리지 않나요?",
        "문을 억지로 당기지 말고 세탁통 안의 물이 식고 잠금이 풀릴 때까지 기다려 주세요. 충분히 기다린 뒤에도 열리지 않으면 점검을 요청해 주세요."),
}


@lru_cache(maxsize=4)
def read_v6(path: Path, stamp: int) -> dict:
    try:
        with ZipFile(path) as bundle:
            rows = json.loads(bundle.read("knowledge.json"))
    except (OSError, KeyError, ValueError, BadZipFile, RuntimeError):
        return {}
    if not isinstance(rows, list):
        return {}
    return {
        row["source_id"]: row for row in rows
        if isinstance(row, dict) and isinstance(row.get("source_id"), str)
        and row.get("content_status") == "text" and isinstance(row.get("content"), str)
        and isinstance(row.get("products"), list)
    }


def v6_for(directory: Path) -> dict:
    paths = ["lg_smartcare_v6_full_results (1).zip", "lg_smartcare_v6_full_results.zip"]
    for name in paths:
        path = directory / name
        if path.is_file():
            rows = read_v6(path, path.stat().st_mtime_ns)
            if rows:
                return rows
    return {}


@lru_cache(maxsize=8)
def read_knowledge(path: Path, stamp: int) -> dict:
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = json.load(stream) if path.suffix == ".json" else list(csv.DictReader(stream))
        return {row["knowledge_id"]: row for row in rows if isinstance(row, dict) and row.get("knowledge_id")}
    except (OSError, ValueError, TypeError):
        return {}


def knowledge_for(archive: Path) -> dict:
    directory = archive.parent.parent
    for extension in ("json", "csv"):
        path = directory / f"lg_washer_knowledge_v5.{extension}"
        if path.is_file():
            rows = read_knowledge(path, path.stat().st_mtime_ns)
            if rows:
                return rows
    return {}


@lru_cache(maxsize=256)
def appliance_type(archive: Path, model: str, stamp: int) -> str | None:
    if model in MANUALS:
        return "top_loader"
    try:
        entry = catalog_for(archive).get(model)
        if not entry or not entry[0].lower().endswith(".pdf"):
            return None
        with ZipFile(archive) as files, pymupdf.open(stream=files.read(entry[0]), filetype="pdf") as pdf:
            cover = pdf[0].get_text()
    except (OSError, ValueError, KeyError, pymupdf.FileDataError):
        return None
    for label, product in (("워시타워", "wash_tower"), ("워시콤보", "wash_combo"), ("미니워시", "mini_washer")):
        if label in re.sub(r"\s+", "", cover):
            return product
    if re.search(r"드럼\s*세탁기", cover):
        return "front_loader"
    if re.search(r"전자동\s*세탁기|통돌이", cover):
        return "top_loader"
    return None


def official_support_steps(archive: Path, model: str | None, section: str | None, detail: str, *, washer_type=None):
    from llm import rank_public_support

    if not section and re.search(r"탈수", detail) and re.search(r"소음|쿵|덜덜|부딪|진동", detail):
        section = "SPIN_NOISE"
    if not section:
        return [], None
    v6 = v6_for(archive.parent.parent)
    v5 = knowledge_for(archive)
    product = appliance_type(archive, model, archive.stat().st_mtime_ns) if model and archive.is_file() else washer_type
    candidates = {}
    for key, (kind, target, source_id, suffix, marker, condition, instruction) in RULES.items():
        if (kind, target) != (product, section):
            continue
        url = f"https://www.lge.co.kr/support/{source_id}"
        row = v6.get(source_id, {})
        from_v6 = V6_PRODUCTS.get(kind) in row.get("products", []) and source_matches(row, url, marker)
        if not from_v6:
            row = v5.get(f"{source_id}_{suffix}", {})
        if not from_v6 and (row.get("product_type") != kind or not source_matches(row, url, marker)):
            continue
        candidates[key] = {
            "action_id": key, "condition": condition, "instruction": instruction,
            "source": {"manual_name": "LG전자 공식 스스로 해결", "document_id": source_id,
                       "knowledge_version": "v6" if from_v6 else "v5",
                       "source_url": url, "excerpt": "LG전자 공개 안내를 바탕으로 정리한 점검과 조치입니다."},
        }
    if not candidates:
        return [], None
    # Only fixed, reviewed public snippets and a fixed topic cross the API boundary.
    order = rank_public_support(section, tuple(candidates)) if os.environ.get("OPENAI_API_KEY") else []
    return [candidates[key] for key in dict.fromkeys([*order, *candidates])], "openai" if order else "local"


def source_matches(row: dict, url: str, marker: str) -> bool:
    return row.get("source_url") == url and marker in " ".join(str(row.get("content", "")).split())


def official_code_reference(archive: Path, model: str | None, code: str, washer_type: str | None):
    """Find a product-specific LG article for an otherwise unsupported display code."""
    product = appliance_type(archive, model, archive.stat().st_mtime_ns) if model and archive.is_file() else washer_type
    prefix = {"top_loader": "[LG 통돌이 세탁기", "front_loader": "[LG 드럼세탁기"}.get(product)
    if not prefix or not re.fullmatch(r"[A-Z0-9]{2,4}", code, re.IGNORECASE):
        return None
    code_pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])", re.IGNORECASE)
    for source_id, row in v6_for(archive.parent.parent).items():
        title = row.get("title")
        if (V6_PRODUCTS[product] not in row["products"] or not isinstance(title, str)
                or not title.startswith(prefix) or not code_pattern.search(title)
                or not re.fullmatch(r"solutions-\d+", source_id)):
            continue
        url = f"https://www.lge.co.kr/support/{source_id}"
        if row.get("source_url") == url:
            return url
    return None
