import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.core.config import Settings
from backend.services.ocr_service import OCRError, _ocr_lock
from backend.services.rag_service import AnswerError, _generate, _local_address, answer_question
from backend.services.retrieval_service import SearchResult


def item(text="骨折とは骨の連続性が損なわれた状態である。"):
    return SearchResult(1, "教科書", "第1版", 3, None, text, 1, "ocr", "unreviewed", True)


def reply(status="answered", claims=None):
    if claims is None:
        claims = [{"text": "骨折は骨の連続性が損なわれた状態です。", "quote": "骨の連続性が損なわれた", "evidence_id": 1}]
    return {"done": True, "response": json.dumps({"status": status, "claims": claims}, ensure_ascii=False)}


class AnswerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name)
        self.settings = Settings(path, path, "http://127.0.0.1:11434", "qwen2.5:3b", False)

    def test_no_evidence_never_calls_model(self):
        with patch("backend.services.rag_service._generate") as generate:
            answer = answer_question(self.settings, "骨折とは？", [])
        self.assertEqual(answer.status, "insufficient")
        generate.assert_not_called()

    def test_valid_answer_keeps_source_and_quote(self):
        with patch("backend.services.rag_service._generate", return_value=reply()) as generate:
            answer = answer_question(self.settings, "骨折とは？", [item()])
        self.assertEqual(answer.claims[0].quote, "骨の連続性が損なわれた")
        self.assertEqual(answer.evidence[0].review_status, "unreviewed")
        self.assertIn("骨折", generate.call_args.args[1]["prompt"])
        with patch("backend.services.rag_service._generate", return_value=reply(claims=[
            {"text": "骨折の説明", "quote": "骨の連続性", "evidence_id": "1"}
        ])):
            self.assertEqual(len(answer_question(self.settings, "骨折とは", [item()]).claims), 1)

    def test_model_insufficient_and_invalid_citations_are_not_displayed(self):
        with patch("backend.services.rag_service._generate", side_effect=[
            reply(status="insufficient"), {"done": True, "response": '{"ids":[]}'}
        ]):
            self.assertEqual(answer_question(self.settings, "質問", [item()]).status, "insufficient")
        invalid = [
            {"text": "答え", "quote": "存在しない引用", "evidence_id": 1},
            {"text": "答え", "quote": "骨の連続性", "evidence_id": 2},
            {"text": "答え", "quote": "骨の連続性", "evidence_id": True},
            {"text": "答え", "quote": "骨", "evidence_id": 1},
            {"text": "", "quote": "骨の連続性", "evidence_id": 1},
        ]
        for claim in invalid:
            with self.subTest(claim=claim), patch(
                "backend.services.rag_service._generate", return_value=reply(claims=[claim])
            ), self.assertRaises(AnswerError):
                answer_question(self.settings, "質問", [item()])

    def test_extract_fallback_preserves_verbatim_text_and_source(self):
        evidence = [
            item("見出し。骨折は骨の連続性が損なわれた状態である。"),
            item("別の資料。原因は外傷であると記載されています。"),
        ]
        with patch("backend.services.rag_service._generate", side_effect=[
            reply(status="insufficient"), {"done": True, "response": '{"ids":[1,2]}'}
        ]):
            answer = answer_question(self.settings, "骨折とは？", evidence)
        self.assertEqual(answer.status, "extractive")
        self.assertEqual(len(answer.claims), 2)
        for claim in answer.claims:
            self.assertEqual(claim.text, claim.quote)
            self.assertIn(claim.text, evidence[claim.evidence_index].text)
        self.assertEqual([claim.evidence_index for claim in answer.claims], [0, 1])

    def test_extract_fallback_rejects_bad_or_incomplete_model_output(self):
        responses = [
            {"done": True, "response": '{"ids":[1,1]}'},
            {"done": True, "response": '{"ids":[999]}'},
            {"done": True, "response": '{"ids":["1"]}'},
            {"done": True, "response": '{"ids":[true]}'},
            {"done": True, "response": '{"ids":[[1]]}'},
            {"done": True, "response": "not json"},
            {"done": True, "done_reason": "length", "response": '{"ids":[1]}'},
            {"done": True, "prompt_eval_count": 8064, "response": '{"ids":[1]}'},
        ]
        for response in responses:
            with self.subTest(response=response), patch(
                "backend.services.rag_service._generate",
                side_effect=[reply(status="insufficient"), response],
            ), self.assertRaises(AnswerError):
                answer_question(self.settings, "質問", [item()])

    def test_invalid_json_and_output_requirements(self):
        for response in (
            {"done": True, "response": "not json"},
            {"done": False, "response": "{}"},
            {"done": True, "done_reason": "length", "response": "{}"},
            {"done": True, "prompt_eval_count": 7492, "response": "{}"},
            {"done": True, "response": '{"status":"answered","claims":[]}'},
        ):
            with self.subTest(response=response), patch(
                "backend.services.rag_service._generate", return_value=response
            ), self.assertRaises(AnswerError):
                answer_question(self.settings, "質問", [item()])
        with self.assertRaises(AnswerError):
            answer_question(self.settings, "", [item()])
        with self.assertRaises(AnswerError):
            answer_question(self.settings, "質問", [item()] * 4)
        with patch("backend.services.rag_service._generate", return_value=reply(claims=[
            {"text": "骨折とは？", "quote": "骨の連続性", "evidence_id": 1}
        ])), self.assertRaises(AnswerError):
            answer_question(self.settings, "骨折とは？", [item()])

    def test_remote_or_malformed_urls_rejected(self):
        for url in ("http://example.com:11434", "http://localhost:11434", "https://127.0.0.1",
                    "http://127.0.0.1@evil.example", "http://127.0.0.1/path",
                    "http://127.0.0.1:99999", "http://127.0.0.1?x=1"):
            with self.subTest(url=url), self.assertRaises(AnswerError):
                _local_address(url)
        self.assertEqual(_local_address("http://127.0.0.1:11434"), ("127.0.0.1", 11434))
        remote = Settings(self.settings.project_root, self.settings.data_dir,
                          "http://example.com:11434", self.settings.ollama_model, False)
        with patch("backend.services.rag_service.http.client.HTTPConnection") as connection:
            with self.assertRaises(AnswerError):
                _generate(remote, {"prompt": "private text"})
            connection.assert_not_called()

    def test_ocr_and_generation_cannot_run_concurrently(self):
        with _ocr_lock(self.settings.data_dir):
            with patch("backend.services.rag_service._generate") as generate:
                with self.assertRaises(OCRError):
                    answer_question(self.settings, "質問", [item()])
                generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
