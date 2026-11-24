import re
from pathlib import Path
from zipfile import BadZipFile

from pymupdf import FileDataError

from case_store import Case, now
from manual_search import MANUALS, explain_alert, reviewed_alert_steps, search_oe


MODEL_LABEL = "제품 라벨에서 ‘모델명’으로 표시된 값을 확인해 알려주시겠어요?"
VISIT_PROMPT = "기사님이 방문해서 제품을 점검하도록 신청할까요? ‘네’ 또는 ‘아니요’로 알려주세요."
CODE_PROMPT = "세탁기 조작부의 디지털 표시창에 에러코드가 있나요? 없으면 ‘코드 없음’이라고 답해 주세요."
OUTCOME_PROMPT = (
    "이 조치를 해보셨나요? ‘해결됐어요’, ‘해봤지만 안 돼요’, "
    "‘아직 안 했어요’, ‘건너뛸래요’ 중 하나로 알려주세요."
)
TERMINAL_STATES = {"resolved", "service_requested", "service_submitted", "service_declined"}
MODEL_PATTERN = re.compile(r"(?<![A-Z0-9])[A-Z]{1,3}[0-9][A-Z0-9-]{3,}(?![A-Z0-9])", re.I | re.ASCII)
CODE_PATTERN = re.compile(
    r"(?<![A-Z0-9])(?:[A-Z]E|DL|CL|\[L|FF|TCL|[A-Z]{1,2}[0-9]{1,2})(?![A-Z0-9])",
    re.I | re.ASCII,
)
HAZARD = re.compile(
    r"타는\s*냄새|탄\s*냄새|연기|불꽃|스파크|감전|누전|전기\s*작업|"
    r"배선|분해|누수|물이\s*새"
)
REPEAT = re.compile(r"다시\s*(?:알려|보여|말해|설명)|(?:이전|지난|직전|방금)\s*(?:안내|조치|단계)")
NOT_DONE = re.compile(r"아직\s*안|안\s*(?:했|해봤|해\s*봤)|못\s*(?:했|해봤|해\s*봤)|시도\s*전")
SKIPPED = re.compile(r"건너뛰|건너뛸|어려워|어렵|못\s*하겠|시도\s*불가")
UNRESOLVED = re.compile(r"미해결|안\s*(?:돼|되|됐|해결)|해결\s*(?:안|되지)|여전히|그대로|실패|똑같")
RESOLVED = re.compile(r"해결\s*(?:됐|되었|됨)|이제\s*(?:돼|되)|정상\s*(?:이에요|입니다|작동)|고쳐졌")
TRIED = re.compile(r"해\s*봤|했어|했습|했는데|해\s*보았|시도했|수행했|조치했")

# Match only reviewed customer-facing instructions in the retrieved OE row.
# New or changed manual wording stays out of the dialogue until reviewed.
REVIEWED_ACTIONS = {
    "배수 호스를 내리세요.": ("lower_hose", "배수 호스가 올려져 있나요?"),
    "배수 호스를 내려주세요.": ("lower_hose", "배수 호스가 올려져 있나요?"),
    "배수 호스가 꺾여 굴곡이 생기면 이물질로 인해서 배수가 되지 않으므로 바닥이 고른 장소에 바르게 놓으세요.": (
        "straighten_hose", "배수 호스가 꺾여 있거나 문턱이 높아 내부가 막혀 있나요?"
    ),
}
HOSE_SYMPTOMS = {
    "lower_hose": re.compile(r"호스.{0,16}(?:올려|올라|높게|위에)"),
    "straighten_hose": re.compile(r"호스.{0,16}(?:꺾|접혀|접혔|구부러)"),
}
SYMPTOM_MANUAL_SECTIONS = (
    ("OE", re.compile(r"배수.{0,12}(?:안\s*돼|안\s*되|멈|못)|물이\s*(?:안\s*빠|빠지지)")),
    ("IE", re.compile(r"급수.{0,12}(?:안\s*돼|안\s*되|멈|못)|물이\s*(?:안\s*들어|들어오지)")),
    ("DE", re.compile(r"(?:문|뚜껑).{0,10}(?:안\s*닫|닫히지)")),
    ("UE", re.compile(r"세탁물.{0,12}(?:한쪽.{0,8}치우|뭉쳤|뭉쳐)")),
)


def observed_hose_conditions(text: str) -> set[str]:
    if re.search(r"없|않|안\s*(?:올|꺾|접)|모르", text):
        return set()
    return {key for key, pattern in HOSE_SYMPTOMS.items() if pattern.search(text)}


