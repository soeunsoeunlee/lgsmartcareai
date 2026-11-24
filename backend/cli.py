import argparse
import os
import sys
from pathlib import Path
from zipfile import BadZipFile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = ROOT / "데이터" / "세탁기 매뉴얼" / "manuals.zip"
SERVICE_OFFER = "자가조치 안내 근거를 찾지 못했습니다. A/S 상담을 권장합니다."


def local_search(args: argparse.Namespace) -> int:
    from manual_search import MANUALS, search_oe
    from pymupdf import FileDataError

    model = args.model
    while not model or not model.strip():
        model = input("제품 라벨에서 ‘모델명’ 값을 찾아 입력해 주세요: ")
    model = model.strip().upper()
    if model not in MANUALS:
        print("이 모델의 매뉴얼은 아직 등록되지 않았습니다. A/S 상담을 권장합니다.")
        return 0
    error_code = args.error or input("제품 화면에 표시된 에러코드를 입력해 주세요: ")
    if error_code.strip().upper() != "OE":
        print("현재 검증 범위는 OE 배수 오류입니다. " + SERVICE_OFFER)
        return 0
    try:
        document_id, results = search_oe(args.archive, model)
    except (OSError, KeyError, BadZipFile, FileDataError):
        print("등록된 PDF를 읽을 수 없습니다. ZIP 경로와 파일 구성을 확인해 주세요.", file=sys.stderr)
        return 1
    if not results:
        input("배수가 멈춘 상황을 알려주세요. 예: 세탁 중, 탈수 중: ")
        print(SERVICE_OFFER)
        return 0
    for page, excerpt in results:
        print(f"\n{model} | {document_id} | PDF {page}쪽\n{excerpt}")
    return 0


def run_chat(args: argparse.Namespace) -> int:
    from agent import TERMINAL_STATES, SelfCareAgent
    from case_store import JsonCaseStore

    agent = SelfCareAgent(args.archive)
    store = JsonCaseStore(args.case_dir)
    message = agent.start()
    try:
        while True:
            record = store.save(agent.case)
            print(f"AI: {message}")
            if agent.case.status in TERMINAL_STATES:
                print(f"상담 기록: {record}")
                return 0
            text = input("고객: ").strip()
            if text == "/종료":
                print(f"상담 기록: {record}")
                return 0
            if text:
                message = agent.handle(text)
    except OSError:
        print("상담 기록을 저장할 수 없습니다. 저장 경로를 확인해 주세요.", file=sys.stderr)
        return 1


def openai_demo(args: argparse.Namespace) -> int:
    from dotenv import load_dotenv
    from openai import OpenAIError

    from llm import DemoTurn, generate_answer

    load_dotenv(ROOT / ".env")
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        print("프로젝트 루트의 .env에 OPENAI_API_KEY를 설정해 주세요.", file=sys.stderr)
        return 1
    # Keep this path independent of local documents, CLI inputs and chat history.
    try:
        answer = generate_answer()
        print(f"가상 시연 | 출처: DEMO-01 1쪽\n{answer}")
        if getattr(args, "interactive", False):
            turn = demo_choice(
                "가상 표시등이 어떻게 보이나요? (1 계속 켜짐, 2 깜빡임, 3 확인 못함, 0 종료): ",
                {"1": DemoTurn.STEADY, "2": DemoTurn.BLINKING, "3": DemoTurn.UNKNOWN},
            )
            if turn is not None:
                print(f"AI: {generate_answer(turn=turn)}")
            if turn is DemoTurn.STEADY:
                outcome = demo_choice(
                    "샘플 화면을 새로고침한 뒤 TEST-01 코드가 사라졌나요? (1 사라짐, 2 계속 표시됨, 0 종료): ",
                    {"1": DemoTurn.RESOLVED, "2": DemoTurn.UNRESOLVED},
                )
                if outcome is not None:
                    print(f"AI: {generate_answer(turn=outcome)}")
    except (OpenAIError, ValueError) as exc:
        print(f"OpenAI 응답 생성 실패 ({type(exc).__name__}).", file=sys.stderr)
        return 1
    return 0


def demo_choice(prompt, choices):
    while True:
        selected = input(prompt).strip()
        if selected == "0":
            return None
        if selected in choices:
            return choices[selected]
        print("화면에 나온 번호를 입력해 주세요.")


def main() -> int:
    parser = argparse.ArgumentParser(description="SmartCare 세탁기 자가조치 MVP")
    commands = parser.add_subparsers(required=True)
    local = commands.add_parser("local", help="실제 매뉴얼에서 모델별 OE 항목 검색")
    local.add_argument("--model", help="제품 라벨의 모델명")
    local.add_argument("--error", help="에러코드")
    local.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE, help="매뉴얼 ZIP 경로")
    local.set_defaults(run=local_search)
    chat = commands.add_parser("chat", help="실제 매뉴얼을 사용해 로컬 상담")
    chat.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE, help="매뉴얼 ZIP 경로")
    chat.add_argument("--case-dir", type=Path, default=ROOT / "database" / "cases", help="상담 JSON 저장 경로")
    chat.set_defaults(run=run_chat)
    demo = commands.add_parser("openai-demo", help="개발자용 OpenAI 연결 점검")
    demo.add_argument("--interactive", action="store_true", help="가상 고객 응답을 선택해 대화 시연")
    demo.set_defaults(run=openai_demo)
    args = parser.parse_args()
    try:
        return args.run(args)
    except (EOFError, KeyboardInterrupt):
        print("\n입력이 취소되었습니다.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
