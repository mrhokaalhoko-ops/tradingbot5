import os
from unittest.mock import Mock, patch

from utils.telegram_bot import TelegramNotifier


def test_callback_rejects_other_member_of_authorized_group():
    notifier = TelegramNotifier.__new__(TelegramNotifier)
    notifier.chat_id = "-100123"
    notifier.answer_callback = Mock()
    callback = {
        "id": "callback-1",
        "data": "gui_home",
        "from": {"id": 999},
        "message": {"message_id": 10, "chat": {"id": -100123, "type": "group"}},
    }

    with patch.dict(os.environ, {"TELEGRAM_WEBAPP_USER_ID": "123"}, clear=False):
        notifier.handle_callback_query(
            callback, Mock(), Mock(), Mock(), Mock(), Mock(), Mock()
        )

    notifier.answer_callback.assert_called_once_with(
        "callback-1", "غير مصرح لك باستخدام هذا البوت.", show_alert=True
    )


def test_actor_fails_closed_when_admin_id_is_unset():
    with patch.dict(os.environ, {}, clear=True):
        assert TelegramNotifier._authorized_actor_user_id(999) is False
