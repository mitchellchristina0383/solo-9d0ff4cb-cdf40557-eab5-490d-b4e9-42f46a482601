"""端到端测试：通过真实 HTTP 服务和 SQLite 校验打印队列。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from printroom import service  # noqa: E402
from printroom.server import create_server  # noqa: E402


class ServerFixture:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.server = create_server(db_path, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method: str, path: str, body=None):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body if body is not None else {})


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.srv = ServerFixture(self.db_path)

    def tearDown(self) -> None:
        self.srv.close()
        self.tmp.cleanup()

    # ---- 辅助 ----
    def make_doc(self, title: str, pages: int) -> int:
        status, doc = self.srv.post("/api/documents", {"title": title})
        self.assertEqual(status, 201, doc)
        for i in range(pages):
            code, body = self.srv.post(f"/api/documents/{doc['id']}/pages", {"content": f"第{i + 1}页内容"})
            self.assertEqual(code, 201, body)
        return doc["id"]

    def finalize(self, doc_id: int) -> dict:
        code, job = self.srv.post(f"/api/documents/{doc_id}/finalize")
        self.assertEqual(code, 200, job)
        return job

    def start(self, job_id: int, printer: str) -> dict:
        code, job = self.srv.post(f"/api/jobs/{job_id}/start", {"printer_id": printer})
        self.assertEqual(code, 200, job)
        return job

    def state(self) -> dict:
        code, data = self.srv.get("/api/state")
        self.assertEqual(code, 200, data)
        return data

    def printer(self, printer_id: str) -> dict:
        return next(p for p in self.state()["printers"] if p["id"] == printer_id)

    def job(self, job_id: int) -> dict:
        return next(j for j in self.state()["jobs"] if j["id"] == job_id)

    def current_attempt(self, job_id: int) -> dict:
        att = next(
            (r for r in self.job(job_id)["records"] if r["status"] == "in_progress"),
            None,
        )
        self.assertIsNotNone(att, "当前应有正在出纸的记录")
        return att

    def segment(self, job_id: int, status: str) -> dict:
        seg = next(
            (s for s in self.job(job_id)["segments"] if s["status"] == status),
            None,
        )
        self.assertIsNotNone(seg, f"应有状态为 {status} 的打印段")
        return seg

    def print_side(self, job_id: int, side: str, attempt_id: int):
        return self.srv.post(
            f"/api/jobs/{job_id}/print", {"side": side, "attempt_id": attempt_id}
        )

    def print_current(self, job_id: int, side: str):
        return self.print_side(job_id, side, self.current_attempt(job_id)["id"])

    def print_sheet(self, job_id: int, sheet_no: int) -> dict:
        code, job = self.print_current(job_id, "front")
        self.assertEqual(code, 200, job)
        if (
            job["status"] == "printing"
            and job["current_sheet"] == sheet_no
            and sheet_no * 2 <= job["page_count"]
        ):
            code, job = self.print_current(job_id, "back")
            self.assertEqual(code, 200, job)
        return job

    def report_jam(self, job_id: int, segment_id=None, attempt_id=None):
        if segment_id is None:
            segment_id = self.segment(job_id, "active")["id"]
        if attempt_id is None:
            attempt_id = self.current_attempt(job_id)["id"]
        return self.srv.post(
            f"/api/jobs/{job_id}/jam",
            {"segment_id": segment_id, "attempt_id": attempt_id},
        )

    def confirm_stop(self, job_id: int, segment_id=None):
        if segment_id is None:
            segment_id = self.segment(job_id, "jammed")["id"]
        return self.srv.post(f"/api/jobs/{job_id}/stop", {"segment_id": segment_id})

    def switch(self, job_id: int, printer_id=None):
        body = {"printer_id": printer_id} if printer_id else {}
        return self.srv.post(f"/api/jobs/{job_id}/switch", body)


class DocumentTests(BaseCase):
    def test_page_entry_and_finalize(self):
        doc_id = self.make_doc("会议纪要", 3)
        code, doc = self.srv.get(f"/api/documents/{doc_id}")
        self.assertEqual(code, 200)
        self.assertEqual(doc["page_count"], 3)
        self.assertEqual([p["page_no"] for p in doc["pages"]], [1, 2, 3])

        code, doc = self.srv.post(f"/api/documents/{doc_id}/pages/last")
        self.assertEqual(code, 200)
        self.assertEqual(doc["page_count"], 2)

        job = self.finalize(doc_id)
        self.assertEqual(job["page_count"], 2)
        self.assertEqual(job["total_sheets"], 1)
        self.assertEqual(job["status"], "queued")

        # 入队后页面锁定
        code, body = self.srv.post(f"/api/documents/{doc_id}/pages", {"content": "迟到页"})
        self.assertEqual(code, 409, body)

        # 重复提交返回同一作业
        again = self.finalize(doc_id)
        self.assertEqual(again["id"], job["id"])
        self.assertEqual(len(self.state()["jobs"]), 1)

    def test_empty_document_cannot_be_finalized(self):
        _, doc = self.srv.post("/api/documents", {"title": "空文档"})
        code, body = self.srv.post(f"/api/documents/{doc['id']}/finalize")
        self.assertEqual(code, 400, body)
        self.assertEqual(body["code"], "empty_document")

    def test_blank_title_rejected(self):
        code, body = self.srv.post("/api/documents", {"title": "   "})
        self.assertEqual(code, 400, body)


class NormalPrintTests(BaseCase):
    def test_even_pages_two_sheets(self):
        job_id = self.finalize(self.make_doc("偶数页", 4))["id"]
        job = self.start(job_id, "P1")
        self.assertEqual(job["status"], "printing")
        self.assertEqual(job["current_sheet"], 1)

        job = self.print_sheet(job_id, 1)
        self.assertEqual(job["printed_sheets"], 1)
        job = self.print_sheet(job_id, 2)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["printed_sheets"], 2)
        self.assertEqual(self.printer("P1")["state"], "idle")
        self.assertEqual(len(job["segments"]), 1)

    def test_odd_pages_last_back_blank(self):
        job_id = self.finalize(self.make_doc("奇数页", 3))["id"]
        self.start(job_id, "P1")

        job = self.print_sheet(job_id, 1)
        self.assertEqual(job["printed_sheets"], 1)

        # 末张只有正面：出正面后整张完成，背面留白不能单独出
        code, job = self.print_current(job_id, "front")
        self.assertEqual(code, 200, job)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["printed_sheets"], 2)

        rec = job["records"][0]
        self.assertEqual(rec["sheet_no"], 2)
        self.assertEqual(rec["front_page"], 3)
        self.assertIsNone(rec["back_page"])
        self.assertFalse(rec["required_back"])
        self.assertEqual(rec["status"], "complete")

    def test_single_page_document(self):
        job_id = self.finalize(self.make_doc("单页", 1))["id"]
        job = self.start(job_id, "P1")
        self.assertEqual(job["total_sheets"], 1)
        code, job = self.print_current(job_id, "front")
        self.assertEqual(code, 200, job)
        self.assertEqual(job["status"], "completed")

    def test_back_before_front_rejected(self):
        job_id = self.finalize(self.make_doc("双面", 4))["id"]
        self.start(job_id, "P1")
        code, body = self.print_current(job_id, "back")
        self.assertEqual(code, 409, body)

    def test_double_front_rejected(self):
        job_id = self.finalize(self.make_doc("双面", 4))["id"]
        self.start(job_id, "P1")
        att = self.current_attempt(job_id)
        code, _ = self.print_side(job_id, "front", att["id"])
        self.assertEqual(code, 200)
        code, body = self.print_side(job_id, "front", att["id"])
        self.assertEqual(code, 409, body)

    def test_stale_attempt_rejected(self):
        """已完整出纸的那次出纸，再点一律按过期拒绝。"""
        job_id = self.finalize(self.make_doc("双面", 4))["id"]
        self.start(job_id, "P1")
        first = self.current_attempt(job_id)
        job = self.print_sheet(job_id, 1)
        self.assertEqual(job["printed_sheets"], 1)
        code, body = self.print_side(job_id, "front", first["id"])
        self.assertEqual(code, 409, body)
        self.assertEqual(body["code"], "stale")

    def test_blank_back_rejected(self):
        job_id = self.finalize(self.make_doc("奇数页", 3))["id"]
        self.start(job_id, "P1")
        self.print_sheet(job_id, 1)
        last = self.current_attempt(job_id)
        code, _ = self.print_side(job_id, "front", last["id"])
        self.assertEqual(code, 200)
        code, body = self.print_side(job_id, "back", last["id"])
        self.assertEqual(code, 409, body)


class JamTests(BaseCase):
    def test_half_sheet_jam_and_switch(self):
        """半张卡纸：该张作废并重印两面，续单只派一份。"""
        job_id = self.finalize(self.make_doc("半张卡纸", 6))["id"]
        self.start(job_id, "P1")

        # 第 1 张完整出纸
        self.print_sheet(job_id, 1)

        # 第 2 张只出了正面就卡纸
        code, job = self.print_current(job_id, "front")
        self.assertEqual(code, 200, job)
        code, job = self.report_jam(job_id)
        self.assertEqual(code, 200, job)
        self.assertEqual(job["status"], "jammed")
        self.assertEqual(job["printed_sheets"], 1)
        self.assertEqual(self.printer("P1")["state"], "jammed")
        self.assertEqual(len(job["segments"]), 1, "停稳前不得派续单")

        # 停稳前不能换机
        code, body = self.switch(job_id)
        self.assertEqual(code, 409, body)
        self.assertEqual(body["code"], "not_stopped")

        # 半张作废记录保留、背面未出
        scrapped = [r for r in job["records"] if r["status"] == "scrapped"]
        self.assertEqual(len(scrapped), 1)
        self.assertEqual(scrapped[0]["sheet_no"], 2)
        self.assertTrue(scrapped[0]["front_printed"])
        self.assertFalse(scrapped[0]["back_printed"])

        # 确认停稳 → 换机
        code, job = self.confirm_stop(job_id)
        self.assertEqual(code, 200, job)
        self.assertEqual(job["status"], "stopped")
        self.assertEqual(job["printed_sheets"], 1)

        code, job = self.switch(job_id)
        self.assertEqual(code, 200, job)
        self.assertEqual(job["status"], "printing")
        self.assertEqual(job["current_sheet"], 2)
        self.assertEqual(job["printed_sheets"], 1)
        cont = [s for s in job["segments"] if s["kind"] == "continuation"]
        self.assertEqual(len(cont), 1)
        self.assertEqual(cont[0]["printer_id"], "P2")
        self.assertEqual(cont[0]["start_sheet"], 2)

        # 重复换机只保留一份续单
        code, job = self.switch(job_id)
        self.assertEqual(code, 200, job)
        self.assertEqual(len([s for s in job["segments"] if s["kind"] == "continuation"]), 1)

        # 备机从第 2 张起重印两面
        code, body = self.print_current(job_id, "back")
        self.assertEqual(code, 409, f"续单必须先重印正面: {body}")
        job = self.print_sheet(job_id, 2)
        self.assertEqual(job["printed_sheets"], 2)
        job = self.print_sheet(job_id, 3)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["printed_sheets"], 3)

        records = self.state()["records"]
        sheets = [(r["sheet_no"], r["printer_id"], r["status"]) for r in records]
        # 第 1 张 P1 完整；第 2 张 P1 作废、P2 完整；第 3 张 P2 完整
        self.assertIn((1, "P1", "complete"), sheets)
        self.assertIn((2, "P1", "scrapped"), sheets)
        self.assertIn((2, "P2", "complete"), sheets)
        self.assertIn((3, "P2", "complete"), sheets)
        self.assertEqual(len([r for r in records if r["job_id"] == job_id and r["status"] == "complete"]), 3)

    def test_completed_before_stop_are_kept(self):
        """原机停稳前完整出纸的纸张都算数。"""
        job_id = self.finalize(self.make_doc("停稳前出纸", 8))["id"]
        self.start(job_id, "P1")
        for sheet in (1, 2):
            self.print_sheet(job_id, sheet)

        # 第 3 张正面卡纸，第 4 张不可能出
        jammed_attempt = self.current_attempt(job_id)
        self.print_current(job_id, "front")
        code, _ = self.report_jam(job_id)
        self.assertEqual(code, 200)

        # 停稳前再尝试出纸一律拒绝（过期），已出纸保持
        code, body = self.print_side(job_id, "front", jammed_attempt["id"])
        self.assertEqual(code, 409, body)
        self.assertEqual(body["code"], "stale")
        job = self.job(job_id)
        self.assertEqual(job["printed_sheets"], 2)
        self.assertEqual(job["next_sheet"], 3)

        code, _ = self.confirm_stop(job_id)
        self.assertEqual(code, 200)
        code, job = self.switch(job_id)
        self.assertEqual(code, 200, job)
        self.assertEqual(job["printed_sheets"], 2, "停稳前完整出纸的两张必须保留")
        self.assertEqual(job["current_sheet"], 3)

        for sheet in (3, 4):
            job = self.print_sheet(job_id, sheet)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["printed_sheets"], 4)

    def test_jam_at_first_sheet_has_no_printed_sheets(self):
        job_id = self.finalize(self.make_doc("首张卡纸", 5))["id"]
        self.start(job_id, "P1")
        self.print_current(job_id, "front")
        code, _ = self.report_jam(job_id)
        self.assertEqual(code, 200)
        self.assertEqual(self.job(job_id)["printed_sheets"], 0)
        code, _ = self.confirm_stop(job_id)
        self.assertEqual(code, 200)
        code, job = self.switch(job_id)
        self.assertEqual(code, 200, job)
        self.assertEqual(job["current_sheet"], 1)
        job = self.print_sheet(job_id, 1)
        job = self.print_sheet(job_id, 2)
        self.assertEqual(job["printed_sheets"], 2)
        job = self.print_sheet(job_id, 3)
        self.assertEqual(job["status"], "completed")

    def test_switch_before_start_rejected(self):
        job_id = self.finalize(self.make_doc("未开始", 2))["id"]
        code, body = self.switch(job_id)
        self.assertEqual(code, 409, body)

    def test_jam_twice_and_switch_twice(self):
        """两台机各卡一次，每次停稳后只派一份续单。"""
        job_id = self.finalize(self.make_doc("两次换机", 8))["id"]
        self.start(job_id, "P1")
        self.print_sheet(job_id, 1)
        self.print_current(job_id, "front")
        code, _ = self.report_jam(job_id)
        self.assertEqual(code, 200)
        code, _ = self.confirm_stop(job_id)
        self.assertEqual(code, 200)
        code, _ = self.switch(job_id)
        self.assertEqual(code, 200)

        # 备机第 2 张正面后再次卡纸
        self.print_current(job_id, "front")
        code, _ = self.report_jam(job_id)
        self.assertEqual(code, 200)
        code, _ = self.confirm_stop(job_id)
        self.assertEqual(code, 200)
        code, job = self.switch(job_id)
        self.assertEqual(code, 200, job)
        conts = [s for s in job["segments"] if s["kind"] == "continuation"]
        self.assertEqual(len(conts), 2)
        self.assertEqual([s["printer_id"] for s in conts], ["P2", "P1"])
        self.assertEqual([s["start_sheet"] for s in conts], [2, 2])
        self.assertEqual(job["current_sheet"], 2)
        self.assertEqual(job["printed_sheets"], 1)

        for sheet in (2, 3, 4):
            job = self.print_sheet(job_id, sheet)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["printed_sheets"], 4)

    def test_cannot_switch_to_same_printer_or_busy_printer(self):
        job1 = self.finalize(self.make_doc("作业一", 6))["id"]
        job2 = self.finalize(self.make_doc("作业二", 4))["id"]
        self.start(job1, "P1")
        self.start(job2, "P2")
        self.print_current(job1, "front")
        code, _ = self.report_jam(job1)
        self.assertEqual(code, 200)
        code, _ = self.confirm_stop(job1)
        self.assertEqual(code, 200)

        code, body = self.switch(job1, "P1")
        self.assertEqual(code, 400, body)
        code, body = self.switch(job1, "P2")
        self.assertEqual(code, 409, body)
        self.assertEqual(body["code"], "printer_busy")


class StalePageTests(BaseCase):
    """过期页面：出纸、报卡、停稳只能落在页面显示的那一次上。"""

    def two_switch_setup(self):
        """一号机卡纸换二号机、再卡换回一号机，回到一号机重印同一张纸。

        返回 (job_id, (首段, 首次出纸), (次段, 二次出纸), 当前出纸)。
        """
        job_id = self.finalize(self.make_doc("换机两次", 4))["id"]
        self.start(job_id, "P1")
        first = (self.segment(job_id, "active"), self.current_attempt(job_id))
        second = None
        for i in range(2):
            code, _ = self.report_jam(job_id)
            self.assertEqual(code, 200)
            code, _ = self.confirm_stop(job_id)
            self.assertEqual(code, 200)
            code, _ = self.switch(job_id)
            self.assertEqual(code, 200)
            if i == 0:
                second = (self.segment(job_id, "active"), self.current_attempt(job_id))
        current = self.current_attempt(job_id)
        self.assertEqual(self.printer("P1")["state"], "printing")
        return job_id, first, second, current

    def test_stale_front_click_does_not_mark_new_sheet(self):
        """换机两次回到一号机：最初页面的「出正面」不得记到新纸上。"""
        job_id, (_, att1), _, current = self.two_switch_setup()
        self.assertEqual(att1["sheet_no"], current["sheet_no"], "重印的是同一张纸")
        self.assertNotEqual(att1["id"], current["id"])

        code, body = self.print_side(job_id, "front", att1["id"])
        self.assertEqual(code, 409, body)
        self.assertEqual(body["code"], "stale")

        # 出纸记录、作业进度、机台占用保持原状
        job = self.job(job_id)
        self.assertEqual(job["printed_sheets"], 0)
        self.assertEqual(job["status"], "printing")
        rec = next(r for r in job["records"] if r["id"] == current["id"])
        self.assertFalse(rec["front_printed"])
        self.assertEqual(rec["status"], "in_progress")
        p1 = self.printer("P1")
        self.assertEqual(p1["state"], "printing")
        self.assertEqual(p1["attempt"]["id"], current["id"])

        # 刷新到当前这次出纸后可以正常出
        code, job = self.print_side(job_id, "front", current["id"])
        self.assertEqual(code, 200, job)
        rec = next(r for r in job["records"] if r["id"] == current["id"])
        self.assertTrue(rec["front_printed"])

    def test_stale_back_click_rejected(self):
        """二号机那次的旧页面点「出背面」，不得落到一号机新纸上。"""
        job_id, _, (_, att2), current = self.two_switch_setup()
        code, _ = self.print_side(job_id, "front", current["id"])
        self.assertEqual(code, 200)
        code, body = self.print_side(job_id, "back", att2["id"])
        self.assertEqual(code, 409, body)
        self.assertEqual(body["code"], "stale")
        rec = next(r for r in self.job(job_id)["records"] if r["id"] == current["id"])
        self.assertFalse(rec["back_printed"])

    def test_stale_jam_report_keeps_state(self):
        """旧页面的「报告卡纸」不得作废当前出纸、不得改变机台占用。"""
        job_id, (seg1, att1), _, current = self.two_switch_setup()
        code, body = self.srv.post(
            f"/api/jobs/{job_id}/jam",
            {"segment_id": seg1["id"], "attempt_id": att1["id"]},
        )
        self.assertEqual(code, 409, body)
        self.assertEqual(body["code"], "stale")

        job = self.job(job_id)
        self.assertEqual(job["status"], "printing")
        self.assertEqual(job["printed_sheets"], 0)
        rec = next(r for r in job["records"] if r["id"] == current["id"])
        self.assertEqual(rec["status"], "in_progress")
        self.assertEqual(self.printer("P1")["state"], "printing", "机台占用保持原状")

    def test_stale_stop_after_switch_rejected(self):
        """已换机的那次卡纸，再确认停稳按过期拒绝。"""
        job_id, (seg1, _), _, _ = self.two_switch_setup()
        code, body = self.confirm_stop(job_id, seg1["id"])
        self.assertEqual(code, 409, body)
        self.assertEqual(body["code"], "stale")

    def test_confirm_stop_idempotent_until_switch(self):
        """确认后还没换机，重复确认同一次卡纸仍可成功。"""
        job_id = self.finalize(self.make_doc("重复确认", 4))["id"]
        self.start(job_id, "P1")
        code, _ = self.report_jam(job_id)
        self.assertEqual(code, 200)
        seg = self.segment(job_id, "jammed")

        code, _ = self.confirm_stop(job_id, seg["id"])
        self.assertEqual(code, 200)
        code, job = self.confirm_stop(job_id, seg["id"])
        self.assertEqual(code, 200, job)
        self.assertEqual(job["status"], "stopped")
        self.assertEqual(len(job["segments"]), 1)

        code, _ = self.switch(job_id)
        self.assertEqual(code, 200)
        code, body = self.confirm_stop(job_id, seg["id"])
        self.assertEqual(code, 409, body)
        self.assertEqual(body["code"], "stale")

    def test_jam_report_idempotent_same_segment(self):
        """同一次卡纸重复报告：第二次幂等返回，不重复作废。"""
        job_id = self.finalize(self.make_doc("重复报卡", 4))["id"]
        self.start(job_id, "P1")
        seg = self.segment(job_id, "active")
        att = self.current_attempt(job_id)
        code, _ = self.report_jam(job_id, seg["id"], att["id"])
        self.assertEqual(code, 200)
        code, job = self.report_jam(job_id, seg["id"], att["id"])
        self.assertEqual(code, 200, job)
        self.assertEqual(job["status"], "jammed")
        self.assertEqual(len([r for r in job["records"] if r["status"] == "scrapped"]), 1)

    def test_other_printer_activity_does_not_block_buttons(self):
        """另一台机的无关操作不影响本机按钮。"""
        job1 = self.finalize(self.make_doc("本机作业", 4))["id"]
        job2 = self.finalize(self.make_doc("邻机作业", 4))["id"]
        self.start(job1, "P1")
        self.start(job2, "P2")
        att1 = self.current_attempt(job1)

        # 二号机一连串无关操作：出纸、卡纸、停稳
        self.print_sheet(job2, 1)
        code, _ = self.report_jam(job2)
        self.assertEqual(code, 200)
        code, _ = self.confirm_stop(job2)
        self.assertEqual(code, 200)

        # 本机按之前看到的那次出纸提交，照常可用
        code, job = self.print_side(job1, "front", att1["id"])
        self.assertEqual(code, 200, job)
        code, job = self.print_side(job1, "back", att1["id"])
        self.assertEqual(code, 200, job)
        self.assertEqual(job["printed_sheets"], 1)

    def test_missing_target_rejected(self):
        """缺少操作对象的请求直接拒绝，状态不变。"""
        job_id = self.finalize(self.make_doc("缺少目标", 4))["id"]
        self.start(job_id, "P1")
        code, body = self.srv.post(f"/api/jobs/{job_id}/print", {"side": "front"})
        self.assertEqual(code, 400, body)
        code, body = self.srv.post(f"/api/jobs/{job_id}/jam", {})
        self.assertEqual(code, 400, body)
        code, body = self.srv.post(f"/api/jobs/{job_id}/stop", {})
        self.assertEqual(code, 400, body)
        self.assertEqual(self.job(job_id)["status"], "printing")
        self.assertEqual(self.printer("P1")["state"], "printing")

    def test_two_pages_interleaved(self):
        """两个页面交错试跑出纸、报卡、停稳：各按自己看到的那次提交。"""
        job_id = self.finalize(self.make_doc("双页面", 4))["id"]
        self.start(job_id, "P1")
        # 页面 A、页面 B 同时打开，看到同一次出纸
        page_a_attempt = self.current_attempt(job_id)["id"]
        page_b_attempt = self.current_attempt(job_id)["id"]

        # A 出正面；B 用同一次出纸出背面（那次出纸仍在进行，有效）
        code, _ = self.print_side(job_id, "front", page_a_attempt)
        self.assertEqual(code, 200)
        code, job = self.print_side(job_id, "back", page_b_attempt)
        self.assertEqual(code, 200, job)
        self.assertEqual(job["printed_sheets"], 1)

        # B 的页面还停在第 1 张：再点出正面 → 过期拒绝
        code, body = self.print_side(job_id, "front", page_b_attempt)
        self.assertEqual(code, 409, body)
        self.assertEqual(body["code"], "stale")

        # A 报卡；B 重复报同一次卡（幂等）；A 确认停稳；B 未换机再确认（成功）
        seg = self.segment(job_id, "active")["id"]
        att = self.current_attempt(job_id)["id"]
        code, _ = self.report_jam(job_id, seg, att)
        self.assertEqual(code, 200)
        code, _ = self.report_jam(job_id, seg, att)
        self.assertEqual(code, 200)
        code, _ = self.confirm_stop(job_id, seg)
        self.assertEqual(code, 200)
        code, _ = self.confirm_stop(job_id, seg)
        self.assertEqual(code, 200)

        # 换机后 B 的过期出纸点击仍被拒绝；A 刷新后正常续印到完成
        code, _ = self.switch(job_id)
        self.assertEqual(code, 200)
        code, body = self.print_side(job_id, "front", att)
        self.assertEqual(code, 409, body)
        self.assertEqual(body["code"], "stale")
        job = self.print_sheet(job_id, 2)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["printed_sheets"], 2)


class EdgeCaseTests(BaseCase):
    def test_jam_before_last_sheet_of_odd_document(self):
        """奇数页末张（背面留白）在出纸前卡纸：续印只需重出正面。"""
        job_id = self.finalize(self.make_doc("末张卡纸", 5))["id"]
        self.start(job_id, "P1")
        self.print_sheet(job_id, 1)
        self.print_sheet(job_id, 2)
        # 末张尚未完整出纸时卡住，半张作废，整张需重印
        code, _ = self.report_jam(job_id)
        self.assertEqual(code, 200)
        job = self.job(job_id)
        self.assertEqual(job["printed_sheets"], 2)
        self.assertEqual(job["status"], "jammed")
        code, _ = self.confirm_stop(job_id)
        self.assertEqual(code, 200)
        code, job = self.switch(job_id)
        self.assertEqual(code, 200, job)
        self.assertEqual(job["current_sheet"], 3)
        code, job = self.print_current(job_id, "front")
        self.assertEqual(code, 200, job)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["printed_sheets"], 3)
        rec = job["records"][0]
        self.assertIsNone(rec["back_page"])
        self.assertFalse(rec["required_back"])

    def test_printing_blocked_on_both_printers_after_jam(self):
        job_id = self.finalize(self.make_doc("停稳前禁印", 6))["id"]
        self.start(job_id, "P1")
        jammed_attempt = self.current_attempt(job_id)
        self.print_current(job_id, "front")
        code, _ = self.report_jam(job_id)
        self.assertEqual(code, 200)
        for side in ("front", "back"):
            code, body = self.print_side(job_id, side, jammed_attempt["id"])
            self.assertEqual(code, 409, body)
            self.assertEqual(body["code"], "stale")

    def test_collation_order_matches_original_pages(self):
        """出纸记录按张号给出正/背面页码，装订顺序即原页序。"""
        job_id = self.finalize(self.make_doc("装订核对", 5))["id"]
        self.start(job_id, "P1")
        for sheet in (1, 2, 3):
            self.print_sheet(job_id, sheet)
        job = self.job(job_id)
        collation = [(c["sheet_no"], c["front_page"], c["back_page"]) for c in job["collation"]]
        self.assertEqual(collation, [(1, 1, 2), (2, 3, 4), (3, 5, None)])


class RestartTests(BaseCase):
    def test_progress_survives_restart(self):
        job_id = self.finalize(self.make_doc("重启续印", 6))["id"]
        self.start(job_id, "P1")
        self.print_sheet(job_id, 1)
        self.print_current(job_id, "front")
        code, _ = self.report_jam(job_id)
        self.assertEqual(code, 200)
        code, _ = self.confirm_stop(job_id)
        self.assertEqual(code, 200)
        code, job = self.switch(job_id)
        self.assertEqual(code, 200, job)
        self.assertEqual(job["current_sheet"], 2)

        # 关停服务再启动，数据仍来自同一个 SQLite 文件
        self.srv.close()
        self.srv = ServerFixture(self.db_path)

        state = self.state()
        restored = next(j for j in state["jobs"] if j["id"] == job_id)
        self.assertEqual(restored["status"], "printing")
        self.assertEqual(restored["printed_sheets"], 1)
        self.assertEqual(restored["current_sheet"], 2)
        self.assertEqual(len(restored["segments"]), 2)
        self.assertEqual(self.printer("P2")["job_id"], job_id)
        records = [r for r in state["records"] if r["job_id"] == job_id]
        self.assertTrue(any(r["status"] == "scrapped" for r in records))
        self.assertTrue(any(r["status"] == "complete" for r in records))

        # 重启后继续从第 2 张续印
        job = self.print_sheet(job_id, 2)
        self.assertEqual(job["printed_sheets"], 2)
        job = self.print_sheet(job_id, 3)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["printed_sheets"], 3)

        # 重启后不会重复派续单
        code, _ = self.switch(job_id)
        self.assertEqual(code, 409)


class ConcurrencyTests(BaseCase):
    def test_duplicate_switch_requests_create_one_continuation(self):
        job_id = self.finalize(self.make_doc("并发换机", 8))["id"]
        self.start(job_id, "P1")
        self.print_sheet(job_id, 1)
        self.print_current(job_id, "front")
        code, _ = self.report_jam(job_id)
        self.assertEqual(code, 200)
        code, _ = self.confirm_stop(job_id)
        self.assertEqual(code, 200)

        results = []
        lock = threading.Lock()

        def worker():
            code, body = self.switch(job_id)
            with lock:
                results.append((code, body))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(all(code == 200 for code, _ in results), results)
        conts = [s for s in self.job(job_id)["segments"] if s["kind"] == "continuation"]
        self.assertEqual(len(conts), 1)

    def test_printer_cannot_take_two_active_jobs(self):
        job1 = self.finalize(self.make_doc("作业一", 2))["id"]
        job2 = self.finalize(self.make_doc("作业二", 2))["id"]
        self.start(job1, "P1")
        code, body = self.srv.post(f"/api/jobs/{job2}/start", {"printer_id": "P1"})
        self.assertEqual(code, 409, body)
        self.assertEqual(body["code"], "printer_busy")


class ServiceDirectTests(unittest.TestCase):
    """直接调用领域层，验证幂等与校验（绕过 HTTP）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "svc.db")
        from printroom import db as dbmod
        dbmod.init_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def _job(self, pages: int) -> int:
        doc = service.create_document(self.db, "直连")
        for i in range(pages):
            service.add_page(self.db, doc["id"], f"p{i}")
        return service.finalize_document(self.db, doc["id"])["id"]

    def _targets(self, job_id: int):
        job = service.get_job(self.db, job_id)
        att = next(r for r in job["records"] if r["status"] == "in_progress")
        seg = next(s for s in job["segments"] if s["status"] == "active")
        return seg["id"], att["id"]

    def test_jam_idempotent_and_stop_idempotent(self):
        job_id = self._job(4)
        service.start_job(self.db, job_id, "P1")
        seg_id, att_id = self._targets(job_id)
        service.print_side(self.db, job_id, "front", att_id)
        service.report_jam(self.db, job_id, seg_id, att_id)
        service.report_jam(self.db, job_id, seg_id, att_id)  # 重复报告不报错
        job = service.get_job(self.db, job_id)
        self.assertEqual(job["status"], "jammed")
        self.assertEqual(len([r for r in job["records"] if r["status"] == "scrapped"]), 1)

        service.confirm_stopped(self.db, job_id, seg_id)
        service.confirm_stopped(self.db, job_id, seg_id)  # 未换机，重复确认仍成功
        job = service.get_job(self.db, job_id)
        self.assertEqual(job["status"], "stopped")
        self.assertEqual(len(job["segments"]), 1)

    def test_jam_requires_printing(self):
        job_id = self._job(2)
        with self.assertRaises(service.ServiceError):
            service.report_jam(self.db, job_id, 999, 999)

    def test_stale_attempt_rejected_direct(self):
        job_id = self._job(4)
        service.start_job(self.db, job_id, "P1")
        seg_id, att_id = self._targets(job_id)
        service.report_jam(self.db, job_id, seg_id, att_id)
        with self.assertRaises(service.ServiceError) as ctx:
            service.print_side(self.db, job_id, "front", att_id)
        self.assertEqual(ctx.exception.code, "stale")


if __name__ == "__main__":
    unittest.main(verbosity=2)
