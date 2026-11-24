import os
from enum import Enum

from openai import OpenAI


class DemoTurn(Enum):
    START = "가상 고객에게 표시등을 관찰하도록 안내하세요. 관찰 전에는 표시등의 의미를 추정하지 마세요."
    STEADY = "가상 고객이 계속 켜져 있다고 답했습니다. 의미와 샘플 화면 갱신 조치를 안내하세요."
    BLINKING = "가상 고객이 표시등이 깜빡인다고 답했습니다. 화면에 표시된 코드와 표시등의 관찰 상태를 구분하여 기사 전달용으로 요약하고, 가상 매뉴얼상 의미를 설명하세요."
    UNKNOWN = "가상 고객이 표시등 상태를 확인하지 못했습니다. 의미는 미확인이라고 설명하고 기사 전달용 요약을 작성하세요."
    RESOLVED = "가상 고객이 계속 켜진 표시등을 보고 샘플 화면을 갱신한 후 코드가 사라졌다고 답했습니다. 상담을 마무리하세요."
    UNRESOLVED = "가상 고객이 계속 켜진 표시등을 보고 샘플 화면을 갱신했으나 코드가 남았습니다. 기사 점검을 제안하고 코드, 관찰 상태, 조치 결과를 요약하세요."


def generate_answer(*, turn: DemoTurn = DemoTurn.START) -> str:
    if type(turn) is not DemoTurn:
        raise ValueError("등록된 가상 시연 단계만 사용할 수 있습니다.")
    # The API boundary accepts a fixed demo branch, never a transcript or PDF excerpt.
    symptom = "가상 제품에서 샘플 코드 TEST-01이 표시됨"
    evidence = (
        "예시 문서 DEMO-01 1쪽. 가상 TEST-01의 표시등이 계속 켜져 있으면 샘플 화면의 상태 갱신 대기입니다. "
        "샘플 화면의 ‘새로고침’을 한 번 누르고 코드가 사라지는지 확인합니다. "
        "깜빡이면 가상 점검 요청 상태입니다. 추가 조치를 안내하지 않고 코드와 관찰 상태를 기사에게 전달할 요약을 작성합니다. "
        "표시 상태를 확인하지 못했으면 미확인으로 기록합니다. 실제 접수나 전달이 완료됐다고 말하지 않습니다."
    )
    if turn is DemoTurn.START:
        evidence = "예시 문서 DEMO-01 1쪽. 가상 TEST-01이 보이면 먼저 화면의 표시등을 관찰합니다."
    with OpenAI() as client:
        response = client.responses.create(
            model=os.environ.get("OPENAI_MODEL") or "gpt-4.1-mini",
            store=False,
            instructions=(
                "한국어 자가조치 챗봇의 가상 시연이다. "
                "제공된 예시 근거만 사용해 고객용 안내를 짧게 작성한다. "
                "예시에 없는 조치는 추가하지 않는다. 근거가 부족하면 점검을 권장한다. "
                "고객 응답 질문과 선택지는 화면에 별도로 나오므로 질문을 덧붙이지 않는다."
            ),
            input=f"증상: {symptom}\n테스트용 근거: {evidence}\n가상 대화: {turn.value}",
            max_output_tokens=500,
        )
    answer = response.output_text.strip()
    if not answer or response.status != "completed":
        raise ValueError("완료된 답변을 받지 못했습니다.")
    return answer
