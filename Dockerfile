FROM python:3.11-slim

# نصب وابستگی‌های پایه (برای timezone و …)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# نصب کتابخانه‌های پایتون (شامل JobQueue)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir "python-telegram-bot[job-queue]>=20,<21" python-dateutil

# کپی سورس داخل کانتینر
COPY telegram_subscription_bot.py /app/telegram_subscription_bot.py

# فولدر دیتا برای دیتابیس
RUN mkdir -p /app/data

# متغیرهای پیش‌فرض (قابل override با .env)
ENV TZ=Asia/Tehran \
    DB_PATH=/app/data/data.db

# اجرای ربات
CMD ["python", "-u", "/app/telegram_subscription_bot.py"]