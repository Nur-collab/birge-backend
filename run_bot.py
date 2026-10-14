"""
Birge Telegram Bot — Автономный запуск в режиме Long Polling.
Позволяет боту работать на локальном компьютере без внешнего IP, без Render и без Firebase Blaze.
"""

import asyncio
import os
import sys

# Обеспечиваем корректный вывод UTF-8 в консоли Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import httpx
from database import SessionLocal, engine, Base
import models
from main import telegram_webhook

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8730455344:AAHQ3rj5r9kaGVTdDZH0T0CtMpwHyegNqMo")
API_URL = f"https://api.telegram.org/bot{TOKEN}"

# Создаем таблицы если их нет
Base.metadata.create_all(bind=engine)


async def main():
    print("=" * 60)
    print("Запуск Telegram-бота Birge (@BirgeAuthBot) в режиме Long Polling...")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=40.0) as client:
        # 1. Проверяем токен
        me_resp = await client.get(f"{API_URL}/getMe")
        if me_resp.status_code != 200 or not me_resp.json().get("ok"):
            print(f"[ОШИБКА] Неверный токен бота: {me_resp.text}")
            return
        bot_info = me_resp.json().get("result", {})
        print(f"[*] Бот авторизован: @{bot_info.get('username')} ({bot_info.get('first_name')})")

        # 2. Удаляем старый вебхук для включения long-polling
        del_resp = await client.post(f"{API_URL}/deleteWebhook", json={"drop_pending_updates": False})
        print(f"[*] Вебхук отключен, режим Long Polling активен: {del_resp.json().get('description', 'OK')}")
        print("[+] БОТ ГОТОВ ПРИНИМАТЬ СООБЩЕНИЯ!")
        print("    Откройте Telegram: https://t.me/BirgeAuthBot и нажмите /start\n")

        offset = 0
        while True:
            try:
                # Ожидаем новые сообщения от Telegram (long-polling timeout 25s)
                resp = await client.get(
                    f"{API_URL}/getUpdates",
                    params={"offset": offset, "timeout": 25},
                    timeout=35.0,
                )
                if resp.status_code != 200:
                    await asyncio.sleep(2)
                    continue

                data = resp.json()
                if not data.get("ok"):
                    await asyncio.sleep(2)
                    continue

                updates = data.get("result", [])
                for update in updates:
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    sender = msg.get("from", {}).get("first_name", "Пользователь")
                    chat_id = msg.get("chat", {}).get("id")
                    text = msg.get("text", "")
                    has_contact = "contact" in msg

                    info = f"текст: '{text}'" if text else ("контакт: " + str(msg.get("contact", {}).get("phone_number"))) if has_contact else "событие"
                    print(f"[Сообщение] [{chat_id}] {sender}: {info}")

                    # Обрабатываем сообщение через логику telegram_webhook
                    db = SessionLocal()
                    try:
                        await telegram_webhook(update, db)
                    except Exception as e:
                        print(f"[!] Ошибка обработки: {e}")
                    finally:
                        db.close()

            except (httpx.ReadTimeout, httpx.ConnectTimeout):
                continue
            except asyncio.CancelledError:
                print("\n[!] Остановка бота...")
                break
            except Exception as exc:
                print(f"[!] Ошибка сети/запроса: {exc}")
                await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] Бот остановлен пользователем.")