def referenced_actions(archive: Path, model: str) -> list[dict]:
    document_id, rows = search_oe(archive, model)
    actions = {}
    for page, excerpt in rows:
        for match in re.finditer(r"^•\s*(.+?(?:\n[^•\n][^\n]*)*?)\.", excerpt, re.MULTILINE):
            # PDF extraction can wrap the middle of the word '장소'.
            instruction = " ".join(match.group(1).split()).replace("장 소", "장소") + "."
            reviewed = REVIEWED_ACTIONS.get(instruction)
            if reviewed and reviewed[1] in " ".join(excerpt[:match.start()].split()):
                action_id, condition = reviewed
                start = excerpt.rfind(condition, 0, match.start())
                actions.setdefault(action_id, {
                    "action_id": action_id,
                    "condition": condition,
                    "instruction": instruction,
                    "source": {
                        "manual_name": f"LG 세탁기 사용설명서 {model}",
                        "filename": MANUALS[model][0],
                        "document_id": document_id,
                        "pdf_page": page,
                        "excerpt": excerpt[start:match.end()] if start >= 0 else match.group(),
                    },
                })
    return list(actions.values())


class SelfCareAgent:
    def __init__(self, archive: Path, model: str | None = None, *, web: bool = False):
        self.case = Case(model=model)
        self.archive = archive
        self.pending = []
        self.condition = None
        self.asked_model = False
        self.asked_code = False
        self.results_only = False
        self.no_displayed_code = False
        self.web = web

    def start(self) -> str:
        if self.web:
            if self.case.category != "washer" and self.case.model:
                self.case.status = "collecting_report"
                return self.reply(f"{self.case.model}의 증상을 기록해 주세요. 이 제품 유형의 자가조치 매뉴얼은 아직 등록되지 않았어요.")
            if not self.case.model:
                self.asked_model = True
                return self.reply(MODEL_LABEL)
            self.asked_code = True
            return self.reply(f"저장된 {self.case.model} 세탁기로 상담을 시작합니다. " + CODE_PROMPT)
        if self.case.model:
            return self.reply(f"등록된 {self.case.model} 세탁기로 상담을 시작합니다. 증상이나 화면의 에러코드를 알려주세요.")
        return self.reply("세탁기에 어떤 문제가 있나요? 증상이나 화면의 에러코드를 알려주세요.")

    def reply(self, message: str) -> str:
        self.case.add_message("agent", message)
        if self.web and self.case.category == "washer":
            illustration = None
            if CODE_PROMPT in message and self.case.status == "collecting":
                illustration = "washer-error-display"
            elif self.case.status == "awaiting_action_result" and self.case.actions:
                action = self.case.actions[-1]
                # Attach a photo only to the actual instruction, not to later result prompts.
                if action["instruction"] in message:
                    illustration = {
                        "lower_hose": "drain-hose-down",
                        "straighten_hose": "drain-hose-straight",
                        "balance_load": "balance-laundry",
                        "spread_tangled_load": "balance-laundry",
                        "close_door": "washer-door",
                        "reclose_door": "washer-door",
                        "open_tap": "inlet-tap",
                    }.get(action["action_id"])
            if illustration:
                self.case.messages[-1]["illustration"] = illustration
        return message

    def handle(self, text: str) -> str:
        self.case.add_message("customer", text)
        if self.case.status in TERMINAL_STATES:
            return self.reply("이 상담은 종료되었습니다.")
        if self.case.status == "collecting_report":
            return self.reply("아래 양식에 상세 증상을 기록해 주세요.")
        if self.case.status == "service_offered":
            answer = text.strip(" .!?")
            if answer in {"네", "예", "응", "웅", "연결해 주세요", "연결해주세요"}:
                self.case.status = "service_requested"
                self.case.service_consent_at = now()
                if self.web:
                    return self.reply("기사 방문을 희망하는 날짜와 시간을 선택해 주세요.")
                return self.reply("A/S 연결 요청을 기록했어요. 실제 접수는 아직 진행되지 않았어요.")
            if answer in {"아니", "아니요", "괜찮아요"}:
                self.case.status = "service_declined"
                return self.reply("A/S 연결 없이 상담을 마칠게요.")
            return self.reply(VISIT_PROMPT)
        if HAZARD.search(text):
            self.case.symptoms.append(text)
            return self.reply(self.offer_service(
                "safety_concern", "안전 점검이 필요한 증상입니다. 사용을 중지해 주세요."
            ))
        if self.case.status in {"checking_condition", "awaiting_action_result"}:
            models = {model.upper() for model in MODEL_PATTERN.findall(text)}
            if models - {self.case.model}:
                return self.reply(self.offer_service(
                    "model_changed",
                    "다른 모델명이 확인되어 현재 안내를 중단했어요. 정확한 모델명으로 새 상담을 시작해 주세요.",
                ))
        if REPEAT.search(text) and self.case.status == "awaiting_action_result":
            return self.reply(self.describe(self.case.actions[-1], repeat=True))
        if self.case.status == "checking_condition":
            return self.reply(self.check_condition(text))
        if self.case.status == "awaiting_action_result":
            return self.reply(self.record_result(text))
        return self.reply(self.collect(text))

    def collect(self, text: str) -> str:
        case = self.case
        models = {model.upper() for model in MODEL_PATTERN.findall(text)}
        if case.device_id and models - {case.model}:
            return "현재 상담은 선택한 기기로 진행 중이에요. 다른 기기라면 새 상담에서 선택해 주세요."
        if len(models) == 1:
            case.model = models.pop()
        elif self.web and not case.model and re.fullmatch(r"[A-Z0-9.-]{2,40}", text, re.I | re.ASCII):
            case.model = text.upper()
        codes = {code.upper() for code in CODE_PATTERN.findall(text)}
        if len(codes) == 1:
            detected = codes.pop()
            case.error_code = "CL" if detected == "[L" else detected
            case.display_code_status = "reported"
        elif self.web and self.asked_code and re.fullmatch(r"[A-Z0-9-]{2,12}", text, re.I | re.ASCII):
            case.error_code = text.upper()
            case.display_code_status = "reported"
        if re.search(r"코드\s*(?:는\s*)?(?:없|안\s*보)", text) or text == "코드 확인 어려움":
            self.no_displayed_code = True
            case.error_code = None
            case.display_code_status = "unconfirmed" if text == "코드 확인 어려움" else "not_displayed"
        symptom = MODEL_PATTERN.sub("", text).strip()
        if symptom and not re.fullmatch(r"(?:모델(?:명)?\s*(?:은|이)?\s*)?(?:이에요|예요|입니다)?[.! ]*", symptom):
            case.symptoms.append(text)
        if not case.model:
            if self.web and text == "모델명 확인 어려움":
                if self.no_displayed_code:
                    case.status = "collecting_report"
                    return "에러코드 없이 증상을 판단할게요. 모델명은 미확인으로 두고 상세 증상을 기록해 주세요."
                case.status = "collecting_report"
                return "모델명은 미확인으로 두고 서비스센터가 확인할 증상을 기록해 주세요."
            if self.web:
                return MODEL_LABEL
            if self.asked_model:
                return self.offer_service(
                    "model_unconfirmed", "모델을 확인해야 올바른 매뉴얼을 찾을 수 있어요."
                )
            self.asked_model = True
            return MODEL_LABEL
        if self.web and case.category != "washer":
            case.status = "collecting_report"
            return "이 제품 유형의 자가조치 매뉴얼은 아직 등록되지 않았어요. 서비스센터가 확인할 증상을 기록해 주세요."
        if self.web and (self.no_displayed_code or (self.asked_code and case.error_code is None)):
            case.display_code_status = case.display_code_status or "unconfirmed"
            case.status = "collecting_report"
            return (
                "에러코드 없이 증상을 판단할게요. 문제가 나타난 상황을 적어주시면 "
                "매뉴얼에서 적용할 수 있는 자가조치를 확인할게요."
            )
        if self.web and case.error_code is None and not self.asked_code:
            self.asked_code = True
            return CODE_PROMPT
        if self.web and case.model not in MANUALS:
            case.status = "collecting_report"
            return f"{case.model} 모델의 매뉴얼은 아직 등록되지 않았어요. 서비스센터가 확인할 증상을 기록해 주세요."
        if self.web and case.error_code:
            explanation = explain_alert(case.model, case.error_code)
            if explanation is None:
                explanation = f"{case.error_code} · 이 모델의 매뉴얼에서 코드의 의미를 확인하지 못했어요."
            return explanation + "\n" + self.manual_guidance(" ".join(case.symptoms))
        if case.model not in MANUALS:
            return self.offer_service("unregistered_model", f"{case.model} 모델의 매뉴얼은 아직 등록되지 않았어요.")

        symptoms = " ".join(case.symptoms)
        observed = observed_hose_conditions(symptoms)
        if case.error_code != "OE" and not (case.error_code is None and observed):
            if not self.asked_code and not self.no_displayed_code:
                self.asked_code = True
                return CODE_PROMPT if case.error_code is None else (
                    f"말씀하신 {case.error_code}의 조치 근거를 확인하기 어려워요. "
                    "화면에 표시된 코드를 한 번 확인해 주시겠어요?"
                )
            return self.offer_service("no_matching_evidence", "확인한 증상에 맞는 자가조치 근거를 찾지 못했어요.")
        try:
            candidates = referenced_actions(self.archive, case.model)
        except (OSError, KeyError, BadZipFile, FileDataError):
            return self.offer_service("manual_unavailable", "현재 해당 모델의 매뉴얼을 확인할 수 없어요.")
        self.pending = [
            action for action in candidates if case.error_code == "OE" or action["action_id"] in observed
        ]
        if not self.pending and not self.asked_code:
            self.asked_code = True
            return "관련 조치를 확인할 수 없어요. 문제가 발생한 시점이 세탁, 헹굼, 탈수 중 언제였나요?"
        return self.next_action(observed)

    def check_condition(self, text: str) -> str:
        action = self.condition
        answer = text.strip(" .!?")
        if SKIPPED.search(text) or re.search(r"모르|몰라|확인\s*(?:못|안)", text):
            self.record_check(action, "unconfirmed", text)
            if self.web and self.pending:
                return "확인하기 어려운 항목은 넘기고 다음 항목을 확인할게요.\n" + self.next_action()
            return self.request_report_or_service("condition_unconfirmed", "매뉴얼의 점검 조건을 확인하기 어려워요.")
        hose_pattern = HOSE_SYMPTOMS.get(action["action_id"])
        if answer in {"아니", "아니요"} or (hose_pattern and re.search(r"없|안\s*(?:올|꺾|접)|않", text)):
            self.record_check(action, "not_applicable", text)
            return self.next_action()
        if answer in {"네", "예", "응", "웅", "맞아요"} or (hose_pattern and hose_pattern.search(text)):
            self.record_check(action, "confirmed", text)
            return self.offer_action(action)
        return action["condition"] + " ‘네’, ‘아니요’, ‘확인 어려워요’ 중 하나로 알려주세요."

    def record_check(self, action: dict, result: str, response: str) -> None:
        self.case.checks.append({
            "action_id": action["action_id"],
            "question": action["condition"],
            "source": action["source"],
            "result": result,
            "customer_response": response,
            "at": now(),
            "symptom_revision": self.case.symptom_revision,
        })

    def record_result(self, text: str) -> str:
        action = self.case.actions[-1]
        short_answer = text.strip(" .!?")
        result = None
        if self.results_only and short_answer in {"네", "예", "응", "웅", "아니", "아니요"}:
            result = "unresolved" if short_answer.startswith("아니") else "resolved"
            performed = True
        elif SKIPPED.search(text):
            result, performed = "skipped", False
        elif NOT_DONE.search(text):
            action.update(performed=False, result="not_attempted", customer_response=text, responded_at=now())
            self.results_only = False
            return "아직 수행하지 않은 것으로 기록했어요. " + OUTCOME_PROMPT
        elif UNRESOLVED.search(text) and not TRIED.search(text) and action["performed"] is not True:
            self.results_only = False
            return "현재 안내한 조치를 직접 해보셨나요? ‘네’ 또는 ‘아니요’로 알려주세요."
        elif UNRESOLVED.search(text):
            result, performed = "unresolved", True
        elif RESOLVED.search(text):
            result = "resolved"
            performed = True if TRIED.search(text) else action["performed"]
        elif TRIED.search(text) or short_answer in {"네", "예", "응", "웅"}:
            if self.results_only:
                return "문제가 해결됐다면 ‘해결됐어요’, 계속되면 ‘해봤지만 안 돼요’라고 알려주세요."
            action.update(performed=True, customer_response=text, responded_at=now())
            self.results_only = True
            return "수행한 것으로 기록했어요. 이제 문제가 해결됐나요?"
        elif short_answer in {"아니", "아니요"}:
            action.update(performed=False, result="not_attempted", customer_response=text, responded_at=now())
            return "아직 수행하지 않은 것으로 기록했어요. " + OUTCOME_PROMPT
        if result is None:
            return OUTCOME_PROMPT
        action.update(performed=performed, result=result, customer_response=text, responded_at=now())
        self.results_only = False
        if result == "resolved":
            self.case.status = "resolved"
            return "문제가 해결된 것으로 기록했어요. 상담을 마칠게요."
        if result == "skipped":
            detail = "자가조치를 수행하기 어려운 것으로 기록했어요."
            if self.web and self.pending:
                return detail + "\n" + self.next_action()
            return self.request_report_or_service("action_difficult", detail)
        confirmation = "조치를 수행했지만 문제가 계속되는 것으로 기록했어요."
        return confirmation + "\n" + self.next_action()

    def next_action(self, observed=()) -> str:
        if not self.pending:
            has_current_actions = any(item.get("symptom_revision", 0) == self.case.symptom_revision
                                      for item in self.case.actions)
            reason = "actions_exhausted" if has_current_actions else "no_matching_evidence"
            return self.request_report_or_service(reason, "추가로 안내할 수 있는 자가조치 근거가 없어요.")
        action = self.pending.pop(0)
        if action["action_id"] in observed:
            self.record_check(action, "already_reported", "\n".join(self.case.symptoms))
            return self.offer_action(action)
        self.condition = action
        self.case.status = "checking_condition"
        source = action["source"]
        return (
            f"매뉴얼에 따라 먼저 확인할게요. {action['condition']}\n"
            f"출처: {source['manual_name']} ({source['document_id']}), PDF {source['pdf_page']}쪽\n"
            "‘네’, ‘아니요’, ‘확인 어려워요’ 중 하나로 알려주세요."
        )

    def offer_action(self, action: dict) -> str:
        self.condition = None
        action.update(performed=None, result="pending", offered_at=now(), symptom_revision=self.case.symptom_revision)
        self.case.actions.append(action)
        self.case.status = "awaiting_action_result"
        return self.describe(action)

    def offer_service(self, reason: str, detail: str) -> str:
        self.case.status = "service_offered"
        self.case.service_reason = reason
        return f"{detail} {VISIT_PROMPT}"

    def request_report_or_service(self, reason: str, detail: str) -> str:
        if self.web and not self.case.media_collected_at and not self.case.attachments:
            self.case.service_reason = reason
            if not self.case.service_detail:
                self.case.status = "collecting_report"
                return detail + "\n증상을 아래에 기록해 주세요."
            self.case.status = "collecting_attachments"
            return detail + "\n기사님이 확인할 사진, 영상 또는 녹음이 있으면 첨부해 주세요."
        return self.offer_service(reason, detail)

    def reassess_report(self) -> str:
        case = self.case
        # Preserve earlier attempts while restarting the checks for the corrected symptoms.
        for action in case.actions:
            if action["result"] in {"pending", "not_attempted"}:
                action["result"] = "superseded"
        case.symptom_revision += 1
        case.service_reason = None
        case.service_consent_at = None
        case.media_collected_at = None
        self.condition = None
        self.pending = []
        self.results_only = False
        return self.report_saved("수정한 증상으로 다시 확인할게요.\n")

    def report_saved(self, prefix: str = "") -> str:
        detail = self.case.service_detail or ""
        if HAZARD.search(detail):
            return self.reply(prefix + self.request_report_or_service("safety_concern", "안전 점검이 필요한 증상입니다. 사용을 중지해 주세요."))
        if self.case.service_reason:
            return self.reply(prefix + self.request_report_or_service(
                self.case.service_reason, "추가 점검에 필요한 상세 증상을 기록했어요."
            ))
        return self.reply(prefix + self.manual_guidance(detail))

    def manual_guidance(self, detail: str) -> str:
        self.pending = []
        observed = observed_hose_conditions(detail)
        code = self.case.error_code
        # A symptom may suggest a manual section without being a reported code.
        matches = [section for section, pattern in SYMPTOM_MANUAL_SECTIONS if pattern.search(detail)]
        section = code or (matches[0] if len(matches) == 1 else None)
        if self.case.category == "washer" and self.case.model in MANUALS and (section or observed):
            try:
                if section in {None, "OE"}:
                    self.pending = [action for action in referenced_actions(self.archive, self.case.model)
                                    if section == "OE" or action["action_id"] in observed]
                else:
                    self.pending = reviewed_alert_steps(self.archive, self.case.model, section)
            except (OSError, KeyError, BadZipFile, FileDataError, IndexError):
                self.pending = []
            if self.pending:
                return "매뉴얼에 나온 점검 항목을 하나씩 확인할게요.\n" + self.next_action()
        return self.request_report_or_service(
            "no_matching_evidence", "적용할 수 있는 자가조치 근거를 찾지 못했어요."
        )

    def describe(self, action: dict, *, repeat: bool = False) -> str:
        source = action["source"]
        step = sum(item.get("symptom_revision", 0) == self.case.symptom_revision for item in self.case.actions)
        label = "직전 안내" if repeat else f"자가조치 {step}단계"
        return (
            f"{label}. {action['instruction']}\n"
            f"출처: {source['manual_name']} ({source['document_id']}), PDF {source['pdf_page']}쪽\n"
            f"참고 원문:\n{source['excerpt']}\n{OUTCOME_PROMPT}"
        )
