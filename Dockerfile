# استخدام نسخة بايثون خفيفة
FROM python:3.10-slim

# إعداد بيئة العمل
WORKDIR /app

# نسخ ملف المتطلبات وتثبيتها
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir jsonschema==4.25.1

# نسخ باقي ملفات المشروع
COPY . .

# هذا الفرع يشغّل عقل Telegram الحالي نفسه داخل خدمة Demo المعزولة. حراس
# TELEGRAM_PROPOSAL_DEMO_MODE/DEMO_EXECUTION_ENABLED يمنعون أي أمر خارج
# demo-fapi.binance.com، ولا تُمنح رسائل watchlist/shadow صلاحية تنفيذ.
CMD ["python", "-u", "main.py"]
