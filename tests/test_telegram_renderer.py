import unittest
from unittest.mock import Mock, patch

from core.config import Config
from utils.telegram_bot import TelegramNotifier


class TestTelegramHtmlRenderer(unittest.TestCase):
    def _notifier(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.chat_id = "123"
        notifier.base_url = "https://telegram.invalid/botTEST"
        notifier._is_configured = Mock(return_value=True)
        return notifier

    def test_split_closes_and_reopens_active_tag(self):
        notifier = self._notifier()

        chunks = notifier._split_html_message("<b>" + "س" * 5000 + "</b>")

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 3900 for chunk in chunks))
        self.assertTrue(all(chunk.startswith("<b>") for chunk in chunks))
        self.assertTrue(all(chunk.endswith("</b>") for chunk in chunks))

    def test_entity_is_never_cut_between_chunks(self):
        notifier = self._notifier()
        source = "<code>" + ("&amp;" * 1200) + "</code>"

        chunks = notifier._split_html_message(source, limit=120)

        self.assertTrue(all(len(chunk) <= 120 for chunk in chunks))
        self.assertEqual(sum(chunk.count("&amp;") for chunk in chunks), 1200)
        self.assertFalse(any(chunk.endswith("&") for chunk in chunks))

    def test_rr_comparison_is_escaped_without_destroying_allowed_tags(self):
        notifier = self._notifier()

        sanitized = notifier._sanitize_html(
            "<b>RR</b>: 1.08R < 1.50R & waiting"
        )

        self.assertEqual(
            sanitized,
            "<b>RR</b>: 1.08R &lt; 1.50R &amp; waiting",
        )

    def test_keyboard_is_attached_only_to_last_chunk(self):
        notifier = self._notifier()
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        keyboard = {"inline_keyboard": [[{"text": "back", "callback_data": "back"}]]}

        with patch("utils.telegram_bot.requests.post", return_value=response) as post:
            self.assertTrue(notifier.send_message("x" * 5000, reply_markup=keyboard))

        payloads = [call.kwargs["json"] for call in post.call_args_list]
        self.assertGreater(len(payloads), 1)
        self.assertTrue(all("reply_markup" not in item for item in payloads[:-1]))
        self.assertEqual(payloads[-1]["reply_markup"], keyboard)

    def test_html_400_retries_plain_without_logging_response_body(self):
        notifier = self._notifier()
        bad = Mock(status_code=400, text="SECRET RESPONSE BODY")
        bad.json.return_value = {"description": "can't parse entities"}
        good = Mock(status_code=200)
        good.raise_for_status.return_value = None

        with patch(
            "utils.telegram_bot.requests.post", side_effect=[bad, good]
        ) as post, patch("utils.telegram_bot.logger.warning") as warning:
            self.assertTrue(notifier.send_message("<b>hello</b>", reply_markup={}))

        plain_payload = post.call_args_list[1].kwargs["json"]
        self.assertEqual(plain_payload["text"], "hello")
        self.assertNotIn("parse_mode", plain_payload)
        self.assertNotIn("SECRET RESPONSE BODY", warning.call_args.args[0])

    def test_scan_summary_explains_direction_stage_and_rr_in_plain_arabic(self):
        notifier = self._notifier()
        notifier._make_btn = Mock(return_value={"text": "button"})
        notifier.send_message = Mock(return_value=True)
        results = [
            {
                "coin": "BICO",
                "decision": "🔴 REJECT",
                "side": "buy",
                "price": 0.0188,
                "rsi": 39.4,
                "htf_trend": "bullish",
                "rejection_stage": "RR_FILTER",
                "reason": "تم حظر الصفقة: أول سيولة مقابلة تعطي RR غير كافٍ (0.38R < 1.50R).",
                "target_plan": {"rr": 0.38, "min_rr": 1.50},
                "candidate_assessment": {
                    "is_candidate": True,
                    "candidate_rank": 1,
                    "status": "WAITING_FOR_BETTER_RR",
                    "status_label_ar": "انتظار نقطة دخول بعائد/مخاطرة أفضل",
                    "pipeline_progress_pct": 40.0,
                    "next_trigger": "تحسن سعر الدخول ليصبح RR 1.50 على الأقل.",
                    "invalidation_level": 0.0179,
                },
            },
            {
                "coin": "AIN",
                "decision": "🔴 REJECT",
                "side": "sell",
                "price": 0.0755,
                "rsi": 55.5,
                "htf_trend": "bearish",
                "rejection_stage": "QUALITY_FILTER",
                "reason": "السعر ممتد بعيداً عن منطقة الدخول",
            },
        ]

        self.assertTrue(
            notifier.send_scan_summary(
                total_scanned=58,
                executed_count=0,
                trade_executed=False,
                scan_results=results,
            )
        )

        message = notifier.send_message.call_args.args[0]
        self.assertIn("ظهرت <b>2</b> أفكار مراقبة أو إشارات أولية", message)
        self.assertIn("🟢 LONG — شراء", message)
        self.assertIn("🔴 SHORT — بيع", message)
        self.assertIn("توقّف عند: <b>العائد مقابل المخاطرة</b>", message)
        self.assertIn("عائداً 0.38 مقابل مخاطرة 1", message)
        self.assertIn("اتجاه الساعة: <b>صاعد</b>", message)
        self.assertIn("انتظار نقطة دخول بعائد/مخاطرة أفضل", message)
        self.assertIn("المطلوب تالياً", message)
        self.assertIn("التفاصيل ومراحل القرار كاملة داخل Mini App", message)
        self.assertNotIn("لا توجد فرص قوية", message)

    def test_scan_summary_labels_analysis_bias_as_watch_only(self):
        notifier = self._notifier()
        notifier._make_btn = Mock(return_value={"text": "button"})
        notifier.send_message = Mock(return_value=True)

        notifier.send_scan_summary(
            total_scanned=50,
            executed_count=0,
            trade_executed=False,
            scan_results=[{
                "coin": "WATCH",
                "decision": "🟡 HOLD",
                "price": 1.25,
                "rsi": 54,
                "candidate_assessment": {
                    "is_candidate": True,
                    "candidate_rank": 1,
                    "status": "SETUP_FORMING",
                    "status_label_ar": "الفكرة قيد التكوين",
                    "bias": "BUY",
                    "execution_side_confirmed": False,
                    "next_trigger": "إغلاق 1H فوق المقاومة",
                },
            }],
        )

        message = notifier.send_message.call_args.args[0]
        self.assertIn("فكرة قيد المراقبة", message)
        self.assertIn("ميل المراقبة: <b>🟢 LONG — شراء</b>", message)
        self.assertNotIn("الاتجاه المقترح", message)

    def test_scan_summary_uses_canonical_runtime_webapp_url(self):
        notifier = self._notifier()
        notifier.send_message = Mock(return_value=True)

        with patch.object(Config, "WEBAPP_URL", "https://current.example/dashboard"):
            notifier.send_scan_summary(50, 0, False, [])

        keyboard = notifier.send_message.call_args.kwargs["reply_markup"]
        self.assertEqual(
            keyboard["inline_keyboard"][0][0]["web_app"]["url"],
            "https://current.example/dashboard/",
        )

    def test_menu_button_updates_default_and_authorized_chat(self):
        notifier = self._notifier()
        notifier._post = Mock(side_effect=[{"ok": True}, {"ok": True}])

        with patch.object(Config, "WEBAPP_URL", "https://current.example/app"):
            notifier._set_webapp_menu_button()

        self.assertEqual(notifier._post.call_count, 2)
        default_payload = notifier._post.call_args_list[0].args[1]
        chat_payload = notifier._post.call_args_list[1].args[1]
        self.assertNotIn("chat_id", default_payload)
        self.assertEqual(chat_payload["chat_id"], "123")
        self.assertEqual(
            default_payload["menu_button"]["web_app"]["url"],
            "https://current.example/app/",
        )

    def test_reports_menu_exposes_shadow_hot_list_without_trade_action(self):
        notifier = self._notifier()
        notifier._make_btn = lambda text, callback, *_args, **_kwargs: {
            "text": text,
            "callback_data": callback,
        }

        buttons = [
            button
            for row in notifier.reports_menu_keyboard()["inline_keyboard"]
            for button in row
        ]
        hot_button = next(
            button for button in buttons if button["callback_data"] == "hot_list_shadow"
        )
        self.assertIn("مراقبة فقط", hot_button["text"])


if __name__ == "__main__":
    unittest.main()
