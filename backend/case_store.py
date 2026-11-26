import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Case:
    case_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=now)
    updated_at: str | None = None
    browser_id: str | None = None
    device_id: str | None = None
    device_name: str | None = None
    category: str = "washer"
    previous_case_id: str | None = None
    related_case_ids: list[str] = field(default_factory=list)
    device_history: list[dict] = field(default_factory=list)
    agent_state: dict = field(default_factory=dict)
    model: str | None = None
    symptoms: list[str] = field(default_factory=list)
    error_code: str | None = None
    display_label: str | None = None
    washer_type: str | None = None
    guidance_mode: str = "hybrid"
    guidance_match: str | None = None
    status: str = "collecting"
    service_reason: str | None = None
    service_consent_at: str | None = None
    service_request: dict | None = None
    service_detail: str | None = None
    symptom_revision: int = 0
    display_code_status: str | None = None
    report_details: dict = field(default_factory=dict)
    attachments: list[dict] = field(default_factory=list)
    media_collected_at: str | None = None
    evidence_submitted_at: str | None = None
    checks: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)

    def add_message(self, role: str, text: str) -> None:
        self.messages.append({"role": role, "text": text, "at": now()})


class JsonCaseStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def save(self, case: Case) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{case.case_id}.json"
        case.updated_at = now()
        payload = {"schema_version": 1, **asdict(case)}
        # Replace the snapshot only after a complete write.
        with NamedTemporaryFile(
            dir=self.directory, mode="w", encoding="utf-8", delete=False
        ) as temporary:
            temp_path = Path(temporary.name)
        try:
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)
        return path

    def load(self, case_id: str) -> Case | None:
        path = self.directory / f"{case_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return Case(**{item.name: data[item.name] for item in fields(Case) if item.name in data})

    def history(self, browser_id: str) -> list[Case]:
        cases = (self.load(path.stem) for path in self.directory.glob("*.json"))
        return sorted((case for case in cases if case and case.browser_id == browser_id),
                      key=lambda case: case.updated_at or case.created_at, reverse=True)
