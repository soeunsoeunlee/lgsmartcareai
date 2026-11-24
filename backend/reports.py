"""Customer and technician views of the same consultation record."""

from case_store import Case
from agent import SYMPTOM_MANUAL_SECTIONS, observed_hose_conditions
from manual_search import explain_alert, source_location


LG_SUPPORT = "https://www.lge.co.kr/support/solutions-20152965504689"
WASHER_CAUSES = {
    "OE": ("배수 경로 이상 가능성", "배수 호스가 눌리거나 꺾였는지, 막힘이 있는지 확인할 필요가 있어요."),
    "IE": ("급수 흐름 이상 가능성", "수도꼭지나 급수 호스 상태 때문에 세탁통에 필요한 물이 들어오지 않았을 수 있어요."),
    "UE": ("세탁물 불균형 가능성", "탈수 중 세탁물이 한쪽에 몰려 균형을 잡기 어려웠을 수 있어요."),
    "DE": ("문 닫힘 상태 확인 필요", "문이 덜 닫혔거나 문 사이에 이물이 끼었을 수 있어요."),
    "DL": ("문 잠금 상태 확인 필요", "문이 닫혀 보이더라도 잠금장치가 정상적으로 작동하지 않았을 수 있어요."),
    "LE1": ("세탁통 회전 방해 가능성", "세탁물의 양이나 이물질로 인해 회전이 어려웠을 수 있어요."),
    "LE": ("세탁통 회전 계통 확인 필요", "공식 지원 자료에서는 회전이 원활하지 않을 때 표시될 수 있다고 안내해요."),
    "AE": ("세탁통 회전 계통 확인 필요", "공식 지원 자료에서는 회전 구동부가 제대로 작동하지 않을 때 표시될 수 있다고 안내해요."),
    "FE": ("과급수 감지 계통 확인 필요", "공식 지원 자료에서는 물이 과하게 들어오는 상태가 감지될 때 표시될 수 있다고 안내해요."),
    "TE": ("온도 감지 계통 확인 필요", "공식 지원 자료에서 온도 감지 관련 표시로 안내해요."),
    "E3": ("배수 동작 확인 필요", "공식 지원 자료에서 배수 모터 동작과 관련된 표시로 안내해요."),
    "FF": ("급수·배수 경로 동결 가능성", "저온에 노출되었다면 물이 지나가는 부분이 얼었을 수 있어요."),
    "CL": ("버튼 잠금 표시", "해당 표시는 버튼 잠금 기능이 켜진 상태에서 나타날 수 있어요."),
    "TCL": ("통세척 시기 알림", "해당 표시는 세탁통을 청소할 시기를 알려줘요."),
}


def assessment_for(case: Case, symptom: str, archive=None) -> dict:
    reported_code = case.error_code
    observation = case.service_detail
    findings = {"observation": observation} if observation else {}
    basis = (f"고객이 기록한 표시 문구 ‘{case.display_label}’" if case.display_label else
             f"고객이 기록한 표시 코드 {reported_code}" if reported_code else "고객이 기록한 증상")
    matches = {key for key, pattern in SYMPTOM_MANUAL_SECTIONS if pattern.search(symptom)}
    if observed_hose_conditions(symptom):
        matches.add("OE")
    code = reported_code or (next(iter(matches)) if len(matches) == 1 else None)
    if case.service_reason == "safety_concern":
        return {**findings, "title": "안전 점검 필요", "explanation": "기록된 증상은 기사 점검이 필요해요.",
                "basis": "증상 기록에 안전 점검이 필요한 내용이 포함되어 있어요."}
    if case.category != "washer" or code not in WASHER_CAUSES or not case.model or not explain_alert(case.model, code, archive):
        return {**findings, "title": "가능한 원인을 좁히기 어려워요",
                "explanation": "현재 확인된 모델과 코드 또는 증상만으로는 원인을 구분하기 어려워요.", "basis": basis}
    if not explain_alert(case.model, code):
        findings["manual_note"] = explain_alert(case.model, code, archive)
    else:
        findings["source_url"] = LG_SUPPORT
    related = {"DL": {"DE"}, "E3": {"OE"}, "FF": {"OE", "IE"}}
    compatible = {code} | related.get(code, set())
    if reported_code and matches and matches - compatible:
        return {
            **findings, "title": "코드와 증상을 함께 점검해야 해요",
            "explanation": "입력하신 증상에 표시 코드만으로 설명하기 어려운 내용이 있어요. 두 정보를 함께 확인해야 원인을 좁힐 수 있어요.",
            "basis": basis + "와 직접 입력한 증상을 매뉴얼 항목과 대조했어요.",
        }
    if reported_code and matches:
        basis += "와 입력한 증상이 같은 매뉴얼 점검 항목에 해당해요."
    elif reported_code and observation:
        basis += "를 기준으로 정리했어요. 입력한 증상만으로는 원인을 더 좁히기 어려워요."
    elif not reported_code:
        basis = "입력한 증상과 일치하는 매뉴얼 점검 항목을 참고했어요."
    title, explanation = WASHER_CAUSES[code]
    return {**findings, "title": title, "explanation": explanation, "basis": basis}


def customer_report(case: Case, archive=None) -> dict:
    request = case.service_request or {}
    symptom = case.service_detail or " / ".join(case.symptoms)
    return {
        "case_id": case.case_id,
        "model": case.model,
        "error_code": case.error_code,
        "display_label": case.display_label,
        "display_code_status": case.display_code_status,
        "symptom": symptom,
        "assessment": assessment_for(case, symptom, archive),
        "first_observed": case.report_details.get("first_observed"),
        "cycle_stage": case.report_details.get("cycle_stage"),
        "frequency": case.report_details.get("frequency"),
        "confirmed_checks": [item["question"] for item in case.checks
                             if item["result"] in {"confirmed", "already_reported"}
                             and item.get("symptom_revision", 0) == case.symptom_revision],
        "manual_references": list(dict.fromkeys(
            f"{item['source']['document_id']} {source_location(item['source'])}"
            for item in [*case.checks, *case.actions] if item.get("source")
        )),
        "actions": [{"description": item["instruction"], "result": item["result"],
                     "previous": item.get("symptom_revision", 0) < case.symptom_revision} for item in case.actions],
        "status": case.status,
        "preferred_visit_at": request.get("preferred_visit_at"),
        "submitted_at": request.get("submitted_at"),
    }


def technician_report(case: Case, archive=None) -> dict:
    """Internal projection for a future authenticated technician service."""
    return {
        "schema_version": 1,
        "summary": customer_report(case, archive),
        "device": {"id": case.device_id, "name": case.device_name, "category": case.category},
        "created_at": case.created_at,
        "updated_at": case.updated_at,
        "customer_symptoms": case.symptoms,
        "symptom_details": case.report_details,
        "manual_checks": case.checks,
        "action_history": case.actions,
        "conversation": case.messages,
        "attachments": case.attachments,
        "previous_case_id": case.previous_case_id,
        "device_history": case.device_history,
        "service_reason": case.service_reason,
        "service_request": case.service_request,
    }
