#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webapp.py — необязательная обёртка для PythonAnywhere.

Зачем: на бесплатном тарифе PythonAnywhere (2026) нет задач по расписанию,
поэтому проверку запускает внешний бесплатный планировщик (cron-job.org),
который раз в 10 минут открывает URL:

    https://ТВОЙ_ЛОГИН.pythonanywhere.com/run?token=СЕКРЕТ

Секрет задаётся переменной окружения WEBHOOK_TOKEN (или в .env).
Если WEBHOOK_TOKEN пустой — endpoint открыт всем (не рекомендуется).

Локальный запуск для проверки:  python webapp.py  →  http://127.0.0.1:5000/run
"""

import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import notifier  # noqa: E402  (импорт после настройки sys.path)

notifier.load_dotenv(notifier.ENV_PATH)  # WEBHOOK_TOKEN тоже может лежать в .env

app = Flask(__name__)


@app.route("/run")
def run_check():
    expected = os.environ.get("WEBHOOK_TOKEN", "").strip()
    if expected and request.args.get("token") != expected:
        return jsonify(ok=False, error="bad token"), 403
    code = notifier.main([])  # одна проверка: те же config.json / seen.json / .env
    return jsonify(ok=code == 0, exit_code=code)


@app.route("/")
def index():
    return "content-notifier is alive"


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
