import json
import os
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

from agent import SelfCareAgent, referenced_actions
from case_store import JsonCaseStore
from cli import DEFAULT_ARCHIVE, local_search, openai_demo, run_chat
from llm import DemoTurn, generate_answer, rank_public_support
from manual_search import MANUALS, reviewed_alert_steps
from support_knowledge import official_code_reference


class LocalAgentTests(unittest.TestCase):
    def setUp(self):
        self.agent = SelfCareAgent(DEFAULT_ARCHIVE)
        self.agent.start()

    def test_official_support_adds_no_code_spin_guidance_only_in_hybrid_mode(self):
        public_url = "https://www.lge.co.kr/support/solutions-20150465968160"
        rows = {"solutions-20150465968160_main": {"source_url": public_url, "product_type": "top_loader",
                                                 "content": "선반이나 수납장이 세탁기에 부딪치는지 확인합니다."}}
        with (patch.dict(os.environ, {"OPENAI_API_KEY": ""}),
              patch("support_knowledge.v6_for", return_value={}),
              patch("support_knowledge.knowledge_for", return_value=rows)):
            for mode, status in [("manual_only", "collecting_attachments"), ("hybrid", "checking_condition")]:
                agent = SelfCareAgent(DEFAULT_ARCHIVE, model="TR14WK1", web=True)
                agent.case.guidance_mode = mode
                agent.start()
                agent.handle("코드 없음")
                agent.case.service_detail = "탈수할 때 쿵쿵 소리가 나요."
                message = agent.report_saved()
                self.assertEqual(agent.case.status, status)
                if mode == "hybrid":
                    self.assertIn(public_url, message)
                    self.assertIn("가벼운 물건을 치워", agent.handle("네"))

    def test_v6_zip_supplies_new_type_specific_guidance_and_code_reference(self):
        source = "solutions-1779828"
        url = f"https://www.lge.co.kr/support/{source}"
        row = {"source_id": source, "source_url": url, "products": ["드럼세탁기"],
               "title": "[LG 드럼세탁기] IE 에러가 떠요", "content_status": "text",
               "content": "수도꼭지가 제대로 열려 있는지 먼저 확인해 주세요."}
        with TemporaryDirectory() as directory, patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            root = Path(directory)
            with ZipFile(root / "lg_smartcare_v6_full_results (1).zip", "w") as bundle:
                bundle.writestr("knowledge.json", json.dumps([row]))
            archive = root / "manuals" / "manuals.zip"
            agent = SelfCareAgent(archive, web=True)
            agent.case.washer_type = "front_loader"
            agent.start()
            agent.handle("코드 없음")
            agent.case.service_detail = "물이 안 들어와요."
            self.assertIn(url, agent.report_saved())
            self.assertIn("수도꼭지를 부드럽게 열어", agent.handle("네"))
            self.assertEqual(agent.case.actions[-1]["source"]["knowledge_version"], "v6")
            self.assertEqual(official_code_reference(archive, None, "IE", "front_loader"), url)
            self.assertIsNone(official_code_reference(archive, None, "IE", "top_loader"))

    def test_support_ranking_never_sends_unknown_candidates_or_accepts_invented_ids(self):
        with patch("llm.OpenAI") as client:
            create = client.return_value.__enter__.return_value.responses.create
            create.return_value = SimpleNamespace(status="completed", output_text='{"order":["fabricated","top_spin_contact"]}')
            order = rank_public_support("SPIN_NOISE", ("top_spin_contact", "private entered text", "top_fill_supply"))
            self.assertEqual(order, ["top_spin_contact"])
            request = create.call_args.kwargs
            self.assertFalse(request["store"])
            self.assertNotIn("private entered text", request["input"])
            self.assertNotIn("top_fill_supply", request["input"])

    def test_registered_models_find_only_their_own_pages(self):
        expected = {
            "T25OZ0": ("MFL71831239", [45, 46]),
            "TR14WK1": ("MFL71831208", [36, 37]),
            "TR13ML2": ("MFL71831210", [39, 40]),
            "TR16DV6": ("MFL71831233", [35, 36]),
        }
        for model, (document, pages) in expected.items():
            with self.subTest(model=model):
                actions = referenced_actions(DEFAULT_ARCHIVE, model)
                self.assertEqual([a["source"]["pdf_page"] for a in actions], pages)
                self.assertTrue(all(a["source"]["document_id"] == document for a in actions))
        answer = self.agent.handle("tr14wk1에서 OE가 떠요")
        self.assertIn("PDF 36쪽", answer)
        self.assertNotIn("내려주세요", answer)
        answer = self.agent.handle("네")
        self.assertIn("참고 원문", answer)
        self.assertNotIn("PDF 37쪽", answer)

    def test_ask_for_missing_model_and_remember_oe(self):
        self.assertIn("제품 라벨", self.agent.handle("OE 에러가 떠요"))
        self.assertIn("PDF 36쪽", self.agent.handle("TR14WK1이에요"))
        self.assertEqual(self.agent.case.error_code, "OE")
        self.assertEqual(self.agent.case.symptoms, ["OE 에러가 떠요"])

    def test_customer_cannot_find_model(self):
        self.agent.handle("OE 에러가 떠요")
        self.assertIn("방문해서 제품을 점검", self.agent.handle("라벨을 확인하기 어려워요"))
        self.assertEqual(self.agent.case.service_reason, "model_unconfirmed")

    def test_unregistered_model(self):
        self.assertIn("방문해서 제품을 점검", self.agent.handle("F21VDSK에서 OE가 떠요"))
        self.assertEqual(self.agent.case.service_reason, "unregistered_model")
        self.assertFalse(self.agent.case.actions)

    def test_performed_then_resolved(self):
        self.agent.handle("TR14WK1 OE")
        self.agent.handle("네")
        self.assertIn("해결됐나요", self.agent.handle("해봤어요"))
        self.assertIn("상담을 마칠게요", self.agent.handle("네"))
        self.assertEqual(self.agent.case.status, "resolved")
        self.assertIs(self.agent.case.actions[0]["performed"], True)

    def test_unresolved_advances_and_offers_service_at_end(self):
        self.agent.handle("TR14WK1 OE")
        self.agent.handle("네")
        self.assertIn("PDF 37쪽", self.agent.handle("해봤는데 여전히 안 돼요"))
        self.agent.handle("네")
        self.assertIn("방문해서 제품을 점검", self.agent.handle("해봤는데 여전히 안 돼요"))
        self.assertEqual(self.agent.case.status, "service_offered")
        self.assertEqual([a["result"] for a in self.agent.case.actions], ["unresolved"] * 2)
        self.assertIn("요청을 기록", self.agent.handle("네"))
        self.assertEqual(self.agent.case.status, "service_requested")
        self.assertIsNotNone(self.agent.case.service_consent_at)

    def test_repeat_preserves_progress_and_skip_records_nonperformance(self):
        self.agent.handle("TR14WK1 OE")
        self.agent.handle("네")
        self.assertIn("직전 안내", self.agent.handle("다시 알려주세요"))
        self.assertEqual(len(self.agent.case.actions), 1)
        self.agent.handle("아직 안 했어요")
        self.assertEqual(self.agent.case.actions[0]["result"], "not_attempted")
        self.assertIn("방문해서 제품을 점검", self.agent.handle("건너뛸래요"))
        self.assertIs(self.agent.case.actions[0]["performed"], False)

    def test_unknown_symptom_gets_one_clarification(self):
        self.assertIn("에러코드", self.agent.handle("TR14WK1에서 이상한 소리가 나요"))
        self.assertIn("방문해서 제품을 점검", self.agent.handle("코드 없음"))
        self.assertFalse(self.agent.case.actions)

    def test_natural_language_with_matching_hose_observation(self):
        self.assertIn("PDF 37쪽", self.agent.handle("TR14WK1 배수 호스가 꺾였어요"))
        self.assertIsNone(self.agent.case.error_code)

    def test_web_no_code_report_can_continue_to_manual_check(self):
        agent = SelfCareAgent(DEFAULT_ARCHIVE, model="TR14WK1", web=True)
        agent.start()
        agent.handle("코드 없음")
        agent.case.service_detail = "탈수 전에 배수 호스가 꺾였어요."
        self.assertIn("PDF 37쪽", agent.report_saved())
        self.assertFalse(agent.case.actions)
        self.assertIn("참고 원문", agent.handle("네"))
        self.assertIsNone(agent.case.error_code)

    def test_each_reviewed_code_has_model_specific_manual_steps(self):
        expected_counts = {"UE": 3, "IE": 2, "DE": 1, "DL": 1, "LE1": 1, "CL": 1, "TCL": 1, "FF": 1}
        for model, (filename, _) in MANUALS.items():
            for code, count in expected_counts.items():
                with self.subTest(model=model, code=code):
                    steps = reviewed_alert_steps(DEFAULT_ARCHIVE, model, code)
                    self.assertEqual(len(steps), count)
                    self.assertTrue(all(step["source"]["filename"] == filename for step in steps))

    def test_expanded_pdf_and_html_manuals_keep_model_specific_sources(self):
        from manual_search import alerts_for, available_models

        models = available_models(DEFAULT_ARCHIVE)
        self.assertIn("F9WTBQ", models)
        self.assertIn("T19DX8", models)
        self.assertIn("T23PZ9", models)  # Cover-listed model sharing T22PZ9's manual.
        self.assertNotIn("F21VDSK", models)
        self.assertIn("배수 안 됨", alerts_for(DEFAULT_ARCHIVE, "T22PZ9"))
        examples = [("T22PZ9", "배수 안 됨", "PDF 52쪽"), ("F9WTBQ", "OE", "PDF 40쪽"),
                    ("T19DX8", "OE", "HTML 도움말 ko-kr/topics/topic_53.html")]
        for model, alert, source in examples:
            with self.subTest(model=model):
                agent = SelfCareAgent(DEFAULT_ARCHIVE, model=model, web=True)
                agent.start()
                self.assertIn(source, agent.handle(alert))
                self.assertIn(source, agent.handle("네"))
                self.assertEqual(agent.case.actions[0]["source"]["manual_name"], f"LG 세탁기 사용설명서 {model}")
                if alert == "배수 안 됨":
                    self.assertEqual(agent.case.display_label, alert)

    def test_web_no_code_fill_symptom_selects_inlet_checks_without_assigning_code(self):
        agent = SelfCareAgent(DEFAULT_ARCHIVE, model="TR14WK1", web=True)
        agent.start()
        agent.handle("코드 없음")
        agent.case.service_detail = "세탁통에 물이 안 들어와요."
        self.assertIn("수도꼭지가 잠겨 있나요?", agent.report_saved())
        self.assertIsNone(agent.case.error_code)

    def test_web_symptom_edit_switches_manual_check_and_preserves_previous_action(self):
        agent = SelfCareAgent(DEFAULT_ARCHIVE, model="TR14WK1", web=True)
        agent.start()
        agent.handle("코드 없음")
        agent.case.service_detail = "물이 안 들어와요."
        agent.report_saved()
        agent.handle("네")
        agent.case.service_detail = "물이 빠지지 않아요."
        self.assertIn("배수 호스가 올려져 있나요?", agent.reassess_report())
        self.assertEqual(agent.case.actions[0]["result"], "superseded")
        self.assertIn("자가조치 1단계", agent.handle("네"))
        self.assertEqual(agent.case.actions[1]["symptom_revision"], 1)

    def test_web_ue_tries_applicable_steps_before_report(self):
        agent = SelfCareAgent(DEFAULT_ARCHIVE, model="T25OZ0", web=True)
        agent.case.guidance_mode = "manual_only"
        agent.start()
        self.assertIn("세탁물이 한쪽으로 치우쳐 있나요?", agent.handle("UE"))
        self.assertIn("세탁물을 고르게 펴세요.", agent.handle("네"))
        self.assertIn("이불을 두 장 이상", agent.handle("해봤지만 안 돼요"))
        self.assertIn("세탁물이 뭉쳐 있나요?", agent.handle("아니요"))
        self.assertIn("증상을 아래에 기록", agent.handle("확인 어려워요"))
        self.assertEqual(agent.case.status, "collecting_report")
        agent.case.service_detail = "탈수 중 멈췄어요."
        self.assertIn("사진, 영상 또는 녹음", agent.report_saved())
        self.assertEqual(agent.case.status, "collecting_attachments")
        self.assertEqual(agent.case.actions[0]["result"], "unresolved")

    def test_web_service_only_code_requests_report_without_inventing_steps(self):
        agent = SelfCareAgent(DEFAULT_ARCHIVE, model="T25OZ0", web=True)
        agent.start()
        self.assertIn("개별 원인을 구분하지 않고", agent.handle("FE"))
        self.assertEqual(agent.case.status, "collecting_report")
        agent.case.service_detail = "세탁 도중 정지했어요."
        agent.report_saved()
        self.assertEqual(agent.case.status, "collecting_attachments")
        self.assertFalse(agent.case.actions)

    def test_safety_concern_during_a_step_stops_guidance(self):
        self.agent.handle("TR14WK1 OE")
        self.agent.handle("네")
        self.assertIn("사용을 중지", self.agent.handle("타는 냄새가 나요. 다시 알려주세요"))
        self.assertEqual(self.agent.case.service_reason, "safety_concern")

    def test_model_correction_stops_current_manual_guidance(self):
        self.agent.handle("TR14WK1 OE")
        self.agent.handle("네")
        self.assertIn("현재 안내를 중단", self.agent.handle("실제로는 T25OZ0이에요"))
        self.assertEqual(self.agent.case.service_reason, "model_changed")
        self.assertEqual(len(self.agent.case.actions), 1)

    def test_conditions_not_met_never_offer_a_step(self):
        self.agent.handle("TR14WK1 OE")
        self.assertIn("PDF 37쪽", self.agent.handle("아니요"))
        self.assertIn("방문해서 제품을 점검", self.agent.handle("아니요"))
        self.assertFalse(self.agent.case.actions)
        self.assertEqual([c["result"] for c in self.agent.case.checks], ["not_applicable"] * 2)
        self.agent.handle("아니요")
        self.assertEqual(self.agent.case.status, "service_declined")

    def test_condition_cannot_be_checked(self):
        self.agent.handle("TR14WK1 OE")
        self.assertIn("방문해서 제품을 점검", self.agent.handle("확인 어려워요"))
        self.assertFalse(self.agent.case.actions)
        self.assertEqual(self.agent.case.checks[0]["result"], "unconfirmed")

    def test_existing_local_command(self):
        args = SimpleNamespace(model="TR14WK1", error="OE", archive=DEFAULT_ARCHIVE)
        with patch("builtins.print") as output:
            self.assertEqual(local_search(args), 0)
        self.assertIn("PDF 36쪽", output.call_args_list[0].args[0])

    def test_cli_records_case_without_accessing_openai(self):
        with TemporaryDirectory() as directory:
            args = SimpleNamespace(archive=DEFAULT_ARCHIVE, case_dir=Path(directory))
            with (
                patch("builtins.input", side_effect=["TR14WK1 OE", "네", "해봤고 해결됐어요"]),
                patch("builtins.print"),
                patch("llm.OpenAI") as client,
            ):
                self.assertEqual(run_chat(args), 0)
                client.assert_not_called()
            records = list(Path(directory).glob("*.json"))
            self.assertEqual(len(records), 1)
            payload = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "resolved")
            self.assertEqual(payload["model"], "TR14WK1")
            self.assertEqual(payload["actions"][0]["source"]["pdf_page"], 36)
            self.assertEqual(len(payload["messages"]), 7)

    def test_case_snapshot_replaces_previous_state(self):
        with TemporaryDirectory() as directory:
            store = JsonCaseStore(Path(directory))
            path = store.save(self.agent.case)
            self.agent.handle("F21VDSK OE")
            self.assertEqual(path, store.save(self.agent.case))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["service_reason"], "unregistered_model")


