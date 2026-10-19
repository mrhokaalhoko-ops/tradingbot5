import unittest
from strategy.hybrid_logic import HybridStrategy

class TestHybridStrategy(unittest.TestCase):
    def setUp(self):
        self.strategy = HybridStrategy()
        self.ta_data = {"dummy": True}

    def test_no_ta_data_returns_hold(self):
        decision = self.strategy.decide("buy", {"label": "positive", "score": 0.8}, ta_data=None)
        self.assertEqual(decision["action"], "hold")
        self.assertIn("بيانات فنية مفقودة", decision["reason"])

    def test_invalid_ta_signal_returns_hold(self):
        decision = self.strategy.decide("neutral", {"label": "positive", "score": 0.8}, ta_data=self.ta_data)
        self.assertEqual(decision["action"], "hold")
        self.assertIn("لم يتم تأكيد الدخول", decision["reason"])

    def test_ta_only_mode_ignores_sentiment(self):
        decision = self.strategy.decide("buy", {"label": "negative", "score": 0.99}, ta_data=self.ta_data, sentiment_mode="ta_only")
        self.assertEqual(decision["action"], "buy")
        self.assertEqual(decision["risk_multiplier"], 1.0)
        self.assertIn("[فني فقط]", decision["reason"])

    def test_weight_mode_reduces_risk(self):
        # Buy with negative sentiment above threshold
        decision = self.strategy.decide("buy", {"label": "negative", "score": 0.6}, ta_data=self.ta_data, sentiment_mode="weight")
        self.assertEqual(decision["action"], "buy")
        self.assertEqual(decision["risk_multiplier"], 0.7)

    def test_fomo_protection_on_buy(self):
        # المشاعر الإيجابية المفرطة تُعامل كخطر قمة وتمنع الشراء.
        decision = self.strategy.decide("buy", {"label": "positive", "score": 0.8, "source": "lunarcrush"}, ta_data=self.ta_data, sentiment_mode="full")
        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["risk_multiplier"], 0.0)
        self.assertIn("FOMO", decision["reason"])

    def test_panic_protection_on_sell(self):
        # المشاعر السلبية المفرطة تُعامل كخطر قاع وتمنع البيع.
        decision = self.strategy.decide("sell", {"label": "negative", "score": 0.8, "source": "lunarcrush"}, ta_data=self.ta_data, sentiment_mode="full")
        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["risk_multiplier"], 0.0)
        self.assertIn("Panic", decision["reason"])

    def test_negative_veto_on_buy(self):
        decision = self.strategy.decide("buy", {"label": "negative", "score": 0.8}, ta_data=self.ta_data, sentiment_mode="full")
        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["risk_multiplier"], 0.0)

    def test_normalize_lunarcrush_galaxy_score(self):
        # galaxy_score of 75 should be normalized to 0.75
        normalized = self.strategy._normalize_sentiment({"label": "positive", "galaxy_score": 75})
        self.assertAlmostEqual(normalized["score"], 0.75, places=2)

    def test_rate_limited_sentiment_holds_in_full_mode(self):
        # بيانات 429 ليست مشاعر حقيقية ولا يجوز أن تؤكد الدخول.
        decision = self.strategy.decide(
            "buy",
            {"label": "neutral", "score": 0.5, "source": "lunarcrush_429_cooldown"},
            ta_data=self.ta_data,
            sentiment_mode="full",
        )
        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["risk_multiplier"], 0.0)

    def test_market_wide_cmc_sentiment_holds_in_full_mode(self):
        # Fear & Greed عام للسوق وليس خاصاً بالعملة.
        decision = self.strategy.decide(
            "sell",
            {"label": "neutral", "score": 0.5, "source": "cmc_fear_greed"},
            ta_data=self.ta_data,
            sentiment_mode="full",
        )
        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["risk_multiplier"], 0.0)

    def test_trusted_neutral_sentiment_allows_ta_signal(self):
        # neutral حقيقي من LunarCrush لا يضيف فيتو على الإشارة الفنية.
        decision = self.strategy.decide(
            "buy",
            {"label": "neutral", "score": 0.5, "source": "lunarcrush"},
            ta_data=self.ta_data,
            sentiment_mode="full",
        )
        self.assertEqual(decision["action"], "buy")
        self.assertEqual(decision["risk_multiplier"], 1.0)

if __name__ == "__main__":
    unittest.main()
