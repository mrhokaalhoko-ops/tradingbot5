import sys
import types
import unittest
import os
from unittest.mock import Mock, patch


# The UI test must never contact Telegram or depend on optional network packages.
if "requests" not in sys.modules:
    sys.modules["requests"] = types.ModuleType("requests")

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub

from core.version import APP_VERSION, SHORT_COMMIT, VERSION_LABEL
from core.config import Config
from utils.telegram_bot import TelegramNotifier


class TestVersionUI(unittest.TestCase):
    def _notifier_without_network(self):
        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.chat_id = "123"
        notifier.ICONS = {}
        return notifier

    def test_main_menu_contains_version_and_commit(self):
        notifier = self._notifier_without_network()
        keyboard = notifier.main_menu_keyboard()
        buttons = [
            button
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        version_button = next(
            button for button in buttons
            if button.get("callback_data") == "gui_version"
        )

        self.assertIn(APP_VERSION, version_button["text"])
        self.assertIn(SHORT_COMMIT, version_button["text"])
        self.assertEqual(VERSION_LABEL, f"{APP_VERSION} • {SHORT_COMMIT}")

    def test_main_menu_contains_demo_signal_mode_switch(self):
        notifier = self._notifier_without_network()
        keyboard = notifier.main_menu_keyboard()
        callback_values = {
            button.get("callback_data")
            for row in keyboard["inline_keyboard"]
            for button in row
        }
        self.assertIn("gui_demo_signal_mode", callback_values)

    def test_main_menu_uses_canonical_runtime_webapp_url(self):
        notifier = self._notifier_without_network()
        with patch.object(Config, "WEBAPP_URL", "https://current.example/app"):
            keyboard = notifier.main_menu_keyboard()

        dashboard = keyboard["inline_keyboard"][0][0]
        self.assertEqual(
            dashboard["web_app"]["url"],
            "https://current.example/app/",
        )

    def test_webapp_url_rejects_non_https_configuration(self):
        notifier = self._notifier_without_network()
        with patch.object(Config, "WEBAPP_URL", "http://unsafe.example"):
            with self.assertRaisesRegex(RuntimeError, "HTTPS"):
                notifier._webapp_url()

    def test_demo_signal_mode_keyboard_marks_current_mode(self):
        notifier = self._notifier_without_network()
        state = Mock()
        state.get_demo_signal_execution_mode.return_value = "hybrid_decision"
        keyboard = notifier.demo_signal_mode_keyboard(state)
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        hybrid = next(
            button for button in buttons
            if button.get("callback_data") == "demo_signal_mode_hybrid_decision"
        )
        self.assertIn("✅", hybrid["text"])

    def test_version_callback_renders_details(self):
        notifier = self._notifier_without_network()
        captured = {}
        notifier.answer_callback = lambda *args, **kwargs: None

        def capture_edit(chat_id, message_id, text, reply_markup):
            captured.update(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )

        notifier.edit_message = capture_edit
        callback = {
            "id": "cb-1",
            "data": "gui_version",
            "from": {"id": 123},
            "message": {
                "message_id": 77,
                "chat": {"id": "123"},
            },
        }

        with patch.dict(os.environ, {"TELEGRAM_WEBAPP_USER_ID": "123"}):
            notifier.handle_callback_query(
                callback,
                client=None,
                state_manager=None,
                ai_engine=None,
                ta_engine=None,
                news_engine=None,
                social_engine=None,
            )

        self.assertEqual(captured["chat_id"], "123")
        self.assertEqual(captured["message_id"], 77)
        self.assertIn(APP_VERSION, captured["text"])
        self.assertIn(SHORT_COMMIT, captured["text"])


if __name__ == "__main__":
    unittest.main()
