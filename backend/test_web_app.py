import json
import threading
import unittest
from datetime import datetime, timedelta
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from case_store import JsonCaseStore
from reports import technician_report
from web_app import KST, SmartCareServer


class WebConsultationTest(unittest.TestCase):
    def test_device_history_resume_recurrence_and_categories(self):
        browser, device, second_device = (str(uuid4()) for _ in range(3))
        with TemporaryDirectory() as folder:
            store = JsonCaseStore(Path(folder))
            case_id = None
            # A fresh server instance must load the current step from disk.
            for phase in range(2):
                with SmartCareServer(("127.0.0.1", 0), store=store) as server:
                    worker = threading.Thread(target=server.serve_forever)
                    worker.start()
                    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    try:
                        def request(path, payload=None, owner=browser):
                            headers = {"X-SmartCare-Browser": owner}
                            if payload is not None:
                                headers["Content-Type"] = "application/json"
                            conn.request("POST" if payload is not None else "GET", path,
                                         json.dumps(payload) if payload is not None else None, headers)
                            response = conn.getresponse()
                            return response.status, json.loads(response.read())

                        model = {"model": "TR14WK1", "device_id": device, "device_name": "세탁실"}
                        if phase == 0:
                            _, started = request("/api/cases", model)
                            self.assertEqual(started["illustration"], "washer-error-display")
                            case_id = started["case_id"]
                            _, step = request(f"/api/cases/{case_id}/message", {"text": "OE"})
                            self.assertEqual(step["status"], "checking_condition")
                            continue
                        root = f"/api/cases/{case_id}"
                        self.assertEqual(request(root + "/open", {}, owner=str(uuid4()))[0], 404)
                        _, opened = request(root + "/open", {})
                        self.assertEqual(opened["history"][-1]["role"], "agent")
                        self.assertIn("배수 호스가 올려져", opened["history"][-1]["text"])
                        _, step = request(root + "/message", {"text": "네"})
                        self.assertEqual(step["status"], "awaiting_action_result")
                        self.assertEqual(step["illustration"], "drain-hose-down")
                        _, resolved = request(root + "/message", {"text": "해봤고 해결됐어요"})
                        self.assertEqual(resolved["status"], "resolved")
                        _, recurrence = request(root + "/recur", {})
                        self.assertEqual(recurrence["previous_case_id"], case_id)
                        self.assertNotEqual(recurrence["case_id"], case_id)
                        self.assertEqual(recurrence["related_count"], 1)
                        self.assertTrue(any("다시 발생" in item["text"] for item in recurrence["history"]))
                        reopened = store.load(recurrence["case_id"])
                        self.assertEqual(reopened.device_history[0]["status"], "resolved")
                        self.assertTrue(reopened.device_history[0]["actions"][0]["responded_at"])
                        self.assertTrue(reopened.device_history[0]["created_at"])
                        _, same_device = request("/api/cases", model)
                        self.assertEqual(same_device["related_count"], 2)
                        _, other_unit = request("/api/cases", {**model, "device_id": second_device})
                        self.assertEqual(other_unit["related_count"], 0)
                        _, ac = request("/api/cases", {"category": "air_conditioner", "model": "FQ17"})
                        self.assertEqual(ac["status"], "collecting_report")
                        self.assertEqual(ac["choices"], [])
                        _, listing = request("/api/cases")
                        self.assertEqual(len(listing["cases"]), 5)
                        self.assertTrue(all(item["created_at"] and item["updated_at"] for item in listing["cases"]))
                    finally:
                        conn.close()
                        server.shutdown()
                        worker.join()

    def test_registered_model_upload_and_visit_request(self):
        with TemporaryDirectory() as folder, SmartCareServer(
            ("127.0.0.1", 0), store=JsonCaseStore(Path(folder))
        ) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                self.check_consultation(conn, Path(folder))
            finally:
                conn.close()
                server.shutdown()
                worker.join()

    def check_consultation(self, conn, directory):
        browser = str(uuid4())
        owner = {"X-SmartCare-Browser": browser}

        def post(path, payload):
            conn.request("POST", path, json.dumps(payload), {**owner, "Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, json.loads(response.read())

        conn.request("GET", "/")
        page = conn.getresponse()
        self.assertEqual(page.status, 200)
        self.assertIn(b"device-form", page.read())
        conn.request("GET", "/guides/washer-error-display.png")
        photo = conn.getresponse()
        self.assertEqual(photo.status, 200)
        self.assertEqual(photo.getheader("Content-Type"), "image/png")
        self.assertTrue(photo.read().startswith(b"\x89PNG\r\n\x1a\n"))
        status, started = post("/api/cases", {"model": "TR14WK1"})
        self.assertEqual(status, 200)
        self.assertIn("TR14WK1", started["message"])
        self.assertEqual(started["choices"], [
            "OE", "UE", "IE", "dE", "dL", "LE1", "CL", "LE", "FE", "tE", "AE", "FF", "E3", "tcL",
            "코드 없음", "코드 확인 어려움",
        ])
        root = "/api/cases/" + started["case_id"]
        visit_date = (datetime.now(KST) + timedelta(days=1)).date().isoformat()
        slot = {"date": visit_date, "time": "14:30", "phone": "010-1234-5678"}
        self.assertEqual(post(root + "/schedule", slot)[0], 400)
        _, intake = post(root + "/message", {"text": "OE"})
        self.assertEqual(intake["status"], "checking_condition")
        self.assertIn("OE · 배수 오류", intake["message"])
        self.assertIn("배수 호스가 올려져 있나요?", intake["message"])
        self.assertEqual(post(root + "/report", {"detail": "배수가 멈췄어요"})[0], 400)
        self.assertEqual(post(root + "/finish-media", {})[0], 400)
        for message, expected in [("네", "awaiting_action_result"), ("해봤지만 안 돼요", "checking_condition"),
                                  ("아니요", "collecting_report")]:
            status, reply = post(root + "/message", {"text": message})
            self.assertEqual((status, reply["status"]), (200, expected))
            self.assertNotIn("schedule", reply)
        _, saved = post(root + "/report", {"detail": "탈수 중 배수가 멈췄어요.", "frequency": "상시"})
        self.assertEqual(saved["status"], "collecting_attachments")
        self.assertEqual(saved["detail"], "탈수 중 배수가 멈췄어요.")
        status, error = post(root + "/schedule", {})
        self.assertEqual(status, 400)
        self.assertNotIn("schedule", error)

        for name, content in [("reject.html", b"<html>bad</html>"),
                              ("sample.wav", b"RIFF\x24\x00\x00\x00WAVEfmt "),
                              ("sample.png", b"\x89PNG\r\n\x1a\n"),
                              ("sample.mp4", b"\x00\x00\x00\x18ftypisom"),
                              ("recording.webm", b"\x1a\x45\xdf\xa3webmA_OPUS"),
                              ("recording.ogg", b"OggSOpusHead")]:
            conn.request("POST", root + "/attachment", content, {
                **owner, "Content-Type": "application/octet-stream", "X-Filename": name
            })
            response = conn.getresponse()
            data = json.loads(response.read())
            if name == "reject.html":
                self.assertEqual(response.status, 400)
            else:
                self.assertEqual(response.status, 201)
                self.assertEqual((directory / data["attachment"]["path"]).read_bytes(), content)
                if name.startswith("recording"):
                    self.assertTrue(data["attachment"]["mime"].startswith("audio/"))

        _, finished = post(root + "/finish-media", {})
        self.assertEqual(finished["status"], "service_offered")
        self.assertNotIn("schedule", finished)
        status, schedule_prompt = post(root + "/message", {"text": "네"})
        self.assertEqual((status, schedule_prompt["status"]), (200, "service_requested"))
        self.assertIn("날짜와 시간", schedule_prompt["message"])
        conn.request("POST", root + "/attachment", b"OggSOpusHead", {**owner, "Content-Type": "application/octet-stream"})
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        self.assertEqual(post(root + "/schedule", {**slot, "date": "2000-01-01"})[0], 400)
        status, submitted = post(root + "/schedule", slot)
        self.assertEqual((status, submitted["status"]), (200, "service_submitted"))
        self.assertEqual(submitted["schedule"]["preferred_visit_at"], f"{visit_date}T14:30+09:00")
        self.assertEqual(submitted["schedule"]["dispatch_status"], "pending_integration")
        report = submitted["customer_report"]
        self.assertEqual(report["case_id"], started["case_id"])
        self.assertEqual(report["symptom"], "탈수 중 배수가 멈췄어요.")
        self.assertEqual(report["assessment"]["title"], "배수 경로 이상 가능성")
        self.assertEqual(report["assessment"]["observation"], report["symptom"])
        self.assertIn("같은 매뉴얼 점검 항목", report["assessment"]["basis"])
        self.assertTrue(report["assessment"]["source_url"].startswith("https://www.lge.co.kr/"))
        self.assertEqual(report["frequency"], "상시")
        self.assertIn("MFL71831208 PDF 36쪽", report["manual_references"])
        self.assertEqual(report["confirmed_checks"], ["배수 호스가 올려져 있나요?"])
        self.assertEqual(report["preferred_visit_at"], submitted["schedule"]["preferred_visit_at"])
        self.assertEqual([action["result"] for action in report["actions"]], ["unresolved"])
        self.assertNotIn("attachments", report)
        self.assertNotIn("conversation", report)
        self.assertNotIn(slot["phone"], json.dumps(report))
        self.assertNotIn("callback_phone", submitted["schedule"])
        stored = json.loads((directory / (started["case_id"] + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(stored["service_detail"], "탈수 중 배수가 멈췄어요.")
        self.assertEqual(len(stored["attachments"]), 5)
        self.assertTrue(stored["media_collected_at"])
        self.assertTrue(stored["service_consent_at"])
        self.assertEqual(stored["service_request"]["callback_phone"], slot["phone"])
        technician = technician_report(JsonCaseStore(directory).load(started["case_id"]))
        self.assertEqual(technician["summary"], report)
        self.assertEqual(technician["service_request"]["callback_phone"], slot["phone"])
        self.assertEqual(len(technician["attachments"]), 5)
        self.assertTrue(technician["manual_checks"])
        _, retried = post(root + "/schedule", slot)
        self.assertEqual(retried["schedule"]["request_id"], submitted["schedule"]["request_id"])
        _, reopened = post(root + "/open", {})
        self.assertEqual(reopened["schedule"], submitted["schedule"])
        self.assertEqual(reopened["customer_report"], report)
        _, edited_after_request = post(root + "/edit-report", {"detail": "세탁통에 물이 안 들어와요."})
        self.assertEqual(edited_after_request["status"], "service_submitted")
        assessment = edited_after_request["customer_report"]["assessment"]
        self.assertEqual(assessment["title"], "코드와 증상을 함께 점검해야 해요")
        self.assertEqual(assessment["observation"], "세탁통에 물이 안 들어와요.")
        self.assertIsNone(edited_after_request["message"])
        self.assertEqual(edited_after_request["schedule"], submitted["schedule"])
        media = {item["name"]: item for item in reopened["media"]}
        self.assertEqual(len(media), 5)
        photo_path = root + "/media/" + media["sample.png"]["id"]
        conn.request("GET", photo_path, headers={"X-SmartCare-Browser": str(uuid4())})
        response = conn.getresponse()
        self.assertEqual(response.status, 404)
        response.read()
        conn.request("GET", photo_path, headers=owner)
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "image/png")
        self.assertEqual(response.read(), b"\x89PNG\r\n\x1a\n")
        for name, mime in [("sample.mp4", "video/mp4"), ("sample.wav", "audio/wav")]:
            conn.request("GET", root + "/media/" + media[name]["id"], headers=owner)
            response = conn.getresponse()
            self.assertEqual((response.status, response.getheader("Content-Type")), (200, mime))
            response.read()

        _, started = post("/api/cases", {"model": None})
        self.assertIn("모델명", started["message"])
        root = "/api/cases/" + started["case_id"]
        _, reply = post(root + "/message", {"text": "TR14WK1"})
        self.assertIn("코드 없음", reply["choices"])
        _, reply = post(root + "/message", {"text": "코드 없음"})
        self.assertEqual(reply["status"], "collecting_report")
        self.assertTrue(reply["message"].startswith("에러코드 없이 증상을 판단할게요."))
        conn.request("POST", root + "/attachment", b"OggSOpusHead", {
            **owner, "Content-Type": "application/octet-stream", "X-Filename": "before-report.ogg"
        })
        upload = conn.getresponse()
        self.assertEqual(upload.status, 201)
        upload.read()
        fields = {"detail": "탈수에서 큰 소리가 나요.", "purchase_period": "2023-10-05", "cycle_stage": "탈수",
                  "frequency": "상시", "prior_actions": "빨래 양을 줄였어요.", "symptom_category": "소음·진동",
                  "first_observed": "2026-11-20"}
        _, reply = post(root + "/report", fields)
        self.assertEqual(reply["status"], "service_offered")
        self.assertEqual([item["name"] for item in reply["media"]], ["before-report.ogg"])
        _, reply = post(root + "/message", {"text": "아니요"})
        self.assertEqual(reply["status"], "service_declined")
        stored = json.loads((directory / (started["case_id"] + ".json")).read_text(encoding="utf-8"))
        self.assertIsNone(stored["error_code"])
        self.assertEqual(stored["display_code_status"], "not_displayed")
        self.assertEqual(stored["service_detail"], fields.pop("detail"))
        self.assertEqual(stored["report_details"], fields)

        _, started = post("/api/cases", {"model": "T25OZ0"})
        root = "/api/cases/" + started["case_id"]
        _, reply = post(root + "/message", {"text": "tcL"})
        self.assertEqual(reply["status"], "checking_condition")
        self.assertIn("tcL · 세탁통을 청소할 시기", reply["message"])
        self.assertIn("46쪽", reply["message"])
        _, reply = post(root + "/message", {"text": "네"})
        self.assertEqual(reply["status"], "awaiting_action_result")
        self.assertIn("통세척 코스를 진행하세요", reply["message"])
        _, reply = post(root + "/message", {"text": "해결됐어요"})
        self.assertEqual(reply["status"], "resolved")
        self.assertEqual(reply["customer_report"]["status"], "resolved")
        self.assertIsNone(reply["customer_report"]["preferred_visit_at"])
        self.assertEqual(post(root + "/finish-media", {})[0], 400)
