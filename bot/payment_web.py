from __future__ import annotations

import logging
from html import escape

from aiohttp import web
from aiohttp.web_request import FileField
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from .database import Database


def page(title: str, body: str, status: int = 200) -> web.Response:
    html = f"""<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing:border-box }}
body {{ margin:0; min-height:100vh; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
background:radial-gradient(circle at top,#47320d,#0c0a07 55%); color:#fff; padding:24px }}
.card {{ max-width:520px; margin:20px auto; background:#17130d; border:1px solid #9b6a19;
border-radius:24px; padding:24px; box-shadow:0 20px 60px #0008 }}
h1 {{ color:#f4c65b; margin:0 0 10px }} .muted {{ color:#c9c1b2; line-height:1.5 }}
.details {{ background:#211a0f; padding:18px; border-radius:16px; margin:20px 0 }}
.number {{ font-size:23px; letter-spacing:1px; font-weight:750; color:#ffd978; word-break:break-word }}
label {{ display:block; margin:16px 0 7px; font-weight:650 }}
input {{ width:100%; padding:14px; border-radius:12px; border:1px solid #57462b;
background:#0e0c09; color:white; font-size:16px }}
button {{ width:100%; margin-top:20px; padding:15px; border:0; border-radius:14px;
background:linear-gradient(135deg,#f7d575,#b97815); color:#1b1204; font-size:17px; font-weight:800 }}
.notice {{ padding:13px; border-radius:12px; background:#2a2112; color:#ead6a8; line-height:1.45 }}
</style>
</head><body><main class="card">{body}</main></body></html>"""
    return web.Response(text=html, content_type="text/html", status=status)


async def payment_page(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    settings = await db.manual_payment_settings()
    if not settings["enabled"]:
        return page("To‘lov vaqtincha yopiq", "<h1>To‘lov vaqtincha yopiq</h1><p class='muted'>Admin karta rekvizitlarini sozlagach sahifa ishga tushadi.</p>", 503)
    card_digits = "".join(ch for ch in settings["card_number"] if ch.isdigit())
    grouped = " ".join(card_digits[i:i + 4] for i in range(0, len(card_digits), 4))
    body = f"""
<h1>💎 VIP uchun to‘lov</h1>
<p class="muted"><b>{settings['vip_days']} kunlik VIP</b> — {settings['price_uzs']:,} so‘m</p>
<div class="details">
<div class="muted">Karta raqami</div>
<div class="number">{escape(grouped)}</div>
<p><b>{escape(settings['card_holder'])}</b></p>
</div>
<div class="notice">1. Ko‘rsatilgan summani kartaga o‘tkazing.<br>
2. Telegram ID raqamingizni kiriting.<br>
3. Bank chekini rasm sifatida yuklang.</div>
<form action="/receipt" method="post" enctype="multipart/form-data">
<label>Telegram ID</label>
<input name="user_id" inputmode="numeric" pattern="[0-9]{{5,20}}" required placeholder="Masalan: 6898342052">
<label>To‘lov cheki</label>
<input name="receipt" type="file" accept="image/jpeg,image/png,image/webp" required>
<button type="submit">📤 Chekni adminga yuborish</button>
</form>
<p class="muted">Tasdiqlashdan oldin admin to‘lovni bank ilovasida tekshiradi.</p>
"""
    return page("AIKINO_UZ VIP", body)


async def receipt_upload(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    bot: Bot = request.app["bot"]
    admin_id: int = request.app["admin_id"]
    settings = await db.manual_payment_settings()
    if not settings["enabled"]:
        return page("To‘lov yopiq", "<h1>To‘lov vaqtincha yopiq</h1>", 503)
    try:
        form = await request.post()
        user_id = int(str(form.get("user_id", "")).strip())
        receipt = form.get("receipt")
    except (ValueError, TypeError, web.HTTPException):
        return page("Xatolik", "<h1>Ma’lumot noto‘g‘ri</h1><p>Telegram ID va chekni qayta tekshiring.</p>", 400)
    if user_id < 1 or not isinstance(receipt, FileField):
        return page("Xatolik", "<h1>Ma’lumot yetarli emas</h1><p>Telegram ID va chek rasmi majburiy.</p>", 400)
    data = receipt.file.read(8 * 1024 * 1024 + 1)
    if not data or len(data) > 8 * 1024 * 1024:
        return page("Xatolik", "<h1>Chek hajmi noto‘g‘ri</h1><p>8 MB dan kichik JPG, PNG yoki WEBP rasm yuboring.</p>", 400)
    content_type = (receipt.content_type or "").lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        return page("Xatolik", "<h1>Faqat rasm qabul qilinadi</h1>", 400)

    state, payment = await db.create_manual_payment_request(user_id, settings["price_uzs"])
    if state == "duplicate":
        return page("Chek kutilmoqda", "<h1>⏳ Chekingiz tekshirilmoqda</h1><p class='muted'>Oldingi so‘rovingiz hali admin tomonidan ko‘rib chiqilmagan.</p>", 409)

    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"adm:payapprove:{payment['id']}"),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"adm:payreject:{payment['id']}"),
    ]])
    caption = (
        "💳 <b>Yangi karta to‘lovi</b>\n\n"
        f"👤 Telegram ID: <code>{user_id}</code>\n"
        f"💰 Summa: <b>{settings['price_uzs']:,} so‘m</b>\n"
        f"⏳ VIP: <b>{settings['vip_days']} kun</b>\n\n"
        "Bank ilovasida pul tushganini tekshirib, qaror bering."
    )
    filename = receipt.filename or "receipt.jpg"
    try:
        sent = await bot.send_photo(
            admin_id,
            BufferedInputFile(data, filename=filename),
            caption=caption,
            reply_markup=markup,
        )
        await db.set_manual_payment_receipt(payment["id"], sent.photo[-1].file_id)
    except TelegramAPIError:
        logging.getLogger(__name__).exception("Manual payment receipt could not be sent")
        await db.review_manual_payment(payment["id"], "rejected")
        return page("Xatolik", "<h1>Chek yuborilmadi</h1><p class='muted'>Birozdan keyin qayta urinib ko‘ring.</p>", 502)

    return page("Chek yuborildi", "<h1>✅ Chek yuborildi</h1><p class='muted'>Admin to‘lovni tekshiradi. Natija bot orqali sizga yuboriladi.</p>")


async def start_payment_web(bot: Bot, db: Database, admin_id: int):
    app = web.Application(client_max_size=8 * 1024 * 1024)
    app["bot"] = bot
    app["db"] = db
    app["admin_id"] = admin_id
    app.router.add_get("/", payment_page)
    app.router.add_post("/receipt", receipt_upload)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(__import__("os").environ.get("PORT", "8080")))
    await site.start()
    return runner
