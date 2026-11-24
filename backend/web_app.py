"""Local web entry point for manual-based consultations."""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from agent import TERMINAL_STATES, SelfCareAgent
from case_store import JsonCaseStore, now
from cli import DEFAULT_ARCHIVE, ROOT
from manual_search import MANUALS, MANUAL_ALERTS
from reports import customer_report


KST = timezone(timedelta(hours=9), name="KST")
ATTACHMENT_STATES = {"collecting_report", "checking_condition", "awaiting_action_result",
                     "collecting_attachments", "service_offered", "resolved", "service_declined"}
MAX_FILE = 25 * 1024 * 1024
MAX_FILES = 5
CATEGORIES = {"washer": "세탁기", "air_conditioner": "에어컨", "dryer": "건조기", "other": "기타 가전"}
IDENTIFIER = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
STATE_FIELDS = ("pending", "condition", "asked_model", "asked_code", "results_only", "no_displayed_code")
ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/styles.css": ("styles.css", "text/css")}
for photo in ("washer-error-display", "drain-hose-down", "drain-hose-straight",
              "balance-laundry", "washer-door", "inlet-tap"):
    filename = f"guides/{photo}.png"
    ASSETS[f"/{filename}"] = (filename, "image/png")


def media_type(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"RIFF"):
        if data[8:12] == b"WEBP":
            return "webp", "image/webp"
        if data[8:12] == b"WAVE":
            return "wav", "audio/wav"
    if data.startswith(b"ID3") or (
        len(data) > 2 and data[0] == 255 and data[1] & 0xE0 == 0xE0 and data[1] & 0x06
    ):
        return "mp3", "audio/mpeg"
    if data[4:8] == b"ftyp":
        if data[8:12] in {b"M4A ", b"M4B "}:
            return "m4a", "audio/mp4"
        if data[8:12] == b"qt  ":
            return "mov", "video/quicktime"
        if data[8:12] in {b"isom", b"iso2", b"iso5", b"mp41", b"mp42", b"avc1", b"M4V ", b"MSNV"}:
            if b"soun" in data[:65_536] and b"vide" not in data[:65_536]:
                return "m4a", "audio/mp4"
            return "mp4", "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in data[:128]:
        header = data[:65_536]
        if (b"A_OPUS" in header or b"A_VORBIS" in header) and not any(
            codec in header for codec in (b"V_VP8", b"V_VP9", b"V_AV1")
        ):
            return "webm", "audio/webm"
        return "webm", "video/webm"
    if data.startswith(b"OggS") and (b"OpusHead" in data[:256] or b"vorbis" in data[:256]):
        return "ogg", "audio/ogg"
    return None