class OpenAIDemoTests(unittest.TestCase):
    def test_demo_does_not_forward_free_text_or_load_manuals(self):
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "test-model"}),
            patch("builtins.input", side_effect=["PRIVATE CUSTOMER TEXT MFL71831208", "1", "2"]),
            patch("builtins.print"),
            patch("manual_search.search_oe") as search,
            patch("llm.OpenAI") as client,
        ):
            create = client.return_value.__enter__.return_value.responses.create
            create.return_value = SimpleNamespace(status="completed", output_text="가상 응답")
            self.assertEqual(openai_demo(SimpleNamespace(interactive=True)), 0)
            search.assert_not_called()
            self.assertEqual(create.call_count, 3)
            for call, turn in zip(create.call_args_list, [DemoTurn.START, DemoTurn.STEADY, DemoTurn.UNRESOLVED]):
                request = call.kwargs
                self.assertIs(request["store"], False)
                self.assertIn("DEMO-01", request["input"])
                self.assertIn(turn.value, request["input"])
                self.assertNotIn("PRIVATE CUSTOMER TEXT", request["input"])
                self.assertNotIn("MFL71831208", request["input"])
                self.assertNotIn("test-key", str(request))

    def test_api_boundary_rejects_arbitrary_text_before_client_creation(self):
        with patch("llm.OpenAI") as client:
            with self.assertRaises(ValueError):
                generate_answer(turn="actual manual text")
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
