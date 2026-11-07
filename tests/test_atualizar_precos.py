import unittest

from atualizar_precos import build_product_result, collect_offers, matching_model, normalize, update_history


class PriceParsingTests(unittest.TestCase):
    def setUp(self):
        self.product = {
            "id": "gpu-rx6600",
            "models": ["rx 6600 8gb"],
        }

    def test_normalize_handles_accents_case_and_memory_spacing(self):
        self.assertEqual(normalize("GeForce RTX 4060 8 GB"), "geforce rtx 4060 8gb")
        self.assertEqual(normalize("Memória RAM"), "memoria ram")

    def test_matching_requires_the_configured_model_phrase(self):
        self.assertEqual(matching_model("Placa Radeon RX 6600 8GB", self.product["models"]), "rx 6600 8gb")
        self.assertIsNone(matching_model("Placa Radeon RX 7600 8GB", self.product["models"]))
        self.assertIsNone(matching_model("Razer Cobra Pro wireless", ["razer cobra"], ["cobra pro"]))

    def test_collect_offers_discards_wrong_models_and_duplicate_urls(self):
        items = [
            {"title": "Radeon RX 6600 8GB", "extracted_price": 1400, "source": "Loja A", "product_link": "https://loja-a.example/produto"},
            {"title": "Radeon RX 6600 8GB", "extracted_price": 1500, "source": "Loja A", "product_link": "https://loja-a.example/produto"},
            {"title": "Radeon RX 7600 8GB", "extracted_price": 1800, "source": "Loja B", "product_link": "https://loja-b.example/produto"},
            {"title": "Radeon RX 6600 8GB", "extracted_price": 1350, "source": "Loja C", "product_link": "http://loja-c.example/produto"},
        ]
        offers = collect_offers(items, self.product["models"])
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["price"], 1400)

    def test_only_marks_price_range_verified_with_minimum_sample(self):
        offers = [
            {"title": "RX 6600 8GB A", "extracted_price": 1300, "source": "Loja A", "product_link": "https://a.example/1"},
            {"title": "RX 6600 8GB B", "extracted_price": 1400, "source": "Loja B", "product_link": "https://b.example/1"},
        ]
        result = build_product_result(self.product, offers, "2026-11-07T12:00:00Z")
        self.assertEqual(result["status"], "insufficient_data")
        self.assertNotIn("minimum_price", result)

    def test_verified_range_uses_distinct_real_offers(self):
        offers = [
            {"title": "RX 6600 8GB A", "extracted_price": 1300, "source": "Loja A", "product_link": "https://a.example/1"},
            {"title": "RX 6600 8GB B", "extracted_price": 1400, "source": "Loja B", "product_link": "https://b.example/1"},
            {"title": "RX 6600 8GB C", "extracted_price": 1500, "source": "Loja C", "product_link": "https://c.example/1"},
        ]
        result = build_product_result(self.product, offers, "2026-11-07T12:00:00Z")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["minimum_price"], 1300)
        self.assertEqual(result["maximum_price"], 1500)
        self.assertEqual(result["store_count"], 3)
        self.assertEqual(result["models"][0]["median_price"], 1400)

    def test_multiple_models_keep_separate_price_ranges(self):
        product = {
            "id": "gpu",
            "models": ["rx 7600 8gb", "rtx 4060 8gb"],
        }
        offers = [
            {"title": "Radeon RX 7600 8GB A", "extracted_price": 1800, "source": "Loja A", "product_link": "https://a.example/rx"},
            {"title": "Radeon RX 7600 8GB B", "extracted_price": 1900, "source": "Loja B", "product_link": "https://b.example/rx"},
            {"title": "Radeon RX 7600 8GB C", "extracted_price": 2000, "source": "Loja C", "product_link": "https://c.example/rx"},
            {"title": "GeForce RTX 4060 8GB A", "extracted_price": 2100, "source": "Loja A", "product_link": "https://a.example/rtx"},
            {"title": "GeForce RTX 4060 8GB B", "extracted_price": 2200, "source": "Loja B", "product_link": "https://b.example/rtx"},
            {"title": "GeForce RTX 4060 8GB C", "extracted_price": 2300, "source": "Loja C", "product_link": "https://c.example/rtx"},
        ]
        result = build_product_result(product, offers, "2026-11-07T12:00:00Z")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(len(result["models"]), 2)
        self.assertEqual(result["models"][0]["minimum_price"], 1800)
        self.assertEqual(result["models"][0]["maximum_price"], 2000)
        self.assertEqual(result["models"][1]["minimum_price"], 2100)
        self.assertEqual(result["models"][1]["maximum_price"], 2300)

    def test_history_calculates_change_only_against_a_prior_verified_sample(self):
        first_sample = [
            {"title": "RX 6600 8GB A", "extracted_price": 1300, "source": "Loja A", "product_link": "https://a.example/1"},
            {"title": "RX 6600 8GB B", "extracted_price": 1400, "source": "Loja B", "product_link": "https://b.example/1"},
            {"title": "RX 6600 8GB C", "extracted_price": 1500, "source": "Loja C", "product_link": "https://c.example/1"},
        ]
        second_sample = [
            {"title": "RX 6600 8GB A", "extracted_price": 1400, "source": "Loja A", "product_link": "https://a.example/1"},
            {"title": "RX 6600 8GB B", "extracted_price": 1500, "source": "Loja B", "product_link": "https://b.example/1"},
            {"title": "RX 6600 8GB C", "extracted_price": 1600, "source": "Loja C", "product_link": "https://c.example/1"},
        ]
        previous = build_product_result(self.product, first_sample, "2026-11-06T12:00:00Z")
        previous = update_history(previous, None, "2026-11-06T12:00:00Z")
        current = build_product_result(self.product, second_sample, "2026-11-07T12:00:00Z")
        current = update_history(current, previous, "2026-11-07T12:00:00Z")
        self.assertEqual(len(current["history"]), 2)
        self.assertEqual(current["models"][0]["change_since_previous"], 100)
        self.assertEqual(current["models"][0]["compared_to"], "2026-11-06")


if __name__ == "__main__":
    unittest.main()