class SmartCareServer(ThreadingHTTPServer):
    def __init__(self, address, store=None, archive=DEFAULT_ARCHIVE):
        super().__init__(address, Handler)
        self.write_lock = Lock()
        self.store = store if store is not None else JsonCaseStore(ROOT / "database" / "cases")
        self.archive = archive


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        return

    def send_data(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type if content_type.startswith(("image/", "audio/", "video/"))
                         else content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def json_response(self, http_status, **values):
        self.send_data(http_status, json.dumps(values, ensure_ascii=False).encode("utf-8"), "application/json")

    def do_GET(self):
        if self.path == "/api/models":
            self.json_response(200, models=list(MANUALS))
        elif self.path == "/api/cases":
            browser_id = self.browser_id()
            history = self.server.store.history(browser_id) if browser_id else []
            self.json_response(200, cases=[self.summary(case) for case in history])
        elif self.path in ASSETS:
            filename, media = ASSETS[self.path]
            self.send_data(200, (ROOT / "frontend" / filename).read_bytes(), media)
        else:
            route = re.fullmatch(r"/api/cases/([a-f0-9-]{36})/media/([a-f0-9-]{36})", self.path)
            if route and all(IDENTIFIER.fullmatch(value) for value in route.groups()):
                if self.send_case_media(*route.groups()):
                    return
            self.json_response(404, error="페이지를 찾을 수 없어요.")

    def send_case_media(self, case_id, media_id):
        case = self.server.store.load(case_id) if self.browser_id() else None
        if not case or case.browser_id != self.browser_id():
            return False
        for attachment in case.attachments:
            if attachment["id"] != media_id or not attachment["mime"].startswith(("image/", "video/", "audio/")):
                continue
            folder = (self.server.store.directory / "media" / case_id).resolve()
            path = (self.server.store.directory / attachment["path"]).resolve()
            if path.parent != folder:
                return False
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                return False
            self.send_data(200, data, attachment["mime"])
            return True
        return False

    def browser_id(self):
        value = self.headers.get("X-SmartCare-Browser", "")
        return value if IDENTIFIER.fullmatch(value) else None

    @staticmethod
    def summary(case):
        return {key: getattr(case, key) for key in (
            "case_id", "model", "category", "device_id", "device_name", "error_code", "status",
            "created_at", "updated_at", "previous_case_id", "evidence_submitted_at",
        )}

    def save_agent(self, agent):
        agent.case.agent_state = {key: getattr(agent, key) for key in STATE_FIELDS}
        self.server.store.save(agent.case)

    def link_device_history(self, case, previous=None):
        related = []
        if case.browser_id and case.device_id:
            related = [item for item in self.server.store.history(case.browser_id)
                       if item.device_id == case.device_id and item.category == case.category
                       and item.model == case.model and item.case_id != case.case_id]
        if previous and all(item.case_id != previous.case_id for item in related):
            related.insert(0, previous)
        case.related_case_ids = [item.case_id for item in related]
        case.device_history = [
            {
                **self.summary(item),
                "symptoms": item.symptoms,
                "service_detail": item.service_detail,
                "actions": [
                    {key: action.get(key) for key in ("instruction", "performed", "result", "offered_at", "responded_at")}
                    for action in item.actions
                ],
            }
            for item in related
        ]

    def restore_agent(self, case):
        agent = SelfCareAgent(self.server.archive, model=case.model, web=True)
        agent.case = case
        for key in STATE_FIELDS:
            if key in case.agent_state:
                setattr(agent, key, case.agent_state[key])
        return agent

    def open_case(self, case):
        chain = [case]
        seen = {case.case_id}
        while chain[-1].previous_case_id and chain[-1].previous_case_id not in seen:
            previous = self.server.store.load(chain[-1].previous_case_id)
            if not previous or previous.browser_id != case.browser_id:
                break
            seen.add(previous.case_id)
            chain.append(previous)
        history = [message for item in reversed(chain) for message in item.messages]
        self.respond_with_case(self.restore_agent(case), None, history=history,
                               attachment_names=[a["name"] for a in case.attachments])

    def read_body(self, limit):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= limit:
            raise ValueError("제출 내용이 비어 있거나 크기 제한을 초과했어요.")
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("제출 내용을 모두 받지 못했어요.")
        return data

    def read_json(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("잘못된 요청 형식이에요.")
        payload = json.loads(self.body)
        if not isinstance(payload, dict):
            raise ValueError("잘못된 요청 형식이에요.")
        return payload

    def do_POST(self):
        port = self.server.server_port
        origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        public = urlsplit(os.environ.get("SMARTCARE_PUBLIC_ORIGIN", ""))
        if public.scheme == "https" and public.netloc and public.path in {"", "/"}:
            origins.add(f"https://{public.netloc}")
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        if not any(urlsplit(allowed).netloc == host and (not origin or origin == allowed) for allowed in origins):
            self.json_response(403, error="이 웹페이지에서 요청을 다시 시도해 주세요.")
            return
        try:
            self.body = self.read_body(MAX_FILE if self.path.endswith("/attachment") else 32_768)
            with self.server.write_lock:
                self.handle_post()
        except (json.JSONDecodeError, UnicodeError):
            self.json_response(400, error="잘못된 요청 형식이에요.")
        except ValueError as exc:
            self.json_response(400, error=str(exc))
        except OSError:
            self.json_response(500, error="상담 기록을 저장하지 못했어요.")

    def handle_post(self):
        if self.path == "/api/cases":
            payload = self.read_json()
            model = payload.get("model")
            if model is not None and (
                not isinstance(model, str) or not re.fullmatch(r"[A-Z0-9.-]{2,40}", model)
            ):
                raise ValueError("제품 라벨의 모델명을 확인해 주세요.")
            category, device_id, device_name = (payload.get("category", "washer"),
                                                payload.get("device_id"), payload.get("device_name"))
            if not isinstance(category, str) or category not in CATEGORIES:
                raise ValueError("제품 유형을 선택해 주세요.")
            if device_id is not None and (
                not isinstance(device_id, str) or not IDENTIFIER.fullmatch(device_id) or not self.browser_id()
            ):
                raise ValueError("등록된 기기를 다시 선택해 주세요.")
            if device_name is not None and (not isinstance(device_name, str) or len(device_name) > 60):
                raise ValueError("기기 이름은 60자까지 입력할 수 있어요.")
            self.start_case(model, category, device_id, device_name)
            return
        match = re.fullmatch(r"/api/cases/([a-f0-9-]{36})/(message|report|edit-report|attachment|finish-media|schedule|open|recur)", self.path)
        case = self.server.store.load(match[1]) if match and IDENTIFIER.fullmatch(match[1]) else None
        if case is None or case.browser_id != self.browser_id():
            self.json_response(404, error="상담을 찾을 수 없어요. 새 상담을 시작해 주세요.")
            return
        if match[2] == "open":
            self.open_case(case)
            return
        if match[2] == "recur":
            if case.status != "resolved":
                raise ValueError("해결된 상담에서만 재발 상담을 시작할 수 있어요.")
            self.start_case(case.model, case.category, case.device_id, case.device_name, previous=case)
            return
        agent = self.restore_agent(case)
        if match[2] == "message":
            text = self.read_json().get("text")
            if not isinstance(text, str) or not 0 < len(text.strip()) <= 3000:
                raise ValueError("메시지를 1~3,000자로 입력해 주세요.")
            if case.status in TERMINAL_STATES:
                raise ValueError("이 상담의 대화가 종료됐어요.")
            message = agent.handle(text.strip())
            self.save_agent(agent)
            self.respond_with_case(agent, message)
        elif match[2] == "report":
            if case.status != "collecting_report":
                raise ValueError("현재 상담에서 증상 기록을 요청하지 않았어요.")
            detail, fields = self.report_payload()
            case.service_detail = detail
            case.report_details = fields
            message = agent.report_saved()
            self.save_agent(agent)
            self.respond_with_case(agent, message)
        elif match[2] == "edit-report":
            if not case.service_detail:
                raise ValueError("먼저 증상 기록을 저장해 주세요.")
            detail, fields = self.report_payload()
            message = None
            if (detail, fields) != (case.service_detail, case.report_details):
                case.service_detail, case.report_details = detail, fields
                if not case.service_request and case.status != "service_submitted":
                    message = agent.reassess_report()
                self.save_agent(agent)
            self.respond_with_case(agent, message)
        elif match[2] == "finish-media":
            if case.status != "collecting_attachments":
                raise ValueError("증상 기록을 먼저 저장해 주세요.")
            self.read_json()
            case.media_collected_at = now()
            message = agent.reply(agent.offer_service(case.service_reason or "report_for_service", "기사님이 확인할 증상 기록을 저장했어요."))
            self.save_agent(agent)
            self.respond_with_case(agent, message)
        elif match[2] == "attachment":
            if case.status not in ATTACHMENT_STATES:
                raise ValueError("증상 자료는 A/S 연결 요청 전에 첨부할 수 있어요.")
            self.save_attachment(case)
        else:
            self.submit_schedule(agent)

    def submit_schedule(self, agent):
        case = agent.case
        if case.status not in {"service_requested", "service_submitted"}:
            raise ValueError("A/S 연결에 동의한 후 희망 일정을 제출해 주세요.")
        payload = self.read_json()
        if case.service_request:
            self.respond_with_case(agent, None)
            return
        date, time = payload.get("date"), payload.get("time")
        if (not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)
                or not isinstance(time, str) or not re.fullmatch(r"\d{2}:\d{2}", time)):
            raise ValueError("희망 방문 날짜와 시간을 선택해 주세요.")
        try:
            preferred = datetime.fromisoformat(f"{date}T{time}").replace(tzinfo=KST)
        except ValueError:
            raise ValueError("유효한 방문 날짜와 시간을 선택해 주세요.") from None
        if preferred <= datetime.now(KST):
            raise ValueError("현재 시각 이후의 방문 일정을 선택해 주세요.")
        phone = payload.get("phone", "")
        if not isinstance(phone, str) or (
            phone.strip() and (not re.fullmatch(r"[0-9+() -]{8,24}", phone.strip())
                               or not 8 <= len(re.sub(r"\D", "", phone)) <= 15)
        ):
            raise ValueError("연락받을 번호를 확인해 주세요.")
        # The local case is the handoff record until technician dispatch is integrated.
        case.service_request = {
            "request_id": str(uuid4()), "preferred_visit_at": preferred.isoformat(timespec="minutes"),
            "callback_phone": phone.strip() or None, "submitted_at": now(),
            "dispatch_status": "pending_integration",
        }
        previous_case = self.server.store.load(case.previous_case_id) if case.previous_case_id else None
        self.link_device_history(case, previous_case)
        case.status = "service_submitted"
        message = agent.reply("희망 방문 일정을 저장했어요. 아래에서 접수 내용을 확인해 주세요.")
        self.save_agent(agent)
        self.respond_with_case(agent, message)

    def start_case(self, model, category, device_id, device_name, previous=None):
        agent = SelfCareAgent(self.server.archive, model=model, web=True)
        case = agent.case
        case.browser_id = self.browser_id()
        case.category, case.device_id, case.device_name = category, device_id, device_name
        self.link_device_history(case, previous)
        if previous:
            case.previous_case_id = previous.case_id
            agent.reply("이전 상담에서 해결한 증상이 다시 발생한 것으로 기록했어요. 이번에 나타난 증상을 확인할게요.")
        message = agent.start()
        self.save_agent(agent)
        if previous:
            self.open_case(case)
        else:
            self.respond_with_case(agent, message)

    def report_payload(self):
        payload = self.read_json()
        detail = payload.get("detail")
        if not isinstance(detail, str) or not 0 < len(detail.strip()) <= 3000:
            raise ValueError("상세 증상을 1~3,000자로 입력해 주세요.")
        fields = {}
        for key in ("purchase_period", "symptom_category", "cycle_stage", "first_observed", "frequency", "prior_actions"):
            value = payload.get(key, "")
            if not isinstance(value, str) or len(value) > 500:
                raise ValueError("추가 정보는 항목당 500자까지 입력할 수 있어요.")
            if value.strip():
                fields[key] = value.strip()
        return detail.strip(), fields

    def save_attachment(self, case):
        if self.headers.get_content_type() != "application/octet-stream":
            raise ValueError("잘못된 파일 요청 형식이에요.")
        if len(case.attachments) >= MAX_FILES:
            raise ValueError("한 상담에 최대 5개까지 첨부할 수 있어요.")
        data = self.body
        detected = media_type(data)
        if detected is None:
            raise ValueError("지원하는 사진, 영상 또는 녹음 파일을 첨부해 주세요.")
        extension, mime = detected
        file_id = str(uuid4())
        relative = f"media/{case.case_id}/{file_id}.{extension}"
        path = self.server.store.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        original = unquote(self.headers.get("X-Filename", "")).replace("\\", "/").split("/")[-1]
        attachment = {"id": file_id, "name": original[:200] or path.name, "mime": mime,
                      "size": len(data), "path": relative, "uploaded_at": now()}
        case.attachments.append(attachment)
        try:
            self.server.store.save(case)
        except OSError:
            case.attachments.pop()
            path.unlink(missing_ok=True)
            raise
        self.json_response(201, attachment=attachment)

    def respond_with_case(self, agent, message, **extra):
        case = agent.case
        choices = {
            "checking_condition": ["네", "아니요", "확인 어려워요"],
            "awaiting_action_result": ["해결됐어요", "해봤지만 안 돼요", "아직 안 했어요", "조치하기 어려워요"],
            "service_offered": ["네", "아니요"],
        }.get(case.status, [])
        if case.status == "collecting":
            choices = ([*MANUAL_ALERTS, "코드 없음", "코드 확인 어려움"] if case.model
                       else [*MANUALS, "모델명 확인 어려움"]) if case.category == "washer" else ["모델명 확인 어려움"]
        if case.service_request:
            # The private callback number is available in the local handoff record only.
            extra["schedule"] = {key: value for key, value in case.service_request.items() if key != "callback_phone"}
        if case.status in {"resolved", "service_submitted", "service_declined"}:
            extra["customer_report"] = customer_report(case)
        if case.service_detail:
            extra.update(detail=case.service_detail, report_fields=case.report_details,
                         media=[{key: attachment[key] for key in ("id", "name", "mime")} for attachment in case.attachments])
        extra["can_attach"] = case.status in ATTACHMENT_STATES
        if message and case.messages and case.messages[-1].get("illustration"):
            extra["illustration"] = case.messages[-1]["illustration"]
        self.json_response(200, **self.summary(case), message=message, choices=choices,
                           related_count=len(case.related_case_ids), **extra)


if __name__ == "__main__":
    with SmartCareServer(("127.0.0.1", 8765)) as server:
        print("SmartCare 웹 상담: http://127.0.0.1:8765", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
