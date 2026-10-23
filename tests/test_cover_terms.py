import unittest

import classify_library as C


class CoverTitle(unittest.TestCase):
    def check(self, cover, want):
        self.assertEqual(C.cover_type(cover), want, cover)

    def test_languages(self):
        self.check("Relatório Anual de Sustentabilidade 2024 Grupo X", "Sustainability Report")
        self.check("RELATÓRIO ANUAL 2024", "Annual Report")
        self.check("統合報告書 2024", "Integrated Report")
        self.check("PT ABC Tbk Laporan Tahunan 2024 Laporan Keuangan", "Annual Report")
        self.check("Halbjahresfinanzbericht zum 30. Juni 2025", "Interim Reports")
        self.check("中国石油 2024年度报告", "Annual Report")
        self.check("2025半年度报告", "Interim Reports")
        self.check("2024 环境、社会及管治报告", "ESG Report")
        self.check("Bilancio di Sostenibilita 2024", "Sustainability Report")
        self.check("Código de Conduta e Ética", "Code of Conduct")
        self.check("Document d'enregistrement universel 2024", "Registration Document")
        self.check("Politique des droits de l'homme", "Human Rights Policy")

    def test_first_phrase_wins_and_longer_breaks_ties(self):
        self.check("Informe Anual 2024 ... Informe de Sostenibilidad", "Annual Report")
        self.check("Relatório e Contas 2024 incluindo Demonstrações Financeiras", "Annual Report")

    def test_word_edges_and_nothing(self):
        self.check("Our informations about the jaarverslagen archive", "")
        self.check("", "")

    def test_only_after_address_and_file_name(self):
        self.assertEqual(C.classify_from_file("annual-report-2024.pdf", "Relatório de Sustentabilidade"),
                         ("Annual Report", "file name"))
        self.assertEqual(C.classify_from_file("download.pdf", "Relatório de Sustentabilidade 2024"),
                         ("Sustainability Report", "cover title"))

    def test_retype_writes_only_found_types(self):
        recs = [{"id": "r1", "fields": {"cover_text": "Nachhaltigkeitsbericht 2024", "file": [{"filename": "x.pdf"}]}},
                {"id": "r2", "fields": {"cover_text": "zzz", "file": [{"filename": "y.pdf"}]}}]
        updates, by = C.retype(recs)
        self.assertEqual([u["id"] for u in updates], ["r1"])
        self.assertEqual(updates[0]["fields"]["match_confidence"], "cover title")


if __name__ == "__main__":
    unittest.main()
