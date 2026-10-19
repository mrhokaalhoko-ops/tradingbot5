import os
import re


_SECRET_ENV_NAMES = (
    "BINANCE_API_KEY", "BINANCE_API_SECRET", "TELEGRAM_BOT_TOKEN",
    "LUNARCRUSH_API_KEY", "LUNARCRUSH_API_KEYS", "GROQ_API_KEY",
    "GROQ_API_KEYS", "GEMINI_API_KEY", "GEMINI_API_KEYS",
    "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS", "QWEN_API_KEY", "QWEN_API_KEYS",
    "OPENAI_API_KEY", "CRYPTOPANIC_API_KEY", "NEWS_API_KEY",
    "COINMARKETCAP_API_KEY", "COINGLASS_API_KEY", "COINGLASS_API_KEYS",
    "HUGGINGFACE_API_KEY",
)


def _masked(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    return f"{value[:4]}****"


def redact_secrets(value) -> str:
    """إخفاء الأسرار المعروفة والحقول الحساسة مع إبقاء أول أربعة أحرف فقط."""
    text = str(value if value is not None else "")

    # أولاً: القيم الفعلية الموجودة في البيئة، حتى إن ظهرت بلا اسم حقل.
    known = set()
    for name in _SECRET_ENV_NAMES:
        raw = os.getenv(name, "")
        if name.endswith("_API_KEYS"):
            known.update(part.strip() for part in raw.split(",") if part.strip())
        elif raw:
            known.add(raw)
    for secret in sorted(known, key=len, reverse=True):
        if len(secret) >= 6:
            text = text.replace(secret, _masked(secret))

    def _bearer(match):
        token = match.group(1)
        return f"Bearer {_masked(token)}"

    text = re.sub(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=-]{8,})", _bearer, text)

    # توكن Telegram له بنية مميزة وقد يظهر داخل URL.
    text = re.sub(
        r"\b(\d{6,}:[A-Za-z0-9_-]{20,})\b",
        lambda match: _masked(match.group(1)),
        text,
    )

    # حقول key/token/secret في نص، URL، dict أو JSON. لا نخفي المعرفات الطويلة
    # العامة مثل trade_id ما لم تأتِ بعد اسم حقل حساس.
    labelled = re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?key|secret|api[_-]?secret|token|authorization)\b"
        r"\s*[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9._~+/:-]{8,})"
    )
    text = labelled.sub(lambda match: match.group(1) + _masked(match.group(2)), text)
    return text
