"""
Backend для Mini App "Стилист": Flask API + Telegram-бот (для оплаты).

АРХИТЕКТУРА:
- Mini App (index.html) отправляет запросы сюда: /api/status и /api/advice.
- Telegram-бот в этом же файле обрабатывает /start (открывает Mini App)
  и /subscribe (выставляет счёт в Telegram Stars).
- Оба процесса (Flask и бот) работают одновременно в одном скрипте.

ВАЖНО — ГДЕ ЭТО ЗАПУСКАТЬ:
GitHub Pages НЕ подходит для этого файла — он хостит только статичные
страницы (HTML/CSS/JS), а не запускает Python и не хранит секретные ключи.
Этот файл нужно разместить на сервисе, который умеет запускать Python
24/7, например:
  - render.com (есть бесплatный план для таких проектов)
  - railway.app
  - обычный VPS

ЧТО НУЖНО ПЕРЕД ЗАПУСКОМ:
  pip install flask flask-cors python-telegram-bot openai

ПРИМЕЧАНИЕ: Groq часто обновляет список моделей. Если появится ошибка
"model not found" — смотрите актуальный список на
https://console.groq.com/docs/models
"""

import json
import os
import hmac
import hashlib
import threading
from datetime import datetime, timedelta
from urllib.parse import parse_qsl

from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI
from telegram import Update, LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application, CommandHandler, PreCheckoutQueryHandler, MessageHandler,
    filters, ContextTypes
)

# ==== КЛЮЧИ БЕРУТСЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ (настраиваются на Render, см. инструкцию) ====
# Для локального теста на своём компьютере можно временно вписать значения
# прямо в os.environ.get("...", "СЮДА_ЗНАЧЕНИЕ") вторым аргументом.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8322782866:AAGIVaPDeU_dU601ryIm2qJltWXBBVcIV5M")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://blackpan002.github.io/-/")
# ==================================

FREE_REQUESTS_PER_DAY = 3
SUBSCRIPTION_DAYS = 30
SUBSCRIPTION_PRICE_STARS = 199
USERS_FILE = "users.json"

SYSTEM_PROMPT = (
    "Ты — опытный личный стилист. Ты даёшь конкретные, практичные советы "
    "по одежде и образу: что надеть, как сочетать вещи, что докупить. "
    "Если не хватает важной информации (повод, погода, стиль который любит "
    "человек) — сначала коротко уточни один вопрос, потом переходи к совету. "
    "Отвечай тепло, но по делу, без воды, 3-6 предложений."
)

groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")

storage_lock = threading.Lock()


# ---------- Хранилище пользователей ----------

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def get_user(users, user_id):
    uid = str(user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if uid not in users:
        users[uid] = {"subscribed_until": None, "free_used": 0, "last_reset": today}
    if users[uid]["last_reset"] != today:
        users[uid]["free_used"] = 0
        users[uid]["last_reset"] = today
    return users[uid]


def is_subscribed(user_data):
    if not user_data["subscribed_until"]:
        return False
    return datetime.utcnow() < datetime.fromisoformat(user_data["subscribed_until"])


# ---------- Проверка подлинности запроса от Telegram Mini App ----------

def validate_init_data(init_data: str, bot_token: str):
    """Проверяет, что запрос действительно пришёл из Telegram, и достаёт user_id."""
    try:
        parsed = dict(parse_qsl(init_data))
        received_hash = parsed.pop("hash", None)
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if computed_hash != received_hash:
            return None
        user_json = parsed.get("user")
        return json.loads(user_json) if user_json else None
    except Exception:
        return None


# ---------- Flask API для Mini App ----------

flask_app = Flask(__name__)
CORS(flask_app)


@flask_app.route("/api/status", methods=["POST"])
def api_status():
    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""), TELEGRAM_TOKEN)
    if not user:
        return jsonify({"error": "invalid_init_data"}), 403

    with storage_lock:
        users = load_users()
        user_data = get_user(users, user["id"])
        save_users(users)

    if is_subscribed(user_data):
        until = datetime.fromisoformat(user_data["subscribed_until"])
        return jsonify({"subscribed": True, "subscribed_until": until.strftime("%d.%m.%Y")})
    else:
        return jsonify({
            "subscribed": False,
            "free_left": max(FREE_REQUESTS_PER_DAY - user_data["free_used"], 0),
            "free_total": FREE_REQUESTS_PER_DAY
        })


@flask_app.route("/api/advice", methods=["POST"])
def api_advice():
    body = request.get_json(force=True)
    user = validate_init_data(body.get("initData", ""), TELEGRAM_TOKEN)
    if not user:
        return jsonify({"error": "invalid_init_data"}), 403

    with storage_lock:
        users = load_users()
        user_data = get_user(users, user["id"])

        if not is_subscribed(user_data) and user_data["free_used"] >= FREE_REQUESTS_PER_DAY:
            save_users(users)
            return jsonify({"error": "limit_reached"})

        if not is_subscribed(user_data):
            user_data["free_used"] += 1
        save_users(users)

    user_message = body.get("message", "")
    image_data_url = body.get("image")

    try:
        if image_data_url:
            content = [
                {"type": "text", "text": user_message or "Что скажешь про эту вещь/образ?"},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]
            model = "qwen/qwen3.6-27b"
        else:
            content = user_message
            model = "openai/gpt-oss-120b"

        response = groq_client.chat.completions.create(
            model=model,
            max_tokens=400,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        )
        reply_text = response.choices[0].message.content
    except Exception as e:
        print(f"Ошибка при обращении к Groq API: {e}")
        reply_text = "Извините, произошла ошибка. Попробуйте ещё раз чуть позже."

    return jsonify({"reply": reply_text})


# ---------- Telegram-бот (запуск Mini App + оплата подписки) ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👗 Открыть стилиста", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await update.message.reply_text(
        "Привет! Я твой личный AI-стилист.\nНажми кнопку ниже, чтобы открыть приложение.",
        reply_markup=keyboard
    )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prices = [LabeledPrice("Подписка на 30 дней", SUBSCRIPTION_PRICE_STARS)]
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="AI-стилист — безлимитная подписка",
        description=f"Безлимитные консультации по стилю на {SUBSCRIPTION_DAYS} дней",
        payload="stylist_subscription",
        provider_token="",
        currency="XTR",
        prices=prices,
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with storage_lock:
        users = load_users()
        user_data = get_user(users, update.effective_user.id)
        until = datetime.utcnow() + timedelta(days=SUBSCRIPTION_DAYS)
        user_data["subscribed_until"] = until.isoformat()
        save_users(users)
    await update.message.reply_text(
        f"Спасибо! Подписка активна до {until.strftime('%d.%m.%Y')}. "
        "Открой приложение ещё раз — лимит уже снят 🎉"
    )


def run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)


def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("subscribe", subscribe))
    bot_app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    bot_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    print("Сервер и бот запущены. Нажмите Ctrl+C для остановки.")
    bot_app.run_polling()


if __name__ == "__main__":
    main()
