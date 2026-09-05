import json
import os
from pathlib import Path
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

from app import llm, extract, vision
from app.ingest import parse_pdf, _caption_role
from app.models import ArticleMeta, ExtractedArticle
from scripts.repair_codex_providers import patch_config


class ModelSwitchTests(unittest.TestCase):
    def setUp(self):
        self.settings = json.loads((Path(__file__).parents[1] / "models.example.json").read_text(encoding="utf-8"))

    def test_requests_use_separate_keys_models_and_parameters(self):
        for provider, key in [("deepseek", "test-ds"), ("gpt", "test-gpt")]:
            for role in ("text", "vision"):
                self.settings["active_provider"] = provider
                client = MagicMock()
                client.__enter__.return_value = client
                client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="reaction"))])
                with patch.dict(os.environ, {"OPENAI_API_KEY": "test-ds", "GPT_API_KEY": "test-gpt"}), patch.object(llm, "read_settings", return_value=self.settings), patch("openai.OpenAI", return_value=client) as factory:
                    self.assertEqual(llm.complete(role, []), "reaction")
                self.assertEqual(factory.call_args.kwargs["api_key"], key)
                self.assertEqual(factory.call_args.kwargs["base_url"], self.settings["providers"][provider]["base_url"])
                request = client.chat.completions.create.call_args.kwargs
                self.assertEqual(request["model"], self.settings["providers"][provider][role + "_model"])
                self.assertEqual("reasoning_effort" in request, provider == "deepseek")

    def test_mixed_providers(self):
        self.settings["vision_provider"] = "gpt"
        self.assertEqual(llm.resolve("text", self.settings)[0], "deepseek")
        self.assertEqual(llm.resolve("vision", self.settings)[0], "gpt")

    def test_missing_key_never_falls_back_to_other_provider(self):
        self.settings["active_provider"] = "gpt"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-ds", "GPT_API_KEY": ""}), patch.object(llm, "read_settings", return_value=self.settings), patch.object(llm, "dotenv_values", return_value={}), patch("openai.OpenAI") as client:
            with self.assertRaises(ValueError):
                llm.complete("text", [])
            client.assert_not_called()

    def test_same_extraction_schema(self):
        with patch.object(llm, "complete", return_value='{"article":{"title":"测试"},"keywords":{"tags":["氧化"]}}'):
            result = extract._chat("文献正文")
        self.assertEqual(result.article.title, "测试")
        self.assertEqual(result.keywords.tags, ["氧化"])

    def test_image_retry_and_role(self):
        with tempfile.TemporaryDirectory() as td:
            image = Path(td) / "test.png"
            image.write_bytes(b"test")
            with patch.object(llm, "complete", side_effect=[ValueError("empty"), "mechanism"]):
                self.assertEqual(vision.classify_image(str(image)), "mechanism")

    def test_codex_repair_preserves_selection_and_unrelated_settings(self):
        original = 'model = "gpt-6-astra"\n[desktop]\nx = true\n'
        fixed = patch_config(original)
        data = tomllib.loads(fixed)
        self.assertEqual(data["model"], "gpt-6-astra")
        self.assertTrue(data["desktop"]["x"])
        self.assertFalse(data["model_providers"]["deepseek"]["requires_openai_auth"])
        self.assertEqual(patch_config(fixed), fixed)

    def test_codex_repair_refuses_other_custom_provider(self):
        with self.assertRaises(ValueError):
            patch_config('[model_providers.custom]\nbase_url="https://example.com"\n')

    def test_switch_without_gpt_key_keeps_saved_settings(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "models.local.json"
            original = json.dumps(self.settings)
            path.write_text(original, encoding="utf-8")
            with patch.object(llm, "SETTINGS_PATH", path), patch.object(llm, "read_settings", return_value=self.settings), patch.object(llm, "api_key", return_value=""), patch("sys.argv", ["llm", "switch", "gpt"]), patch("sys.stderr"):
                with self.assertRaises(SystemExit):
                    llm.main()
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_switch_writes_both_routes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "models.local.json"
            with patch.object(llm, "SETTINGS_PATH", path), patch.object(llm, "read_settings", return_value=self.settings), patch.object(llm, "api_key", return_value="test"), patch("sys.argv", ["llm", "switch", "gpt"]), patch("builtins.print"):
                llm.main()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["text_provider"], "gpt")
            self.assertEqual(saved["vision_provider"], "gpt")
            self.assertEqual(saved["providers"]["deepseek"], self.settings["providers"]["deepseek"])

    def test_pdf_metadata_refinement_preserves_source_and_acs_citation(self):
        meta = ArticleMeta(title="Deoxytrifluoromethylation of Alcohols", source_type="journal", source_url="archive.pdf")
        text = "Deoxytrifluoromethylation of Alcohols\nCite This: J. Am. Chem. Soc. 2022, 144, 11961-11968\nhttps://doi.org/10.1021/jacs.2c04807"
        result = extract._refine(meta, text, ExtractedArticle())
        self.assertEqual(result.article.source_type, "journal")
        self.assertEqual(result.article.source_url, "archive.pdf")
        self.assertEqual(result.article.journal, "J. Am. Chem. Soc.")
        self.assertEqual(result.article.year, "2022")
        self.assertEqual(result.article.doi, "10.1021/jacs.2c04807")

    def test_pdf_fallback_extracts_reaction_factors_and_mechanism(self):
        meta = ArticleMeta(title="Deoxytrifluoromethylation of Alcohols", source_type="journal", source_url="archive.pdf")
        text = ("Our mechanistic design is detailed in Figure 2. Alcohol 1 is activated by NHC salt 2.\n"
                "Following an extensive optimization campaign, we identified the conditions outlined in Table 1 as optimal. "
                "Alcohol 15 was condensed with NHC salt 2 under mildly basic conditions, then subjected to irradiation with blue light, "
                "along with 1 mol % photocatalyst 4, 5 mol % Cu(terpy)Cl2, 1.5 equiv of dMesSCF3, 1.6 equiv of quinuclidine, "
                "and 2 equiv of TBACl in DMSO. After 8 h, the trifluoromethylated product 16 was obtained in 84% yield.\n"
                "The presence of exogenous chloride anion (Cl-) proved critical to the overall success of this transformation.")
        with patch.object(llm, "complete", return_value=""):
            result = extract.extract(meta, text, [])
        self.assertEqual(len(result.reactions), 1)
        self.assertIn("Cu(terpy)Cl2", result.reactions[0].conditions_text)
        self.assertIn("关键影响因素", result.substrate_scope.notes)
        self.assertTrue(result.mechanism.overall)

    def test_pdf_caption_roles_keep_conditions_out_of_reaction_slot(self):
        self.assertEqual(_caption_role("Figure 1. Deoxytrifluoromethylation"), "reaction")
        self.assertEqual(_caption_role("Figure 2. Plausible mechanism"), "mechanism")
        self.assertEqual(_caption_role("Table 1. Control Reactions"), "conditions")
        self.assertEqual(_caption_role("Table 2. Scope of Reaction"), "scope")
        self.assertEqual(_caption_role("Table 1. Control Reactions", 988, 215), "reaction")
        self.assertEqual(_caption_role("Figure 1. Overview", 999, 2040), "other")


if __name__ == "__main__":
    unittest.main()
