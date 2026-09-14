from __future__ import annotations

import asyncio
import math
import re
import logging
import time
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .database import Database

router = Router()
PAGE_MOVIES = 8
PAGE_EPISODES = 10
PAGE_REQUESTS = 10
PAGE_ADMIN_ACTIONS = 10
LONDON_TZ = ZoneInfo("Europe/London")
ERROR_ALERT_COOLDOWN = 300
_last_error_alerts: dict[str, float] = {}


def subscription_kb(channel_id: str, target: str = "home"):
    username = channel_id.lstrip("@")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Kanalga obuna bo‘lish", url=f"https://t.me/{username}")],
        [InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data=f"subcheck:{target}")],
    ])


async def is_channel_member(bot: Bot, channel_id: str | None, user_id: int) -> bool:
    if not channel_id:
        return True
    try:
        member = await bot.get_chat_member(channel_id, user_id)
    except TelegramAPIError:
        logging.getLogger(__name__).exception("Kanal obunasini tekshirib bo‘lmadi")
        return True
    if member.status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
    }:
        return True
    return member.status == ChatMemberStatus.RESTRICTED and bool(getattr(member, "is_member", False))


def subscription_target(event: Message | CallbackQuery) -> str:
    if isinstance(event, Message):
        parts = (event.text or "").split(maxsplit=1)
        if len(parts) == 2 and parts[0] == "/start" and re.fullmatch(r"ep_\d+", parts[1]):
            return parts[1]
    return "home"


class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        user = event.from_user
        admin_id = data.get("admin_id")
        channel_id = data.get("channel_id")
        db: Database | None = data.get("db")
        if user and user.id != admin_id and db:
            await db.track_user(user.id)
        if not user or not channel_id or user.id == admin_id:
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and (event.data or "").startswith("subcheck:"):
            return await handler(event, data)
        bot: Bot = data["bot"]
        if await is_channel_member(bot, channel_id, user.id):
            return await handler(event, data)
        text = (
            "🔒 <b>Botdan foydalanish uchun kanalga obuna bo‘ling.</b>\n\n"
            "1. <b>📢 Kanalga obuna bo‘lish</b> tugmasini bosing.\n"
            "2. Kanalga qo‘shiling.\n"
            "3. Botga qaytib <b>✅ Obunani tekshirish</b>ni bosing."
        )
        markup = subscription_kb(channel_id, subscription_target(event))
        if isinstance(event, CallbackQuery):
            await event.answer("Avval kanalga obuna bo‘ling.", show_alert=True)
            await event.message.answer(text, reply_markup=markup)
            return None
        await event.answer(text, reply_markup=markup)
        return None


subscription_middleware = SubscriptionMiddleware()
router.message.outer_middleware(subscription_middleware)
router.callback_query.outer_middleware(subscription_middleware)


class AdminFlow(StatesGroup):
    movie_title = State()
    movie_poster = State()
    movie_description = State()
    episode_number = State()
    episode_video = State()
    bulk_start_number = State()
    bulk_videos = State()
    rename_movie = State()
    update_movie_poster = State()
    update_movie_description = State()
    renumber_episode = State()
    replace_video = State()
    broadcast_content = State()
    broadcast_ready = State()
    vip_user_id = State()
    vip_days = State()
    vip_revoke_id = State()
    manual_card_number = State()
    manual_card_holder = State()
    manual_price_uzs = State()
    manual_vip_days = State()
    stars_plans = State()


class PaymentSupportFlow(StatesGroup):
    message = State()


class SearchFlow(StatesGroup):
    query = State()


class MovieRequestFlow(StatesGroup):
    title = State()


def main_menu(is_admin=False):
    rows = [
        [InlineKeyboardButton(text="🎬 Kinolar", callback_data="movies:0")],
        [InlineKeyboardButton(text="▶️ Tomosha qilishni davom ettirish", callback_data="continue")],
        [InlineKeyboardButton(text="❤️ Sevimlilar", callback_data="favorites")],
        [InlineKeyboardButton(text="💎 VIP bo‘lim", callback_data="vip:0")],
        [InlineKeyboardButton(text="🔥 Yangi qismlar", callback_data="latest")],
        [InlineKeyboardButton(text="🔎 Kino qidirish", callback_data="search")],
        [InlineKeyboardButton(text="🎬 Kino so‘rash", callback_data="requestmovie")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🔐 Admin panel", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")]])


def skip_movie_poster_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Postersiz davom etish", callback_data="adm:skipposter")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")],
    ])


def skip_movie_description_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Tavsifsiz yakunlash", callback_data="adm:skipdescription")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")],
    ])


def bulk_upload_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yuklashni tugatish", callback_data="adm:bulkfinish")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")],
    ])


def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Yangi kino qo‘shish", callback_data="adm:addmovie")],
        [InlineKeyboardButton(text="➕ Yangi qism qo‘shish", callback_data="adm:addepisode")],
        [InlineKeyboardButton(text="📚 Qismlarni ketma-ket yuklash", callback_data="adm:bulk")],
        [InlineKeyboardButton(text="📹 Video qo‘shish/almashtirish", callback_data="adm:video")],
        [InlineKeyboardButton(text="✏️ Kino/qismni tahrirlash", callback_data="adm:edit")],
        [InlineKeyboardButton(text="🗑 Kino/qismni o‘chirish", callback_data="adm:delete")],
        [InlineKeyboardButton(text="📋 Kinolar ro‘yxati", callback_data="adm:list")],
        [InlineKeyboardButton(text="📊 Statistika", callback_data="adm:stats")],
        [InlineKeyboardButton(text="📣 Hammaga xabar yuborish", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="📜 Tarqatmalar tarixi", callback_data="adm:broadcasts:0")],
        [InlineKeyboardButton(text="🛡 Admin amallari tarixi", callback_data="adm:actions:0")],
        [InlineKeyboardButton(text="📩 Kino so‘rovlari", callback_data="adm:requests:0")],
        [InlineKeyboardButton(text="💎 VIP boshqaruvi", callback_data="adm:vip")],
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")],
    ])


def vip_locked_markup():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Stars bilan VIP olish", callback_data="vipbuy")],
        [InlineKeyboardButton(text="🎬 Kino so‘rash", callback_data="requestmovie")],
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")],
    ])


def vip_admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ VIP berish", callback_data="adm:vipadd")],
        [InlineKeyboardButton(text="➖ VIPni bekor qilish", callback_data="adm:viprevoke")],
        [InlineKeyboardButton(text="🎬 VIP kinolarni belgilash", callback_data="adm:vipmovies")],
        [InlineKeyboardButton(text="👥 Faol VIP ro‘yxati", callback_data="adm:viplist")],
        [InlineKeyboardButton(text="⭐ Stars to‘lov sozlamalari", callback_data="adm:starspay")],
        [InlineKeyboardButton(text="💳 Karta to‘lov sozlamalari", callback_data="adm:manualpay")],
        [InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin")],
    ])


def broadcast_link_markup(bot_username: str, enabled: bool):
    if not enabled:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎬 Botni ochish", url=f"https://t.me/{bot_username}")
    ]])


def broadcast_confirm_markup(link_enabled: bool):
    link_status = "✅ Bot tugmasi yoqilgan" if link_enabled else "➕ Bot tugmasini qo‘shish"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=link_status, callback_data="adm:broadcast:toggle")],
        [InlineKeyboardButton(text="✅ Hammaga yuborish", callback_data="adm:broadcast:send")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")],
    ])


async def safe_edit(call: CallbackQuery, text: str, markup=None):
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await call.message.answer(text, reply_markup=markup)
    await call.answer()


def is_admin(user_id: int, admin_id: int):
    return user_id == admin_id


async def record_admin_action(
    db: Database,
    user,
    action: str,
    details: str | None = None,
):
    try:
        name = f"@{user.username}" if user.username else user.full_name
        await db.add_admin_action(user.id, name, action, details)
    except Exception:
        logging.getLogger(__name__).exception("Admin amalini tarixga yozib bo‘lmadi")


def format_number(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


async def episode_markup(db: Database, ep, user_id: int):
    prev_ep = await db.adjacent_episode(ep["movie_id"], ep["episode_number"], "prev")
    next_ep = await db.adjacent_episode(ep["movie_id"], ep["episode_number"], "next")
    rows = []
    nav = []
    if prev_ep:
        nav.append(InlineKeyboardButton(text="⬅️ Oldingi qism", callback_data=f"ep:{prev_ep['id']}"))
    if next_ep:
        nav.append(InlineKeyboardButton(text="Keyingi qism ➡️", callback_data=f"ep:{next_ep['id']}"))
    if nav:
        rows.append(nav)
    is_favorite = await db.is_episode_favorite(user_id, ep["id"])
    favorite_text = "💔 Sevimlilardan olib tashlash" if is_favorite else "❤️ Qismni sevimlilarga qo‘shish"
    rows.append([InlineKeyboardButton(text=favorite_text, callback_data=f"fave:{ep['id']}")])
    rows.append([InlineKeyboardButton(text="🎬 Barcha qismlar", callback_data=f"movie:{ep['movie_id']}")])
    rows.append([InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_episode_message(
    message: Message,
    db: Database,
    episode_id: int,
    viewer_user_id: int | None = None,
    admin_id: int | None = None,
):
    ep = await db.episode(episode_id)
    if not ep or not ep["file_id"]:
        return False
    if (
        ep["movie_is_vip"]
        and viewer_user_id != admin_id
        and (viewer_user_id is None or not await db.is_vip_user(viewer_user_id))
    ):
        await message.answer(
            "💎 <b>Bu kino faqat VIP foydalanuvchilar uchun.</b>\n\n"
            "VIP huquqini olish uchun admin bilan bog‘laning.",
            reply_markup=vip_locked_markup(),
        )
        return True
    caption = ep["caption"] or f"{ep['movie_emoji']} <b>{ep['movie_title']}</b> — {ep['episode_number']}-QISM"
    await message.answer_video(
        ep["file_id"],
        caption=caption,
        reply_markup=await episode_markup(db, ep, viewer_user_id or message.chat.id),
        supports_streaming=True,
        protect_content=True,
    )
    if viewer_user_id is not None:
        await db.save_watch_progress(viewer_user_id, ep["id"])
        await db.record_episode_view(viewer_user_id, ep["id"])
    return True



PAYMENT_TERMS_TEXT = (
    "📄 <b>VIP to‘lov shartlari</b>\n\n"
    "• VIP faqat tanlangan muddat davomida ishlaydi.\n"
    "• 10 va 20 kunlik paketlar bir martalik.\n"
    "• 30 kunlik paket avtomatik yangilanadigan obuna. Uni istalgan payt bekor qilish mumkin; "
    "bekor qilinganda joriy muddat oxirigacha ishlaydi.\n"
    "• To‘lov amalga oshgach VIP avtomatik yoqiladi.\n"
    "• To‘lov muammosi uchun /paysupport buyrug‘idan foydalaning.\n"
    "• Xarid bo‘yicha yordamni bot egasi beradi; Telegram yordam xizmati javobgar emas."
)


def stars_plan_markup(plans, active_subscription=False):
    rows = []
    for days, stars in plans:
        suffix = " · avtomatik" if days == 30 else ""
        rows.append([InlineKeyboardButton(
            text=f"⭐ {days} kun — {stars} Stars{suffix}",
            callback_data=f"vipstar:{days}:{stars}",
        )])
    if active_subscription:
        rows.append([InlineKeyboardButton(text="❌ Avtomatik obunani bekor qilish", callback_data="vipcancelstars")])
    rows.extend([
        [InlineKeyboardButton(text="📄 To‘lov shartlari", callback_data="vipterms")],
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_stars_payment(call: CallbackQuery, db: Database):
    if not await db.stars_payments_enabled():
        return await safe_edit(call, "⭐ Stars orqali to‘lov vaqtincha o‘chirilgan.", vip_locked_markup())
    plans = await db.stars_plans()
    vip = await db.vip_user(call.from_user.id)
    subscription = await db.active_subscription_payment(call.from_user.id)
    status = ""
    if vip:
        expires = vip["expires_at"].astimezone(LONDON_TZ).strftime("%d.%m.%Y %H:%M")
        status = f"\n\n💎 Hozirgi VIP muddati: <b>{expires}</b> gacha."
    await safe_edit(
        call,
        "⭐ <b>Telegram Stars orqali VIP</b>\n\n"
        "Paketni tanlang. 10/20 kunlik paketlar bir martalik, 30 kunlik paket esa har 30 kunda avtomatik yangilanadi."
        + status,
        stars_plan_markup(plans, bool(subscription)),
    )


def parse_vip_payload(payload: str):
    parts = payload.split(":")
    if len(parts) != 5 or parts[0] != "vipstars":
        return None
    try:
        user_id, days, stars = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return None
    kind = parts[4]
    if kind not in {"once", "sub"}:
        return None
    return user_id, days, stars, kind


@router.message(Command("terms"))
async def payment_terms_command(message: Message):
    await message.answer(PAYMENT_TERMS_TEXT)


@router.callback_query(F.data == "vipterms")
async def payment_terms_callback(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Shartlarga roziman", callback_data="viptermsagree")],
        [InlineKeyboardButton(text="⬅️ VIP to‘lov", callback_data="vipbuy")],
    ])
    await safe_edit(call, PAYMENT_TERMS_TEXT, kb)


@router.callback_query(F.data == "viptermsagree")
async def payment_terms_agree(call: CallbackQuery, db: Database):
    await db.accept_payment_terms(call.from_user.id)
    await call.answer("Shartlar qabul qilindi.")
    await render_stars_payment(call, db)


@router.callback_query(F.data == "vipbuy")
async def vip_buy(call: CallbackQuery, db: Database):
    if not await db.has_accepted_payment_terms(call.from_user.id):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ O‘qidim va roziman", callback_data="viptermsagree")],
            [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")],
        ])
        return await safe_edit(call, PAYMENT_TERMS_TEXT, kb)
    await render_stars_payment(call, db)


@router.callback_query(F.data.startswith("vipstar:"))
async def create_stars_invoice(call: CallbackQuery, db: Database, bot: Bot):
    if not await db.has_accepted_payment_terms(call.from_user.id):
        return await call.answer("Avval to‘lov shartlarini qabul qiling.", show_alert=True)
    if not await db.stars_payments_enabled():
        return await call.answer("Stars to‘lovi vaqtincha o‘chirilgan.", show_alert=True)
    try:
        _, days_text, stars_text = call.data.split(":")
        days, stars = int(days_text), int(stars_text)
    except (TypeError, ValueError):
        return await call.answer("Paket noto‘g‘ri.", show_alert=True)
    plans = await db.stars_plans()
    if (days, stars) not in plans:
        return await call.answer("Paket narxi o‘zgargan. Sahifani qayta oching.", show_alert=True)
    kind = "sub" if days == 30 else "once"
    payload = f"vipstars:{call.from_user.id}:{days}:{stars}:{kind}"
    kwargs = dict(
        title=f"AIKINO_UZ VIP — {days} kun",
        description=(
            "Har 30 kunda avtomatik yangilanadigan VIP obuna."
            if kind == "sub" else f"{days} kunlik bir martalik VIP huquqi."
        ),
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"VIP {days} kun", amount=stars)],
    )
    if kind == "sub":
        kwargs["subscription_period"] = 2592000
    invoice_url = await bot.create_invoice_link(**kwargs)
    text = (
        f"⭐ <b>{days} kunlik VIP</b>\n\n"
        f"Narxi: <b>{stars} Stars</b>\n"
        + ("🔄 Har 30 kunda avtomatik yangilanadi.\n" if kind == "sub" else "Bir martalik to‘lov.\n")
        + "\nTo‘lash uchun tugmani bosing:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ {stars} Stars to‘lash", url=invoice_url)],
        [InlineKeyboardButton(text="⬅️ Paketlar", callback_data="vipbuy")],
    ])
    await safe_edit(call, text, kb)


@router.pre_checkout_query()
async def stars_pre_checkout(query: PreCheckoutQuery, db: Database):
    parsed = parse_vip_payload(query.invoice_payload)
    if not parsed:
        return await query.answer(ok=False, error_message="To‘lov ma’lumoti noto‘g‘ri.")
    user_id, days, stars, kind = parsed
    plans = await db.stars_plans()
    valid = (
        query.currency == "XTR"
        and query.from_user.id == user_id
        and query.total_amount == stars
        and (days, stars) in plans
        and kind == ("sub" if days == 30 else "once")
        and await db.has_accepted_payment_terms(user_id)
        and await db.stars_payments_enabled()
    )
    await query.answer(ok=valid, error_message=None if valid else "Paket narxi yoki holati o‘zgargan. Qayta urinib ko‘ring.")


@router.message(F.successful_payment)
async def stars_payment_success(message: Message, db: Database):
    payment = message.successful_payment
    parsed = parse_vip_payload(payment.invoice_payload)
    if not parsed or payment.currency != "XTR":
        return
    user_id, days, stars, kind = parsed
    if user_id != message.from_user.id or payment.total_amount != stars:
        return
    expiration_value = getattr(payment, "subscription_expiration_date", None)
    if kind == "sub" and expiration_value:
        expires_at = datetime.fromtimestamp(expiration_value, tz=timezone.utc)
    else:
        current_vip = await db.vip_user(user_id)
        base = max(datetime.now(timezone.utc), current_vip["expires_at"]) if current_vip else datetime.now(timezone.utc)
        expires_at = base + timedelta(days=days)
    inserted = await db.record_vip_payment(
        user_id=user_id,
        telegram_charge_id=payment.telegram_payment_charge_id,
        provider_charge_id=payment.provider_payment_charge_id or None,
        invoice_payload=payment.invoice_payload,
        currency=payment.currency,
        total_amount=payment.total_amount,
        expires_at=expires_at,
        is_recurring=bool(getattr(payment, "is_recurring", False)),
        is_first_recurring=bool(getattr(payment, "is_first_recurring", False)),
    )
    if not inserted:
        return
    expires = expires_at.astimezone(LONDON_TZ).strftime("%d.%m.%Y %H:%M")
    await message.answer(
        "✅ <b>Stars to‘lovi qabul qilindi!</b>\n\n"
        f"💎 VIP huquqi <b>{expires}</b> gacha faollashtirildi.",
        reply_markup=main_menu(),
    )


@router.callback_query(F.data == "vipcancelstars")
async def cancel_stars_subscription(call: CallbackQuery, db: Database, bot: Bot):
    payment = await db.active_subscription_payment(call.from_user.id)
    if not payment:
        return await call.answer("Faol avtomatik Stars obunasi topilmadi.", show_alert=True)
    await bot.edit_user_star_subscription(
        user_id=call.from_user.id,
        telegram_payment_charge_id=payment["telegram_payment_charge_id"],
        is_canceled=True,
    )
    await db.cancel_subscription_renewal(payment["telegram_payment_charge_id"])
    await safe_edit(
        call,
        "✅ Avtomatik yangilanish bekor qilindi. VIP joriy muddat tugaguncha ishlaydi.",
        vip_locked_markup(),
    )


@router.message(Command("paysupport"))
async def payment_support_start(message: Message, state: FSMContext):
    await state.set_state(PaymentSupportFlow.message)
    await message.answer(
        "🆘 To‘lov bo‘yicha muammoingizni bitta xabarda yozing. Admin sizga yordam beradi.",
        reply_markup=cancel_kb(),
    )


@router.message(PaymentSupportFlow.message)
async def payment_support_send(message: Message, state: FSMContext, bot: Bot, admin_id: int):
    await state.clear()
    text = message.text or message.caption or "Media/fayl yuborildi"
    await bot.send_message(
        admin_id,
        "🆘 <b>To‘lov bo‘yicha yordam so‘rovi</b>\n\n"
        f"👤 {escape(message.from_user.full_name)}\n"
        f"🆔 <code>{message.from_user.id}</code>\n\n"
        f"{escape(text[:2000])}",
    )
    await message.answer("✅ Xabaringiz adminga yuborildi.", reply_markup=main_menu())


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, admin_id: int, db: Database):
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("ep_"):
        try:
            if await send_episode_message(message, db, int(parts[1][3:]), message.from_user.id, admin_id):
                return
        except ValueError:
            pass
    await message.answer(
        "🎬 <b>AIKINO_UZ botiga xush kelibsiz!</b>\n\nSevimli kino va seriallaringizni tanlang:",
        reply_markup=main_menu(is_admin(message.from_user.id, admin_id)),
    )


@router.callback_query(F.data == "home")
async def home(call: CallbackQuery, state: FSMContext, admin_id: int):
    await state.clear()
    await safe_edit(call, "🎬 <b>Bosh menyu</b>\n\nKerakli bo‘limni tanlang:", main_menu(is_admin(call.from_user.id, admin_id)))


@router.callback_query(F.data == "cancel")
async def cancel(call: CallbackQuery, state: FSMContext, admin_id: int):
    await state.clear()
    await safe_edit(call, "Amal bekor qilindi.", main_menu(is_admin(call.from_user.id, admin_id)))


@router.callback_query(F.data.startswith("subcheck:"))
async def check_subscription(
    call: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    db: Database,
    admin_id: int,
    channel_id: str | None,
):
    if not channel_id or not await is_channel_member(bot, channel_id, call.from_user.id):
        if channel_id:
            await call.answer("❌ Hali kanalga obuna bo‘lmagansiz.", show_alert=True)
        else:
            await call.answer("Kanal sozlamasi topilmadi.", show_alert=True)
        return
    await state.clear()
    target = (call.data or "subcheck:home").split(":", 1)[1]
    if re.fullmatch(r"ep_\d+", target):
        await safe_edit(call, "✅ <b>Obuna tasdiqlandi.</b>\n\nVideo ochilmoqda…")
        if not await send_episode_message(call.message, db, int(target[3:]), call.from_user.id, admin_id):
            await call.message.answer("Video topilmadi.", reply_markup=main_menu())
        return
    await safe_edit(
        call,
        "✅ <b>Obuna tasdiqlandi!</b>\n\nKerakli bo‘limni tanlang:",
        main_menu(is_admin(call.from_user.id, admin_id)),
    )


async def movie_keyboard(db: Database, page: int, prefix="movie", back="home"):
    count = await db.public_movie_count()
    page = max(0, min(page, max(0, math.ceil(count / PAGE_MOVIES) - 1)))
    items = await db.public_movies(page * PAGE_MOVIES, PAGE_MOVIES)
    b = InlineKeyboardBuilder()
    for m in items:
        b.button(text=f"{m['emoji']} {m['title']}", callback_data=f"{prefix}:{m['id']}")
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"movies:{page-1}"))
    if (page + 1) * PAGE_MOVIES < count:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"movies:{page+1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="🏠 Bosh menyu", callback_data=back))
    return b.as_markup(), count, page


@router.callback_query(F.data.startswith("movies:"))
async def show_movies(call: CallbackQuery, db: Database):
    page = int(call.data.split(":")[1])
    kb, count, page = await movie_keyboard(db, page)
    text = "🎬 <b>Kinolar</b>\n\nKinoni tanlang:" if count else "Hozircha kinolar qo‘shilmagan."
    await safe_edit(call, text, kb)


async def vip_movie_keyboard(db: Database, page: int):
    count = await db.vip_movie_count()
    page = max(0, min(page, max(0, math.ceil(count / PAGE_MOVIES) - 1)))
    items = await db.vip_movies(page * PAGE_MOVIES, PAGE_MOVIES)
    b = InlineKeyboardBuilder()
    for movie in items:
        b.button(text=f"💎 {movie['title']}", callback_data=f"movie:{movie['id']}")
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"vip:{page-1}"))
    if (page + 1) * PAGE_MOVIES < count:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"vip:{page+1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home"))
    return b.as_markup(), count, page


@router.callback_query(F.data.startswith("vip:"))
async def vip_movies(call: CallbackQuery, db: Database, admin_id: int):
    vip = await db.vip_user(call.from_user.id)
    if call.from_user.id != admin_id and not vip:
        return await safe_edit(
            call,
            "💎 <b>VIP bo‘lim</b>\n\n"
            "Sizda hozircha faol VIP huquqi yo‘q. VIP olish uchun admin bilan bog‘laning.",
            vip_locked_markup(),
        )
    page = int(call.data.split(":")[1])
    kb, count, page = await vip_movie_keyboard(db, page)
    if call.from_user.id == admin_id:
        expiry = "Admin uchun doim ochiq"
    else:
        expiry = f"VIP muddati: {vip['expires_at'].astimezone(LONDON_TZ).strftime('%d.%m.%Y %H:%M')} gacha"
    text = (
        f"💎 <b>VIP kinolar</b>\n\n{expiry}\n\nKinoni tanlang:"
        if count else
        f"💎 <b>VIP kinolar</b>\n\n{expiry}\n\nHozircha VIP kinolar qo‘shilmagan."
    )
    await safe_edit(call, text, kb)


async def episode_keyboard(db: Database, movie_id: int, page: int, user_id: int):
    count = await db.episode_count(movie_id)
    page = max(0, min(page, max(0, math.ceil(count / PAGE_EPISODES) - 1)))
    eps = await db.episodes(movie_id, page * PAGE_EPISODES, PAGE_EPISODES)
    b = InlineKeyboardBuilder()
    for e in eps:
        b.button(text=f"{e['episode_number']}-QISM", callback_data=f"ep:{e['id']}")
    b.adjust(2)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"moviepage:{movie_id}:{page-1}"))
    if (page + 1) * PAGE_EPISODES < count:
        nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"moviepage:{movie_id}:{page+1}"))
    if nav:
        b.row(*nav)
    is_favorite = await db.is_movie_favorite(user_id, movie_id)
    favorite_text = "💔 Kinoni sevimlilardan olib tashlash" if is_favorite else "❤️ Kinoni sevimlilarga qo‘shish"
    b.row(InlineKeyboardButton(text=favorite_text, callback_data=f"favm:{movie_id}:{page}"))
    b.row(InlineKeyboardButton(text="🎬 Kinolar", callback_data="movies:0"), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home"))
    return b.as_markup(), count


@router.callback_query(F.data.startswith("movie:"))
async def select_movie(call: CallbackQuery, db: Database, admin_id: int):
    movie_id = int(call.data.split(":")[1])
    await render_movie(call, db, movie_id, 0, admin_id)


@router.callback_query(F.data.startswith("moviepage:"))
async def movie_page(call: CallbackQuery, db: Database, admin_id: int):
    _, movie_id, page = call.data.split(":")
    await render_movie(call, db, int(movie_id), int(page), admin_id)


async def render_movie(call, db, movie_id, page, admin_id):
    movie = await db.movie(movie_id)
    if not movie:
        return await safe_edit(call, "Kino topilmadi.", main_menu())
    if movie["is_vip"] and call.from_user.id != admin_id and not await db.is_vip_user(call.from_user.id):
        return await safe_edit(
            call,
            "💎 <b>Bu kino faqat VIP foydalanuvchilar uchun.</b>\n\n"
            "VIP huquqini olish uchun admin bilan bog‘laning.",
            vip_locked_markup(),
        )
    kb, count = await episode_keyboard(db, movie_id, page, call.from_user.id)
    title = f"{escape(movie['emoji'])} <b>{escape(movie['title'])}</b>"
    description = escape(movie["description"]) if movie["description"] else ""
    status = "Qismni tanlang:" if count else "Hozircha qismlar yo‘q."
    text = f"{title}\n\n{description}\n\n{status}" if description else f"{title}\n\n{status}"
    if movie["poster_file_id"]:
        if call.message.photo:
            try:
                await call.message.edit_caption(caption=text, reply_markup=kb)
            except TelegramBadRequest:
                await call.message.answer_photo(movie["poster_file_id"], caption=text, reply_markup=kb)
        else:
            await call.message.answer_photo(movie["poster_file_id"], caption=text, reply_markup=kb)
        await call.answer()
        return
    await safe_edit(call, text, kb)


@router.callback_query(F.data.startswith("ep:"))
async def send_episode(call: CallbackQuery, db: Database, admin_id: int):
    ep = await db.episode(int(call.data.split(":")[1]))
    if not ep or not ep["file_id"]:
        return await call.answer("Video topilmadi.", show_alert=True)
    await send_episode_message(call.message, db, ep["id"], call.from_user.id, admin_id)
    await call.answer()


@router.callback_query(F.data.startswith("favm:"))
async def toggle_favorite_movie(call: CallbackQuery, db: Database, admin_id: int):
    _, movie_id, page = call.data.split(":")
    movie = await db.movie(int(movie_id))
    if not movie:
        return await call.answer("Kino topilmadi.", show_alert=True)
    if movie["is_vip"] and call.from_user.id != admin_id and not await db.is_vip_user(call.from_user.id):
        return await call.answer("Bu kino uchun faol VIP huquqi kerak.", show_alert=True)
    added = await db.toggle_movie_favorite(call.from_user.id, int(movie_id))
    kb, _ = await episode_keyboard(db, int(movie_id), int(page), call.from_user.id)
    await call.message.edit_reply_markup(reply_markup=kb)
    await call.answer("❤️ Kino sevimlilarga qo‘shildi." if added else "Kino sevimlilardan olib tashlandi.")


@router.callback_query(F.data.startswith("fave:"))
async def toggle_favorite_episode(call: CallbackQuery, db: Database, admin_id: int):
    episode_id = int(call.data.split(":")[1])
    ep = await db.episode(episode_id)
    if not ep or not ep["file_id"]:
        return await call.answer("Qism topilmadi.", show_alert=True)
    if ep["movie_is_vip"] and call.from_user.id != admin_id and not await db.is_vip_user(call.from_user.id):
        return await call.answer("Bu qism uchun faol VIP huquqi kerak.", show_alert=True)
    added = await db.toggle_episode_favorite(call.from_user.id, episode_id)
    await call.message.edit_reply_markup(reply_markup=await episode_markup(db, ep, call.from_user.id))
    await call.answer("❤️ Qism sevimlilarga qo‘shildi." if added else "Qism sevimlilardan olib tashlandi.")


@router.callback_query(F.data == "favorites")
async def favorites(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Sevimli kinolar", callback_data="favorites:movies")],
        [InlineKeyboardButton(text="🎞 Sevimli qismlar", callback_data="favorites:episodes")],
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")],
    ])
    await safe_edit(call, "❤️ <b>Sevimlilar</b>\n\nKerakli bo‘limni tanlang:", kb)


@router.callback_query(F.data == "favorites:movies")
async def favorite_movies(call: CallbackQuery, db: Database, admin_id: int):
    items = await db.favorite_movies(call.from_user.id)
    if call.from_user.id != admin_id and not await db.is_vip_user(call.from_user.id):
        items = [movie for movie in items if not movie["is_vip"]]
    b = InlineKeyboardBuilder()
    for movie in items:
        b.button(text=f"{movie['emoji']} {movie['title']}", callback_data=f"movie:{movie['id']}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="⬅️ Sevimlilar", callback_data="favorites"))
    b.row(InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home"))
    text = "🎬 <b>Sevimli kinolar</b>\n\nKinoni tanlang:" if items else "Hozircha sevimli kinolaringiz yo‘q."
    await safe_edit(call, text, b.as_markup())


@router.callback_query(F.data == "favorites:episodes")
async def favorite_episodes(call: CallbackQuery, db: Database, admin_id: int):
    items = await db.favorite_episodes(call.from_user.id)
    if call.from_user.id != admin_id and not await db.is_vip_user(call.from_user.id):
        items = [episode for episode in items if not episode["movie_is_vip"]]
    b = InlineKeyboardBuilder()
    for ep in items:
        b.button(
            text=f"{ep['movie_emoji']} {ep['movie_title']} — {ep['episode_number']}-QISM",
            callback_data=f"ep:{ep['id']}",
        )
    b.adjust(1)
    b.row(InlineKeyboardButton(text="⬅️ Sevimlilar", callback_data="favorites"))
    b.row(InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home"))
    text = "🎞 <b>Sevimli qismlar</b>\n\nQismni tanlang:" if items else "Hozircha sevimli qismlaringiz yo‘q."
    await safe_edit(call, text, b.as_markup())


@router.callback_query(F.data == "continue")
async def continue_watching(call: CallbackQuery, db: Database, admin_id: int):
    ep = await db.watch_progress(call.from_user.id)
    if not ep:
        return await call.answer("Hali hech qaysi qismni tomosha qilmagansiz.", show_alert=True)
    if ep["movie_is_vip"] and call.from_user.id != admin_id and not await db.is_vip_user(call.from_user.id):
        await send_episode_message(call.message, db, ep["id"], call.from_user.id, admin_id)
        return await call.answer("Faol VIP huquqi kerak.", show_alert=True)
    await send_episode_message(call.message, db, ep["id"], call.from_user.id, admin_id)
    await call.answer(f"{ep['movie_title']} — {ep['episode_number']}-QISM ochildi")


@router.callback_query(F.data == "latest")
async def latest(call: CallbackQuery, db: Database):
    eps = await db.latest_episodes()
    b = InlineKeyboardBuilder()
    for e in eps:
        b.button(text=f"{e['movie_emoji']} {e['movie_title']} — {e['episode_number']}-QISM", callback_data=f"ep:{e['id']}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home"))
    await safe_edit(call, "🔥 <b>Yangi qismlar</b>\n\nEng oxirgi qo‘shilgan qismlar:", b.as_markup())


@router.callback_query(F.data == "search")
async def search_prompt(call: CallbackQuery, state: FSMContext):
    await state.set_state(SearchFlow.query)
    await safe_edit(call, "🔎 <b>Kino qidirish</b>\n\nKino nomini yozib yuboring:", cancel_kb())


@router.message(SearchFlow.query, F.text)
async def search_result(message: Message, state: FSMContext, db: Database):
    await state.clear()
    items = await db.search_movies(message.text)
    b = InlineKeyboardBuilder()
    for m in items:
        b.button(text=f"{m['emoji']} {m['title']}", callback_data=f"movie:{m['id']}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="🔎 Yana qidirish", callback_data="search"), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home"))
    text = "Topilgan kinolar:" if items else "Bu nomdagi kino topilmadi."
    await message.answer(text, reply_markup=b.as_markup())


@router.callback_query(F.data == "requestmovie")
async def request_movie_prompt(call: CallbackQuery, state: FSMContext):
    await state.set_state(MovieRequestFlow.title)
    await safe_edit(
        call,
        "🎬 <b>Kino so‘rash</b>\n\n"
        "Botga qo‘shilishini xohlagan kino yoki serial nomini yozing:",
        cancel_kb(),
    )


@router.message(MovieRequestFlow.title, F.text)
async def request_movie_save(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    admin_id: int,
):
    title = message.text.strip()
    if len(title) < 2:
        return await message.answer("Kino nomini to‘liqroq yozing:", reply_markup=cancel_kb())
    if len(title) > 120:
        return await message.answer(
            "Kino nomi juda uzun. 120 ta belgidan qisqaroq yozing:",
            reply_markup=cancel_kb(),
        )

    result, request = await db.create_movie_request(message.from_user.id, title)
    await state.clear()
    if result == "cooldown":
        return await message.answer(
            "⏳ So‘rovlar orasida 1 daqiqa kuting.",
            reply_markup=main_menu(),
        )
    if result == "duplicate":
        return await message.answer(
            f"ℹ️ <b>{escape(title)}</b> uchun so‘rovingiz allaqachon ro‘yxatda.",
            reply_markup=main_menu(),
        )

    await message.answer(
        f"✅ <b>{escape(title)}</b> uchun so‘rovingiz qabul qilindi.\n\n"
        "Kino botga joylanganda sizga xabar beramiz.",
        reply_markup=main_menu(),
    )
    username = f"@{message.from_user.username}" if message.from_user.username else "username yo‘q"
    user_name = escape(message.from_user.full_name)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Joylandi", callback_data=f"adm:reqdone:{request['id']}:0")],
        [InlineKeyboardButton(text="🗑 O‘chirish", callback_data=f"adm:reqdelete:{request['id']}:0")],
    ])
    try:
        await bot.send_message(
            admin_id,
            "📩 <b>Yangi kino so‘rovi</b>\n\n"
            f"🎬 <b>{escape(title)}</b>\n"
            f"👤 {user_name} ({escape(username)})\n"
            f"🆔 <code>{message.from_user.id}</code>",
            reply_markup=kb,
        )
    except TelegramAPIError:
        logging.getLogger(__name__).exception("Adminga kino so‘rovi bildirishnomasi yuborilmadi")


@router.message(Command("admin"))
async def admin_command(message: Message, state: FSMContext, admin_id: int):
    await state.clear()
    if not is_admin(message.from_user.id, admin_id):
        return
    await message.answer("🔐 <b>Admin panel</b>", reply_markup=admin_menu())


@router.callback_query(F.data == "admin")
async def admin_panel(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.clear()
    await safe_edit(call, "🔐 <b>Admin panel</b>\n\nKerakli amalni tanlang:", admin_menu())


@router.callback_query(F.data == "adm:stats")
async def admin_statistics(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    stats = await db.statistics(admin_id)
    top_movies = await db.top_viewed_movies(admin_id)
    if top_movies:
        top_lines = "\n".join(
            f"{index}. {escape(movie['emoji'])} {escape(movie['title'])} — "
            f"<b>{format_number(movie['view_count'])}</b> marta"
            for index, movie in enumerate(top_movies, start=1)
        )
    else:
        top_lines = "Hozircha ko‘rishlar yo‘q."
    text = (
        "📊 <b>Bot statistikasi</b>\n\n"
        "👥 <b>Foydalanuvchilar</b>\n"
        f"• Jami: <b>{format_number(stats['total_users'])}</b>\n"
        f"• Bugun faol: <b>{format_number(stats['today_users'])}</b>\n"
        f"• Faol VIP: <b>{format_number(stats['active_vips'])}</b>\n\n"
        "▶️ <b>Qismlar ko‘rilishi</b>\n"
        f"• Jami: <b>{format_number(stats['total_views'])}</b>\n"
        f"• Bugun: <b>{format_number(stats['today_views'])}</b>\n\n"
        "🎬 <b>Kontent</b>\n"
        f"• Kinolar: <b>{format_number(stats['movie_count'])}</b>\n"
        f"• Qismlar: <b>{format_number(stats['episode_count'])}</b>\n\n"
        "❤️ <b>Sevimlilarga qo‘shilgan</b>\n"
        f"• Kinolar: <b>{format_number(stats['movie_favorites'])}</b>\n"
        f"• Qismlar: <b>{format_number(stats['episode_favorites'])}</b>\n\n"
        f"🔥 <b>Eng ko‘p ko‘rilgan kinolar</b>\n{top_lines}\n\n"
        "<i>Bugungi hisob London vaqti bo‘yicha.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Yangilash", callback_data="adm:stats")],
        [InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin")],
    ])
    await safe_edit(call, text, kb)


@router.callback_query(F.data.startswith("adm:actions:"))
async def admin_action_history(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    try:
        page = max(0, int(call.data.rsplit(":", 1)[1]))
    except (TypeError, ValueError):
        page = 0
    total = await db.admin_action_count()
    max_page = max(0, math.ceil(total / PAGE_ADMIN_ACTIONS) - 1)
    page = min(page, max_page)
    items = await db.admin_actions(page * PAGE_ADMIN_ACTIONS, PAGE_ADMIN_ACTIONS)
    lines = ["🛡 <b>Admin amallari tarixi</b>", ""]
    if not items:
        lines.append("Hozircha amallar tarixi yo‘q.")
    for item in items:
        created = item["created_at"].astimezone(LONDON_TZ).strftime("%d.%m.%Y %H:%M")
        admin_name = escape(item["admin_name"] or str(item["admin_id"]))
        lines.append(f"• <b>{escape(item['action'])}</b>")
        if item["details"]:
            lines.append(f"  {escape(item['details'])}")
        lines.append(f"  👤 {admin_name} · 🕒 {created}")
    rows = []
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm:actions:{page - 1}"))
    if page < max_page:
        navigation.append(InlineKeyboardButton(text="➡️", callback_data=f"adm:actions:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin")])
    await safe_edit(
        call,
        "\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "adm:broadcast")
async def broadcast_prompt(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.clear()
    await state.set_state(AdminFlow.broadcast_content)
    await safe_edit(
        call,
        "📣 <b>Hammaga xabar yuborish</b>\n\n"
        "Yubormoqchi bo‘lgan <b>matn, rasm yoki videoni</b> shu yerga jo‘nating. "
        "Rasm va videoga izoh yozishingiz ham mumkin.\n\n"
        "Keyingi bosqichda xabarni ko‘rib, tasdiqlaysiz.",
        cancel_kb(),
    )


@router.message(AdminFlow.broadcast_content)
async def broadcast_preview(
    message: Message,
    state: FSMContext,
    bot: Bot,
    admin_id: int,
):
    if not is_admin(message.from_user.id, admin_id):
        return
    if not (message.text or message.photo or message.video):
        return await message.answer(
            "Faqat <b>matn, rasm yoki video</b> yuboring.",
            reply_markup=cancel_kb(),
        )

    me = await bot.get_me()
    link_enabled = True
    preview = await bot.copy_message(
        chat_id=admin_id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
        reply_markup=broadcast_link_markup(me.username, link_enabled),
        protect_content=True,
    )
    await state.update_data(
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        preview_message_id=preview.message_id,
        bot_username=me.username,
        link_enabled=link_enabled,
        content_type="text" if message.text else ("photo" if message.photo else "video"),
        preview_text=(message.text or message.caption or ("Rasm" if message.photo else "Video"))[:500],
    )
    await state.set_state(AdminFlow.broadcast_ready)
    await message.answer(
        "👆 <b>Xabar ko‘rinishi</b>\n\n"
        "Bot tugmasini qoldiring yoki olib tashlang. Tayyor bo‘lsa, yuborishni tasdiqlang.",
        reply_markup=broadcast_confirm_markup(link_enabled),
    )


@router.callback_query(AdminFlow.broadcast_ready, F.data == "adm:broadcast:toggle")
async def broadcast_toggle_link(
    call: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    admin_id: int,
):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    data = await state.get_data()
    link_enabled = not data.get("link_enabled", True)
    await state.update_data(link_enabled=link_enabled)
    try:
        await bot.edit_message_reply_markup(
            chat_id=admin_id,
            message_id=data["preview_message_id"],
            reply_markup=broadcast_link_markup(data["bot_username"], link_enabled),
        )
    except TelegramBadRequest:
        pass
    await call.message.edit_reply_markup(reply_markup=broadcast_confirm_markup(link_enabled))
    await call.answer("Bot tugmasi yoqildi." if link_enabled else "Bot tugmasi olib tashlandi.")


@router.callback_query(AdminFlow.broadcast_ready, F.data == "adm:broadcast:send")
async def broadcast_send(
    call: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    db: Database,
    admin_id: int,
):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)

    data = await state.get_data()
    await state.clear()
    recipients = await db.broadcast_user_ids(admin_id)
    history = await db.create_broadcast_history(
        admin_id=admin_id,
        source_chat_id=data["source_chat_id"],
        source_message_id=data["source_message_id"],
        content_type=data.get("content_type", "unknown"),
        preview_text=data.get("preview_text"),
        link_enabled=data.get("link_enabled", True),
        total_recipients=len(recipients),
    )
    await call.answer()
    await call.message.edit_text(
        f"📤 Xabar <b>{format_number(len(recipients))} ta foydalanuvchiga</b> yuborilmoqda…"
    )

    sent = 0
    failed = 0
    markup = broadcast_link_markup(data["bot_username"], data.get("link_enabled", True))
    for row in recipients:
        user_id = row["user_id"]
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=data["source_chat_id"],
                message_id=data["source_message_id"],
                reply_markup=markup,
                protect_content=True,
            )
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=data["source_chat_id"],
                    message_id=data["source_message_id"],
                    reply_markup=markup,
                    protect_content=True,
                )
                sent += 1
            except TelegramAPIError:
                failed += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
            await db.mark_user_inactive(user_id)
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(0.055)

    await db.finish_broadcast_history(history["id"], sent, failed)
    await record_admin_action(db, call.from_user, "📣 Xabar tarqatildi", f"Yuborildi: {sent}; yuborilmadi: {failed}")
    await call.message.edit_text(
        "✅ <b>Tarqatish yakunlandi.</b>\n\n"
        f"📨 Yuborildi: <b>{format_number(sent)}</b>\n"
        f"⚠️ Yuborilmadi: <b>{format_number(failed)}</b>",
        reply_markup=admin_menu(),
    )


@router.callback_query(F.data.startswith("adm:broadcasts:"))
async def admin_broadcast_history(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    page = int(call.data.rsplit(":", 1)[1])
    count = await db.broadcast_history_count()
    page = max(0, min(page, max(0, math.ceil(count / PAGE_REQUESTS) - 1)))
    items = await db.broadcast_history(page * PAGE_REQUESTS, PAGE_REQUESTS)
    builder = InlineKeyboardBuilder()
    for item in items:
        created = item["created_at"].astimezone(LONDON_TZ).strftime("%d.%m %H:%M")
        status = "✅" if item["completed_at"] else "⏳"
        builder.button(
            text=f"{status} {created} — {item['sent_count']}/{item['total_recipients']}",
            callback_data=f"adm:broadcastitem:{item['id']}:{page}",
        )
    builder.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"adm:broadcasts:{page-1}"))
    if (page + 1) * PAGE_REQUESTS < count:
        nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"adm:broadcasts:{page+1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin"))
    text = (
        f"📜 <b>Tarqatmalar tarixi</b> — {format_number(count)} ta\n\nTarqatmani tanlang:"
        if items else
        "📜 <b>Tarqatmalar tarixi</b>\n\nHozircha tarqatmalar yo‘q."
    )
    await safe_edit(call, text, builder.as_markup())


@router.callback_query(F.data.startswith("adm:broadcastitem:"))
async def admin_broadcast_history_detail(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    _, _, history_id, page = call.data.split(":")
    item = await db.broadcast_history_item(int(history_id))
    if not item:
        return await call.answer("Tarqatma topilmadi.", show_alert=True)
    created = item["created_at"].astimezone(LONDON_TZ).strftime("%d.%m.%Y %H:%M")
    completed = (
        item["completed_at"].astimezone(LONDON_TZ).strftime("%d.%m.%Y %H:%M")
        if item["completed_at"] else "Jarayon yakunlanmagan"
    )
    preview = escape(item["preview_text"] or "Mazmun mavjud emas")
    if len(preview) > 500:
        preview = preview[:497] + "…"
    type_names = {"text": "Matn", "photo": "Rasm", "video": "Video"}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🗑 Tarixdan o‘chirish",
            callback_data=f"adm:broadcastdelete:{item['id']}:{page}",
        )],
        [InlineKeyboardButton(text="⬅️ Tarix", callback_data=f"adm:broadcasts:{page}")],
    ])
    await safe_edit(
        call,
        "📜 <b>Tarqatma tafsilotlari</b>\n\n"
        f"🕒 Boshlangan: <b>{created}</b>\n"
        f"✅ Yakunlangan: <b>{completed}</b>\n"
        f"📄 Turi: <b>{type_names.get(item['content_type'], item['content_type'])}</b>\n"
        f"🔗 Bot tugmasi: <b>{'Bor' if item['link_enabled'] else 'Yo‘q'}</b>\n"
        f"👥 Qabul qiluvchilar: <b>{format_number(item['total_recipients'])}</b>\n"
        f"📨 Yuborildi: <b>{format_number(item['sent_count'])}</b>\n"
        f"⚠️ Yuborilmadi: <b>{format_number(item['failed_count'])}</b>\n\n"
        f"📝 <b>Qisqa mazmun:</b>\n{preview}",
        kb,
    )


@router.callback_query(F.data.startswith("adm:broadcastdelete:"))
async def admin_broadcast_history_delete_confirm(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    _, _, history_id, page = call.data.split(":")
    if not await db.broadcast_history_item(int(history_id)):
        return await call.answer("Tarqatma topilmadi.", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Ha, tarixdan o‘chirish",
            callback_data=f"adm:broadcastdodelete:{history_id}:{page}",
        )],
        [InlineKeyboardButton(
            text="❌ Yo‘q",
            callback_data=f"adm:broadcastitem:{history_id}:{page}",
        )],
    ])
    await safe_edit(call, "⚠️ Ushbu tarqatma tarix yozuvi o‘chirilsinmi?", kb)


@router.callback_query(F.data.startswith("adm:broadcastdodelete:"))
async def admin_broadcast_history_delete(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    _, _, history_id, page = call.data.split(":")
    deleted = await db.delete_broadcast_history(int(history_id))
    if not deleted:
        return await call.answer("Tarqatma topilmadi.", show_alert=True)
    await safe_edit(
        call,
        "✅ Tarqatma tarixdan o‘chirildi.",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Tarqatmalar tarixi", callback_data=f"adm:broadcasts:{page}")
        ]]),
    )


@router.callback_query(F.data.startswith("adm:requests:"))
async def admin_movie_requests(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    page = int(call.data.rsplit(":", 1)[1])
    count = await db.movie_request_count()
    page = max(0, min(page, max(0, math.ceil(count / PAGE_REQUESTS) - 1)))
    items = await db.movie_requests(page * PAGE_REQUESTS, PAGE_REQUESTS)
    b = InlineKeyboardBuilder()
    for request in items:
        title = request["title"]
        short_title = title if len(title) <= 42 else f"{title[:39]}…"
        b.button(
            text=f"🎬 {short_title}",
            callback_data=f"adm:req:{request['id']}:{page}",
        )
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"adm:requests:{page-1}"))
    if (page + 1) * PAGE_REQUESTS < count:
        nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"adm:requests:{page+1}"))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin"))
    text = (
        f"📩 <b>Kino so‘rovlari</b> — {format_number(count)} ta\n\nSo‘rovni tanlang:"
        if items else
        "📩 <b>Kino so‘rovlari</b>\n\nHozircha yangi so‘rovlar yo‘q."
    )
    await safe_edit(call, text, b.as_markup())


@router.callback_query(F.data.startswith("adm:req:"))
async def admin_movie_request_detail(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    _, _, request_id, page = call.data.split(":")
    request = await db.movie_request(int(request_id))
    if not request or request["status"] != "pending":
        return await call.answer("Bu so‘rov allaqachon yopilgan.", show_alert=True)
    created = request["created_at"].astimezone(LONDON_TZ).strftime("%d.%m.%Y %H:%M")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Joylandi",
            callback_data=f"adm:reqdone:{request['id']}:{page}",
        )],
        [InlineKeyboardButton(
            text="🗑 O‘chirish",
            callback_data=f"adm:reqdelete:{request['id']}:{page}",
        )],
        [InlineKeyboardButton(text="⬅️ So‘rovlar", callback_data=f"adm:requests:{page}")],
    ])
    await safe_edit(
        call,
        "📩 <b>Kino so‘rovi</b>\n\n"
        f"🎬 <b>{escape(request['title'])}</b>\n"
        f"👤 Foydalanuvchi ID: <code>{request['user_id']}</code>\n"
        f"🕒 {created}",
        kb,
    )


@router.callback_query(F.data.startswith("adm:reqdone:"))
async def admin_complete_movie_request(
    call: CallbackQuery,
    db: Database,
    bot: Bot,
    admin_id: int,
):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    _, _, request_id, page = call.data.split(":")
    request = await db.complete_movie_request(int(request_id))
    if not request:
        return await call.answer("Bu so‘rov allaqachon yopilgan.", show_alert=True)
    await record_admin_action(db, call.from_user, "📩 Kino so‘rovi bajarildi", request["title"])
    try:
        await bot.send_message(
            request["user_id"],
            f"✅ Siz so‘ragan <b>{escape(request['title'])}</b> botga joylandi!\n\n"
            "Tomosha qilish uchun kino qidiruvidan foydalaning.",
            reply_markup=main_menu(),
        )
    except TelegramAPIError:
        logging.getLogger(__name__).exception("Kino so‘rovi egasiga xabar yuborilmadi")
    await safe_edit(
        call,
        f"✅ <b>{escape(request['title'])}</b> so‘rovi bajarildi deb belgilandi.",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ So‘rovlar", callback_data=f"adm:requests:{page}")
        ]]),
    )


@router.callback_query(F.data.startswith("adm:reqdelete:"))
async def admin_delete_movie_request(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    _, _, request_id, page = call.data.split(":")
    request = await db.delete_movie_request(int(request_id))
    if not request:
        return await call.answer("So‘rov topilmadi.", show_alert=True)
    await record_admin_action(db, call.from_user, "🗑 Kino so‘rovi o‘chirildi", request["title"])
    await safe_edit(
        call,
        f"🗑 <b>{escape(request['title'])}</b> so‘rovi o‘chirildi.",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ So‘rovlar", callback_data=f"adm:requests:{page}")
        ]]),
    )


@router.callback_query(F.data == "adm:vip")
async def admin_vip_panel(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.clear()
    await safe_edit(
        call,
        "💎 <b>VIP boshqaruvi</b>\n\nKerakli amalni tanlang:",
        vip_admin_menu(),
    )


@router.callback_query(F.data == "adm:vipadd")
async def admin_vip_add_prompt(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.set_state(AdminFlow.vip_user_id)
    await safe_edit(
        call,
        "➕ <b>VIP berish</b>\n\nFoydalanuvchining Telegram ID raqamini yozing:",
        cancel_kb(),
    )


@router.message(AdminFlow.vip_user_id, F.text)
async def admin_vip_user_save(message: Message, state: FSMContext, admin_id: int):
    if not is_admin(message.from_user.id, admin_id):
        return
    if not message.text.isdigit() or int(message.text) < 1:
        return await message.answer("Faqat Telegram ID raqamini yozing:", reply_markup=cancel_kb())
    await state.update_data(vip_user_id=int(message.text))
    await state.set_state(AdminFlow.vip_days)
    await message.answer(
        "VIP necha kun ishlashini yozing.\n\nMasalan: <code>30</code>",
        reply_markup=cancel_kb(),
    )


@router.message(AdminFlow.vip_days, F.text)
async def admin_vip_days_save(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    admin_id: int,
):
    if not is_admin(message.from_user.id, admin_id):
        return
    if not message.text.isdigit() or not 1 <= int(message.text) <= 3650:
        return await message.answer(
            "1 dan 3650 gacha kun sonini yozing:", reply_markup=cancel_kb()
        )
    data = await state.get_data()
    user_id = data["vip_user_id"]
    days = int(message.text)
    vip = await db.grant_vip(user_id, days)
    await record_admin_action(db, message.from_user, "💎 VIP berildi", f"Foydalanuvchi ID: {user_id}; {days} kun")
    await state.clear()
    expires = vip["expires_at"].astimezone(LONDON_TZ).strftime("%d.%m.%Y %H:%M")
    await message.answer(
        "✅ <b>VIP huquqi berildi.</b>\n\n"
        f"👤 ID: <code>{user_id}</code>\n"
        f"⏳ Muddat: <b>{days} kun</b>\n"
        f"📅 Tugaydi: <b>{expires}</b>",
        reply_markup=vip_admin_menu(),
    )
    try:
        await bot.send_message(
            user_id,
            "💎 <b>Sizga VIP huquqi berildi!</b>\n\n"
            f"VIP muddati: <b>{expires}</b> gacha.",
            reply_markup=main_menu(),
        )
    except TelegramAPIError:
        await message.answer("⚠️ VIP saqlandi, lekin foydalanuvchiga xabar yuborilmadi.")


@router.callback_query(F.data == "adm:viprevoke")
async def admin_vip_revoke_prompt(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.set_state(AdminFlow.vip_revoke_id)
    await safe_edit(
        call,
        "➖ <b>VIPni bekor qilish</b>\n\nFoydalanuvchining Telegram ID raqamini yozing:",
        cancel_kb(),
    )


@router.message(AdminFlow.vip_revoke_id, F.text)
async def admin_vip_revoke_save(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    admin_id: int,
):
    if not is_admin(message.from_user.id, admin_id):
        return
    if not message.text.isdigit() or int(message.text) < 1:
        return await message.answer("Faqat Telegram ID raqamini yozing:", reply_markup=cancel_kb())
    user_id = int(message.text)
    removed = await db.revoke_vip(user_id)
    await state.clear()
    if not removed:
        return await message.answer(
            "Bu foydalanuvchida VIP huquqi topilmadi.", reply_markup=vip_admin_menu()
        )
    await record_admin_action(db, message.from_user, "➖ VIP bekor qilindi", f"Foydalanuvchi ID: {user_id}")
    await message.answer("✅ VIP huquqi bekor qilindi.", reply_markup=vip_admin_menu())
    try:
        await bot.send_message(user_id, "ℹ️ VIP huquqingiz bekor qilindi.", reply_markup=main_menu())
    except TelegramAPIError:
        pass


@router.callback_query(F.data == "adm:vipmovies")
async def admin_vip_movie_picker(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await safe_edit(
        call,
        "🎬 <b>VIP kinolarni belgilash</b>\n\n"
        "💎 belgili kino faqat VIP foydalanuvchilarga ko‘rinadi. Kinoni tanlang:",
        await admin_movie_picker(db, "adm:viptoggle", "adm:vip"),
    )


@router.callback_query(F.data.startswith("adm:viptoggle:"))
async def admin_vip_movie_toggle(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    movie_id = int(call.data.rsplit(":", 1)[1])
    movie = await db.movie(movie_id)
    if not movie:
        return await call.answer("Kino topilmadi.", show_alert=True)
    updated = await db.set_movie_vip(movie_id, not movie["is_vip"])
    await record_admin_action(db, call.from_user, "💎 Kino VIP holati o‘zgartirildi", f"{updated['title']}: {'VIP' if updated['is_vip'] else 'oddiy'}")
    status = "💎 VIP qilindi" if updated["is_vip"] else "🎬 Oddiy katalogga qaytarildi"
    await safe_edit(
        call,
        f"✅ <b>{escape(updated['title'])}</b> — {status}.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎬 Yana kino belgilash", callback_data="adm:vipmovies")],
            [InlineKeyboardButton(text="⬅️ VIP boshqaruvi", callback_data="adm:vip")],
        ]),
    )


@router.callback_query(F.data == "adm:viplist")
async def admin_vip_list(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    users = await db.active_vip_users()
    lines = ["👥 <b>Faol VIP foydalanuvchilar</b>"]
    for vip in users:
        expires = vip["expires_at"].astimezone(LONDON_TZ).strftime("%d.%m.%Y %H:%M")
        lines.append(f"\n💎 <code>{vip['user_id']}</code> — {expires} gacha")
    if not users:
        lines.append("\n\nHozircha faol VIP foydalanuvchilar yo‘q.")
    await safe_edit(call, "".join(lines), vip_admin_menu())



def stars_admin_back_markup():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Stars sozlamalari", callback_data="adm:starspay")
    ]])


async def render_stars_admin(call: CallbackQuery, db: Database):
    enabled = await db.stars_payments_enabled()
    plans = await db.stars_plans()
    plan_text = "\n".join(
        f"• {days} kun — {stars} Stars" + (" (avtomatik)" if days == 30 else "")
        for days, stars in plans
    )
    rows = [
        [InlineKeyboardButton(
            text="⛔️ Stars to‘lovini o‘chirish" if enabled else "✅ Stars to‘lovini yoqish",
            callback_data="adm:starstoggle",
        )],
        [InlineKeyboardButton(text="✏️ Paket va narxlarni o‘zgartirish", callback_data="adm:starsplans")],
        [InlineKeyboardButton(text="⬅️ VIP boshqaruvi", callback_data="adm:vip")],
    ]
    await safe_edit(
        call,
        "⭐ <b>Stars to‘lov sozlamalari</b>\n\n"
        f"Holati: <b>{'✅ Yoqilgan' if enabled else '⛔️ O‘chiq'}</b>\n\n"
        f"{plan_text}\n\n"
        "30 kunlik paket Telegram talabi bo‘yicha avtomatik obuna. 10/20 kunlik paketlar bir martalik.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "adm:starspay")
async def admin_stars_panel(call: CallbackQuery, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.clear()
    await render_stars_admin(call, db)


@router.callback_query(F.data == "adm:starstoggle")
async def admin_stars_toggle(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    enabled = await db.stars_payments_enabled()
    await db.set_setting("stars_payments_enabled", "false" if enabled else "true")
    await record_admin_action(db, call.from_user, "⭐ Stars to‘lovi holati o‘zgartirildi", "O‘chirildi" if enabled else "Yoqildi")
    await render_stars_admin(call, db)


@router.callback_query(F.data == "adm:starsplans")
async def admin_stars_plans_prompt(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.set_state(AdminFlow.stars_plans)
    await safe_edit(
        call,
        "✏️ Paketlarni quyidagi ko‘rinishda yozing:\n\n"
        "<code>10:50, 20:80, 30:100</code>\n\n"
        "Birinchi raqam — kun, ikkinchisi — Stars narxi. 30 kunlik paket avtomatik obuna bo‘ladi.",
        cancel_kb(),
    )


@router.message(AdminFlow.stars_plans, F.text)
async def admin_stars_plans_save(message: Message, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(message.from_user.id, admin_id):
        return
    plans = []
    try:
        for item in message.text.split(","):
            days_text, stars_text = item.strip().split(":", 1)
            days, stars = int(days_text), int(stars_text)
            if not 1 <= days <= 3650 or not 1 <= stars <= 10000:
                raise ValueError
            plans.append((days, stars))
    except ValueError:
        return await message.answer(
            "Format noto‘g‘ri. Masalan: <code>10:50, 20:80, 30:100</code>",
            reply_markup=cancel_kb(),
        )
    if not plans or len(plans) > 6 or len({days for days, _ in plans}) != len(plans):
        return await message.answer("1–6 ta takrorlanmagan paket kiriting.", reply_markup=cancel_kb())
    await db.set_stars_plans(plans)
    await record_admin_action(db, message.from_user, "⭐ Stars paketlari yangilandi", ", ".join(f"{d} kun={s}⭐" for d, s in plans))
    await state.clear()
    await message.answer("✅ Stars paketlari saqlandi.", reply_markup=stars_admin_back_markup())


def manual_payment_back_markup():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Karta to‘lov sozlamalari", callback_data="adm:manualpay")
    ]])


async def render_manual_payment_admin(
    call: CallbackQuery,
    db: Database,
    payment_url: str | None,
):
    settings = await db.manual_payment_settings()
    status = "✅ Yoqilgan" if settings["enabled"] else "⛔️ O‘chiq"
    digits = "".join(ch for ch in settings["card_number"] if ch.isdigit())
    grouped = " ".join(digits[i:i + 4] for i in range(0, len(digits), 4)) or "Kiritilmagan"
    holder = settings["card_holder"] or "Kiritilmagan"
    rows = [
        [InlineKeyboardButton(
            text="⛔️ Karta to‘lovini o‘chirish" if settings["enabled"] else "✅ Karta to‘lovini yoqish",
            callback_data="adm:manualtoggle",
        )],
        [InlineKeyboardButton(text="💳 Karta raqamini o‘zgartirish", callback_data="adm:manualcard")],
        [InlineKeyboardButton(text="👤 Karta egasini o‘zgartirish", callback_data="adm:manualholder")],
        [InlineKeyboardButton(text="💰 VIP narxini o‘zgartirish", callback_data="adm:manualprice")],
        [InlineKeyboardButton(text="⏳ VIP muddatini o‘zgartirish", callback_data="adm:manualdays")],
    ]
    if payment_url:
        rows.append([InlineKeyboardButton(text="🌐 To‘lov sahifasini ochish", url=payment_url)])
    rows.append([InlineKeyboardButton(text="⬅️ VIP boshqaruvi", callback_data="adm:vip")])
    await safe_edit(
        call,
        "💳 <b>Karta to‘lov sozlamalari</b>\n\n"
        f"Holati: <b>{status}</b>\n"
        f"Karta: <code>{escape(grouped)}</code>\n"
        f"Karta egasi: <b>{escape(holder)}</b>\n"
        f"Narx: <b>{format_number(settings['price_uzs'])} so‘m</b>\n"
        f"VIP muddati: <b>{settings['vip_days']} kun</b>\n\n"
        "Chek kelganda bank ilovasida pul tushganini tekshirib tasdiqlang.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "adm:manualpay")
async def admin_manual_payment_panel(
    call: CallbackQuery,
    state: FSMContext,
    db: Database,
    admin_id: int,
    payment_url: str | None,
):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.clear()
    await render_manual_payment_admin(call, db, payment_url)


@router.callback_query(F.data == "adm:manualtoggle")
async def admin_manual_payment_toggle(
    call: CallbackQuery,
    db: Database,
    admin_id: int,
    payment_url: str | None,
):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    settings = await db.manual_payment_settings()
    if not settings["enabled"] and (not settings["card_number"] or not settings["card_holder"]):
        return await call.answer("Avval karta raqami va karta egasini kiriting.", show_alert=True)
    await db.set_setting("manual_payments_enabled", "false" if settings["enabled"] else "true")
    await record_admin_action(db, call.from_user, "💳 Karta to‘lovi holati o‘zgartirildi", "O‘chirildi" if settings["enabled"] else "Yoqildi")
    await render_manual_payment_admin(call, db, payment_url)


@router.callback_query(F.data == "adm:manualcard")
async def admin_manual_card_prompt(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.set_state(AdminFlow.manual_card_number)
    await safe_edit(
        call,
        "💳 Karta raqamini yozing.\n\nFaqat Uzcard/Humo karta raqami; CVV, muddat yoki SMS kod yubormang.",
        cancel_kb(),
    )


@router.message(AdminFlow.manual_card_number, F.text)
async def admin_manual_card_save(message: Message, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(message.from_user.id, admin_id):
        return
    digits = "".join(ch for ch in message.text if ch.isdigit())
    if not 16 <= len(digits) <= 19:
        return await message.answer("Karta raqamini to‘g‘ri kiriting (16–19 ta raqam).", reply_markup=cancel_kb())
    await db.set_setting("manual_card_number", digits)
    await record_admin_action(db, message.from_user, "💳 Karta raqami yangilandi", "Maxfiy qiymat tarixga yozilmadi")
    await state.clear()
    await message.answer("✅ Karta raqami saqlandi.", reply_markup=manual_payment_back_markup())


@router.callback_query(F.data == "adm:manualholder")
async def admin_manual_holder_prompt(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.set_state(AdminFlow.manual_card_holder)
    await safe_edit(call, "👤 Karta egasining ism-familiyasini yozing:", cancel_kb())


@router.message(AdminFlow.manual_card_holder, F.text)
async def admin_manual_holder_save(message: Message, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(message.from_user.id, admin_id):
        return
    holder = message.text.strip()
    if not 2 <= len(holder) <= 80:
        return await message.answer("Ism-familiya 2–80 ta belgi bo‘lsin.", reply_markup=cancel_kb())
    await db.set_setting("manual_card_holder", holder)
    await record_admin_action(db, message.from_user, "👤 Karta egasi yangilandi")
    await state.clear()
    await message.answer("✅ Karta egasi saqlandi.", reply_markup=manual_payment_back_markup())


@router.callback_query(F.data == "adm:manualprice")
async def admin_manual_price_prompt(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.set_state(AdminFlow.manual_price_uzs)
    await safe_edit(call, "💰 VIP narxini so‘mda yozing. Masalan: <code>50000</code>", cancel_kb())


@router.message(AdminFlow.manual_price_uzs, F.text)
async def admin_manual_price_save(message: Message, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(message.from_user.id, admin_id):
        return
    digits = "".join(ch for ch in message.text if ch.isdigit())
    if not digits or not 1000 <= int(digits) <= 100000000:
        return await message.answer("1 000 dan 100 000 000 so‘mgacha summa yozing.", reply_markup=cancel_kb())
    await db.set_setting("vip_price_uzs", digits)
    await record_admin_action(db, message.from_user, "💰 VIP narxi yangilandi", f"{format_number(int(digits))} so‘m")
    await state.clear()
    await message.answer("✅ VIP narxi saqlandi.", reply_markup=manual_payment_back_markup())


@router.callback_query(F.data == "adm:manualdays")
async def admin_manual_days_prompt(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.set_state(AdminFlow.manual_vip_days)
    await safe_edit(call, "⏳ VIP necha kun ishlashini yozing. Masalan: <code>30</code>", cancel_kb())


@router.message(AdminFlow.manual_vip_days, F.text)
async def admin_manual_days_save(message: Message, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(message.from_user.id, admin_id):
        return
    if not message.text.isdigit() or not 1 <= int(message.text) <= 3650:
        return await message.answer("1 dan 3650 gacha kun yozing.", reply_markup=cancel_kb())
    await db.set_setting("vip_days", message.text)
    await record_admin_action(db, message.from_user, "⏳ VIP muddati yangilandi", f"{message.text} kun")
    await state.clear()
    await message.answer("✅ VIP muddati saqlandi.", reply_markup=manual_payment_back_markup())


@router.callback_query(F.data.startswith("adm:payapprove:"))
async def admin_manual_payment_approve(
    call: CallbackQuery,
    db: Database,
    bot: Bot,
    admin_id: int,
):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    payment_id = int(call.data.rsplit(":", 1)[1])
    payment = await db.review_manual_payment(payment_id, "approved")
    if not payment:
        return await call.answer("Bu chek oldin ko‘rib chiqilgan.", show_alert=True)
    settings = await db.manual_payment_settings()
    vip = await db.grant_vip(payment["user_id"], settings["vip_days"])
    await record_admin_action(db, call.from_user, "✅ To‘lov cheki tasdiqlandi", f"Foydalanuvchi ID: {payment['user_id']}")
    expires = vip["expires_at"].astimezone(LONDON_TZ).strftime("%d.%m.%Y %H:%M")
    await call.message.edit_caption(
        caption=(call.message.caption or "") + f"\n\n✅ <b>TASDIQLANDI</b>\nVIP: {expires} gacha",
        reply_markup=None,
    )
    await call.answer("VIP berildi.")
    try:
        await bot.send_message(
            payment["user_id"],
            "✅ <b>To‘lovingiz tasdiqlandi!</b>\n\n"
            f"💎 VIP huquqi <b>{expires}</b> gacha faollashtirildi.",
            reply_markup=main_menu(),
        )
    except TelegramAPIError:
        await call.message.answer("⚠️ VIP berildi, ammo foydalanuvchiga xabar yuborilmadi.")


@router.callback_query(F.data.startswith("adm:payreject:"))
async def admin_manual_payment_reject(
    call: CallbackQuery,
    db: Database,
    bot: Bot,
    admin_id: int,
):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    payment_id = int(call.data.rsplit(":", 1)[1])
    payment = await db.review_manual_payment(payment_id, "rejected")
    if not payment:
        return await call.answer("Bu chek oldin ko‘rib chiqilgan.", show_alert=True)
    await record_admin_action(db, call.from_user, "❌ To‘lov cheki rad etildi", f"Foydalanuvchi ID: {payment['user_id']}")
    await call.message.edit_caption(
        caption=(call.message.caption or "") + "\n\n❌ <b>RAD ETILDI</b>",
        reply_markup=None,
    )
    await call.answer("Chek rad etildi.")
    try:
        await bot.send_message(
            payment["user_id"],
            "❌ To‘lov chekingiz tasdiqlanmadi. Iltimos, summa va chekni tekshirib admin bilan bog‘laning.",
            reply_markup=main_menu(),
        )
    except TelegramAPIError:
        pass


@router.callback_query(F.data == "adm:addmovie")
async def add_movie_prompt(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id): return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.set_state(AdminFlow.movie_title)
    await safe_edit(call, "➕ Kino nomini yuboring.\n\nMasalan: <code>☀️ APOLLO</code>\nEmoji yozmasangiz 🎬 qo‘yiladi.", cancel_kb())


@router.message(AdminFlow.movie_title, F.text)
async def add_movie_save(message: Message, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(message.from_user.id, admin_id): return
    raw = message.text.strip()
    match = re.match(r"^([^\w\s]{1,4})\s+(.+)$", raw, re.UNICODE)
    emoji, title = (match.group(1), match.group(2)) if match else ("🎬", raw)
    try:
        movie = await db.add_movie(title, emoji)
    except asyncpg.UniqueViolationError:
        return await message.answer("Bu nomdagi kino oldin qo‘shilgan. Boshqa nom yozing:", reply_markup=cancel_kb())
    await record_admin_action(db, message.from_user, "🎬 Yangi kino qo‘shildi", f"{movie['emoji']} {movie['title']}")
    await state.update_data(movie_id=movie["id"], movie_label=f"{movie['emoji']} {movie['title']}")
    await state.set_state(AdminFlow.movie_poster)
    await message.answer(
        f"✅ <b>{escape(movie['emoji'])} {escape(movie['title'])}</b> yaratildi.\n\n"
        "🖼 Endi kino posterini <b>rasm ko‘rinishida</b> yuboring.",
        reply_markup=skip_movie_poster_kb(),
    )


async def ask_movie_description(message: Message, state: FSMContext):
    await state.set_state(AdminFlow.movie_description)
    await message.answer(
        "📝 Endi kino haqida qisqa tavsif yozing.\n\n"
        "Masalan: <i>Sevgi, sir va kutilmagan voqealarga boy serial.</i>\n"
        "Tavsif 700 ta belgidan oshmasin.",
        reply_markup=skip_movie_description_kb(),
    )


@router.message(AdminFlow.movie_poster, F.photo)
async def add_movie_poster(message: Message, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(message.from_user.id, admin_id): return
    data = await state.get_data()
    await db.set_movie_poster(data["movie_id"], message.photo[-1].file_id)
    await message.answer("✅ Poster saqlandi.")
    await ask_movie_description(message, state)


@router.callback_query(AdminFlow.movie_poster, F.data == "adm:skipposter")
async def skip_movie_poster(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id): return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await call.answer()
    await ask_movie_description(call.message, state)


@router.message(AdminFlow.movie_poster)
async def movie_poster_required(message: Message):
    await message.answer("Iltimos, posterni oddiy rasm ko‘rinishida yuboring.", reply_markup=skip_movie_poster_kb())


@router.message(AdminFlow.movie_description, F.text)
async def add_movie_description(message: Message, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(message.from_user.id, admin_id): return
    text = message.text.strip()
    if len(text) > 700:
        return await message.answer("Tavsif juda uzun. 700 ta belgidan qisqaroq yozing:", reply_markup=skip_movie_description_kb())
    data = await state.get_data()
    await db.set_movie_description(data["movie_id"], text)
    await state.clear()
    await message.answer("✅ Poster va tavsif saqlandi. Kino tayyor!", reply_markup=admin_menu())


@router.callback_query(AdminFlow.movie_description, F.data == "adm:skipdescription")
async def skip_movie_description(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id): return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.clear()
    await safe_edit(call, "✅ Kino saqlandi.", admin_menu())


async def admin_movie_picker(db: Database, action: str, back="admin"):
    items = await db.movies(0, 100)
    b = InlineKeyboardBuilder()
    for m in items:
        marker = "💎" if m["is_vip"] else m["emoji"]
        b.button(text=f"{marker} {m['title']}", callback_data=f"{action}:{m['id']}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="⬅️ Admin panel", callback_data=back))
    return b.as_markup()


@router.callback_query(F.data == "adm:addepisode")
async def add_ep_pick_movie(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id): return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await safe_edit(call, "➕ <b>Yangi qism</b>\n\nKinoni tanlang:", await admin_movie_picker(db, "adm:epmovie"))


@router.callback_query(F.data.startswith("adm:epmovie:"))
async def add_ep_number(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id): return await call.answer("Ruxsat yo‘q.", show_alert=True)
    movie_id = int(call.data.rsplit(":", 1)[1])
    await state.update_data(movie_id=movie_id)
    await state.set_state(AdminFlow.episode_number)
    await safe_edit(call, "Qism raqamini yozing. Masalan: <code>3</code>", cancel_kb())


@router.message(AdminFlow.episode_number, F.text)
async def add_ep_wait_video(message: Message, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(message.from_user.id, admin_id): return
    if not message.text.isdigit() or int(message.text) < 1:
        return await message.answer("Faqat musbat raqam yozing. Masalan: <code>3</code>", reply_markup=cancel_kb())
    data = await state.get_data()
    try:
        ep = await db.upsert_episode(data["movie_id"], int(message.text))
    except asyncpg.UniqueViolationError:
        return await message.answer("Bu qism raqami mavjud.", reply_markup=admin_menu())
    await state.update_data(episode_id=ep["id"], episode_number=int(message.text))
    await state.set_state(AdminFlow.episode_video)
    await message.answer("📹 Endi videoni shu botga yuboring.\n\n<i>Video fayl sifatida emas, oddiy video ko‘rinishida yuboring.</i>", reply_markup=cancel_kb())


@router.message(AdminFlow.episode_video, F.video)
async def add_ep_video(
    message: Message, state: FSMContext, db: Database, admin_id: int, bot: Bot, channel_id: str | None
):
    if not is_admin(message.from_user.id, admin_id): return
    data = await state.get_data()
    await db.set_episode_video(data["episode_id"], message.video.file_id, message.video.file_unique_id)
    await record_admin_action(db, message.from_user, "▶️ Qism videosi saqlandi", f"{data['episode_number']}-QISM")
    await state.clear()
    await message.answer(f"✅ {data['episode_number']}-QISM videosi saqlandi va foydalanuvchilarga ochildi.", reply_markup=admin_menu())
    ep = await db.episode(data["episode_id"])
    if channel_id and not ep["movie_is_vip"]:
        me = await bot.get_me()
        announcement = (
            f"🔥 <b>YANGI QISM!</b>\n\n"
            f"{ep['movie_emoji']} <b>{ep['movie_title']}</b> — {ep['episode_number']}-QISM\n\n"
            "🎬 Tomosha qilish uchun pastdagi tugmani bosing."
        )
        button = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🎬 Tomosha qilish", url=f"https://t.me/{me.username}?start=ep_{ep['id']}"
            )
        ]])
        try:
            await bot.send_message(channel_id, announcement, reply_markup=button)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            await message.answer(
                "⚠️ Qism saqlandi, lekin kanalga e’lon yuborilmadi. "
                "Botni kanalga <b>Post Messages</b> huquqi bilan admin qiling."
            )


@router.message(AdminFlow.episode_video)
async def video_required(message: Message):
    await message.answer("Iltimos, aynan video yuboring.", reply_markup=cancel_kb())


@router.callback_query(F.data == "adm:bulk")
async def bulk_pick_movie(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await safe_edit(
        call,
        "📚 <b>Qismlarni ketma-ket yuklash</b>\n\nKinoni tanlang:",
        await admin_movie_picker(db, "adm:bulkmovie"),
    )


@router.callback_query(F.data.startswith("adm:bulkmovie:"))
async def bulk_start_prompt(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await state.update_data(movie_id=int(call.data.rsplit(":", 1)[1]))
    await state.set_state(AdminFlow.bulk_start_number)
    await safe_edit(
        call,
        "Birinchi video qaysi qismdan boshlanishini yozing.\n\nMasalan: <code>1</code>",
        cancel_kb(),
    )


@router.message(AdminFlow.bulk_start_number, F.text)
async def bulk_start_save(message: Message, state: FSMContext, admin_id: int):
    if not is_admin(message.from_user.id, admin_id):
        return
    if not message.text.isdigit() or int(message.text) < 1:
        return await message.answer("Faqat musbat raqam yozing. Masalan: <code>1</code>", reply_markup=cancel_kb())
    start_number = int(message.text)
    await state.update_data(next_episode_number=start_number, uploaded_count=0)
    await state.set_state(AdminFlow.bulk_videos)
    await message.answer(
        f"📹 Videolarni tartib bilan yuboring.\n\n"
        f"Birinchi video <b>{start_number}-QISM</b> bo‘ladi. Har bir videodan keyin qism raqami avtomatik oshadi.\n\n"
        "<i>Videolarni oddiy video ko‘rinishida yuboring. Eski qismlarni yuklashda kanalga alohida e’lon chiqmaydi.</i>",
        reply_markup=bulk_upload_kb(),
    )


@router.message(AdminFlow.bulk_videos, F.video)
async def bulk_video_save(message: Message, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(message.from_user.id, admin_id):
        return
    data = await state.get_data()
    number = data["next_episode_number"]
    await db.upsert_episode(
        data["movie_id"], number, message.video.file_id, message.video.file_unique_id
    )
    await record_admin_action(db, message.from_user, "📚 Ketma-ket qism yuklandi", f"{number}-QISM")
    uploaded_count = data["uploaded_count"] + 1
    await state.update_data(next_episode_number=number + 1, uploaded_count=uploaded_count)
    await message.answer(
        f"✅ <b>{number}-QISM</b> saqlandi.\n\nKeyingi video <b>{number + 1}-QISM</b> bo‘ladi.",
        reply_markup=bulk_upload_kb(),
    )


@router.callback_query(AdminFlow.bulk_videos, F.data == "adm:bulkfinish")
async def bulk_finish(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id):
        return await call.answer("Ruxsat yo‘q.", show_alert=True)
    data = await state.get_data()
    count = data.get("uploaded_count", 0)
    await state.clear()
    await safe_edit(
        call,
        f"✅ Ketma-ket yuklash tugadi. <b>{count} ta qism</b> saqlandi.\n\n"
        "Yangi qismga kanal e’loni chiqarish uchun odatdagi <b>➕ Yangi qism qo‘shish</b> tugmasidan foydalaning.",
        admin_menu(),
    )


@router.message(AdminFlow.bulk_videos)
async def bulk_video_required(message: Message):
    await message.answer(
        "Iltimos, aynan video yuboring yoki <b>✅ Yuklashni tugatish</b>ni bosing.",
        reply_markup=bulk_upload_kb(),
    )


@router.callback_query(F.data == "adm:list")
async def admin_list(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id): return await call.answer("Ruxsat yo‘q.", show_alert=True)
    items = await db.movies(0, 100)
    lines = ["📋 <b>Kinolar ro‘yxati</b>"]
    for m in items:
        count = await db.episode_count(m["id"], ready_only=False)
        lines.append(f"\n{m['emoji']} <b>{m['title']}</b> — {count} ta qism")
    await safe_edit(call, "".join(lines) if items else "Kinolar hali yo‘q.", admin_menu())


@router.callback_query(F.data == "adm:edit")
async def edit_options(call: CallbackQuery, admin_id: int):
    if not is_admin(call.from_user.id, admin_id): return await call.answer("Ruxsat yo‘q.", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Kino nomini o‘zgartirish", callback_data="adm:editmovie")],
        [InlineKeyboardButton(text="🖼 Kino posterini o‘zgartirish", callback_data="adm:editposter")],
        [InlineKeyboardButton(text="📝 Kino tavsifini o‘zgartirish", callback_data="adm:editdescription")],
        [InlineKeyboardButton(text="🔢 Qism raqamini o‘zgartirish", callback_data="adm:editepisode")],
        [InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin")],
    ])
    await safe_edit(call, "Nimani tahrirlamoqchisiz?", kb)


@router.callback_query(F.data == "adm:editmovie")
async def edit_movie_picker(call: CallbackQuery, db: Database):
    await safe_edit(call, "Nomini o‘zgartiradigan kinoni tanlang:", await admin_movie_picker(db, "adm:renamemovie"))


@router.callback_query(F.data.startswith("adm:renamemovie:"))
async def rename_movie_prompt(call: CallbackQuery, state: FSMContext):
    await state.update_data(movie_id=int(call.data.rsplit(":", 1)[1]))
    await state.set_state(AdminFlow.rename_movie)
    await safe_edit(call, "Yangi kino nomini yozing:", cancel_kb())


@router.message(AdminFlow.rename_movie, F.text)
async def rename_movie_save(message: Message, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(message.from_user.id, admin_id): return
    data = await state.get_data()
    try:
        await db.rename_movie(data["movie_id"], message.text)
        await record_admin_action(db, message.from_user, "✏️ Kino nomi o‘zgartirildi", message.text.strip())
    except asyncpg.UniqueViolationError:
        return await message.answer("Bu nom band. Boshqa nom yozing:", reply_markup=cancel_kb())
    await state.clear()
    await message.answer("✅ Kino nomi o‘zgartirildi.", reply_markup=admin_menu())


@router.callback_query(F.data == "adm:editposter")
async def edit_poster_picker(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id): return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await safe_edit(call, "Posterini o‘zgartiradigan kinoni tanlang:", await admin_movie_picker(db, "adm:setposter"))


@router.callback_query(F.data.startswith("adm:setposter:"))
async def edit_poster_prompt(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id): return await call.answer("Ruxsat yo‘q.", show_alert=True)
    movie_id = int(call.data.rsplit(":", 1)[1])
    await state.update_data(movie_id=movie_id)
    await state.set_state(AdminFlow.update_movie_poster)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Hozirgi posterni o‘chirish", callback_data=f"adm:clearposter:{movie_id}")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")],
    ])
    await safe_edit(call, "🖼 Yangi posterni rasm ko‘rinishida yuboring:", kb)


@router.message(AdminFlow.update_movie_poster, F.photo)
async def edit_poster_save(message: Message, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(message.from_user.id, admin_id): return
    data = await state.get_data()
    await db.set_movie_poster(data["movie_id"], message.photo[-1].file_id)
    await record_admin_action(db, message.from_user, "🖼 Kino posteri yangilandi", f"Kino ID: {data['movie_id']}")
    await state.clear()
    await message.answer("✅ Kino posteri yangilandi.", reply_markup=admin_menu())


@router.callback_query(F.data.startswith("adm:clearposter:"))
async def clear_poster(call: CallbackQuery, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id): return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await db.set_movie_poster(int(call.data.rsplit(":", 1)[1]), None)
    await state.clear()
    await safe_edit(call, "✅ Kino posteri o‘chirildi.", admin_menu())


@router.message(AdminFlow.update_movie_poster)
async def update_poster_required(message: Message):
    await message.answer("Iltimos, posterni oddiy rasm ko‘rinishida yuboring.", reply_markup=cancel_kb())


@router.callback_query(F.data == "adm:editdescription")
async def edit_description_picker(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id): return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await safe_edit(call, "Tavsifini o‘zgartiradigan kinoni tanlang:", await admin_movie_picker(db, "adm:setdescription"))


@router.callback_query(F.data.startswith("adm:setdescription:"))
async def edit_description_prompt(call: CallbackQuery, state: FSMContext, admin_id: int):
    if not is_admin(call.from_user.id, admin_id): return await call.answer("Ruxsat yo‘q.", show_alert=True)
    movie_id = int(call.data.rsplit(":", 1)[1])
    await state.update_data(movie_id=movie_id)
    await state.set_state(AdminFlow.update_movie_description)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Hozirgi tavsifni o‘chirish", callback_data=f"adm:cleardescription:{movie_id}")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")],
    ])
    await safe_edit(call, "📝 Yangi tavsifni yozib yuboring (700 ta belgigacha):", kb)


@router.message(AdminFlow.update_movie_description, F.text)
async def edit_description_save(message: Message, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(message.from_user.id, admin_id): return
    text = message.text.strip()
    if len(text) > 700:
        return await message.answer("Tavsif juda uzun. 700 ta belgidan qisqaroq yozing:", reply_markup=cancel_kb())
    data = await state.get_data()
    await db.set_movie_description(data["movie_id"], text)
    await record_admin_action(db, message.from_user, "📝 Kino tavsifi yangilandi", f"Kino ID: {data['movie_id']}")
    await state.clear()
    await message.answer("✅ Kino tavsifi yangilandi.", reply_markup=admin_menu())


@router.callback_query(F.data.startswith("adm:cleardescription:"))
async def clear_description(call: CallbackQuery, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id): return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await db.set_movie_description(int(call.data.rsplit(":", 1)[1]), None)
    await state.clear()
    await safe_edit(call, "✅ Kino tavsifi o‘chirildi.", admin_menu())


async def admin_episode_picker(db: Database, movie_id: int, action: str):
    eps = await db.all_episodes_admin(movie_id, 0, 100)
    b = InlineKeyboardBuilder()
    for e in eps:
        status = "✅" if e["file_id"] else "⚠️"
        b.button(text=f"{status} {e['episode_number']}-QISM", callback_data=f"{action}:{e['id']}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin"))
    return b.as_markup()


@router.callback_query(F.data == "adm:editepisode")
async def edit_ep_movie(call: CallbackQuery, db: Database):
    await safe_edit(call, "Kinoni tanlang:", await admin_movie_picker(db, "adm:editparts"))


@router.callback_query(F.data.startswith("adm:editparts:"))
async def edit_ep_picker(call: CallbackQuery, db: Database):
    movie_id = int(call.data.rsplit(":", 1)[1])
    await safe_edit(call, "Qismni tanlang:", await admin_episode_picker(db, movie_id, "adm:renumberep"))


@router.callback_query(F.data.startswith("adm:renumberep:"))
async def renumber_prompt(call: CallbackQuery, state: FSMContext):
    await state.update_data(episode_id=int(call.data.rsplit(":", 1)[1]))
    await state.set_state(AdminFlow.renumber_episode)
    await safe_edit(call, "Yangi qism raqamini yozing:", cancel_kb())


@router.message(AdminFlow.renumber_episode, F.text)
async def renumber_save(message: Message, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(message.from_user.id, admin_id): return
    if not message.text.isdigit() or int(message.text) < 1:
        return await message.answer("Musbat raqam yozing:", reply_markup=cancel_kb())
    data = await state.get_data()
    try:
        await db.set_episode_number(data["episode_id"], int(message.text))
        await record_admin_action(db, message.from_user, "🔢 Qism raqami o‘zgartirildi", f"Yangi raqam: {message.text}")
    except asyncpg.UniqueViolationError:
        return await message.answer("Bu raqam shu kinoda mavjud.", reply_markup=cancel_kb())
    await state.clear()
    await message.answer("✅ Qism raqami o‘zgartirildi.", reply_markup=admin_menu())


@router.callback_query(F.data == "adm:video")
async def video_movie_picker(call: CallbackQuery, db: Database, admin_id: int):
    if not is_admin(call.from_user.id, admin_id): return await call.answer("Ruxsat yo‘q.", show_alert=True)
    await safe_edit(call, "Video qo‘shiladigan kinoni tanlang:", await admin_movie_picker(db, "adm:videoparts"))


@router.callback_query(F.data.startswith("adm:videoparts:"))
async def video_ep_picker(call: CallbackQuery, db: Database):
    movie_id = int(call.data.rsplit(":", 1)[1])
    await safe_edit(call, "Qismni tanlang:", await admin_episode_picker(db, movie_id, "adm:replacevideo"))


@router.callback_query(F.data.startswith("adm:replacevideo:"))
async def replace_video_prompt(call: CallbackQuery, state: FSMContext):
    await state.update_data(episode_id=int(call.data.rsplit(":", 1)[1]))
    await state.set_state(AdminFlow.replace_video)
    await safe_edit(call, "Yangi videoni yuboring:", cancel_kb())


@router.message(AdminFlow.replace_video, F.video)
async def replace_video_save(message: Message, state: FSMContext, db: Database, admin_id: int):
    if not is_admin(message.from_user.id, admin_id): return
    data = await state.get_data()
    await db.set_episode_video(data["episode_id"], message.video.file_id, message.video.file_unique_id)
    await record_admin_action(db, message.from_user, "📹 Qism videosi almashtirildi", f"Qism ID: {data['episode_id']}")
    await state.clear()
    await message.answer("✅ Video saqlandi/almashtirildi.", reply_markup=admin_menu())


@router.callback_query(F.data == "adm:delete")
async def delete_options(call: CallbackQuery, admin_id: int):
    if not is_admin(call.from_user.id, admin_id): return await call.answer("Ruxsat yo‘q.", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Kino va barcha qismlari", callback_data="adm:deletemovie")],
        [InlineKeyboardButton(text="🗑 Faqat bitta qism", callback_data="adm:deleteepisode")],
        [InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin")],
    ])
    await safe_edit(call, "Nimani o‘chirmoqchisiz?", kb)


@router.callback_query(F.data == "adm:deletemovie")
async def delete_movie_picker(call: CallbackQuery, db: Database):
    await safe_edit(call, "⚠️ O‘chiriladigan kinoni tanlang:", await admin_movie_picker(db, "adm:confirmdelmovie"))


@router.callback_query(F.data.startswith("adm:confirmdelmovie:"))
async def confirm_delete_movie(call: CallbackQuery, db: Database):
    movie_id = int(call.data.rsplit(":", 1)[1])
    movie = await db.movie(movie_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ha, o‘chirilsin", callback_data=f"adm:dodelmovie:{movie_id}")],
        [InlineKeyboardButton(text="❌ Yo‘q", callback_data="admin")],
    ])
    await safe_edit(call, f"⚠️ <b>{movie['title']}</b> va uning barcha qismlari o‘chirilsinmi?", kb)


@router.callback_query(F.data.startswith("adm:dodelmovie:"))
async def do_delete_movie(call: CallbackQuery, db: Database):
    movie_id = int(call.data.rsplit(":", 1)[1])
    movie = await db.movie(movie_id)
    await db.delete_movie(movie_id)
    await record_admin_action(db, call.from_user, "🗑 Kino o‘chirildi", movie["title"] if movie else f"Kino ID: {movie_id}")
    await safe_edit(call, "✅ Kino va qismlari o‘chirildi.", admin_menu())


@router.callback_query(F.data == "adm:deleteepisode")
async def delete_ep_movie(call: CallbackQuery, db: Database):
    await safe_edit(call, "Kinoni tanlang:", await admin_movie_picker(db, "adm:deleteparts"))


@router.callback_query(F.data.startswith("adm:deleteparts:"))
async def delete_ep_picker(call: CallbackQuery, db: Database):
    movie_id = int(call.data.rsplit(":", 1)[1])
    await safe_edit(call, "O‘chiriladigan qismni tanlang:", await admin_episode_picker(db, movie_id, "adm:confirmdelepisode"))


@router.callback_query(F.data.startswith("adm:confirmdelepisode:"))
async def confirm_delete_ep(call: CallbackQuery, db: Database):
    ep_id = int(call.data.rsplit(":", 1)[1])
    ep = await db.episode(ep_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ha, o‘chirilsin", callback_data=f"adm:dodeleteepisode:{ep_id}")],
        [InlineKeyboardButton(text="❌ Yo‘q", callback_data="admin")],
    ])
    await safe_edit(call, f"⚠️ <b>{ep['movie_title']} — {ep['episode_number']}-QISM</b> o‘chirilsinmi?", kb)


@router.callback_query(F.data.startswith("adm:dodeleteepisode:"))
async def do_delete_ep(call: CallbackQuery, db: Database):
    episode_id = int(call.data.rsplit(":", 1)[1])
    episode = await db.episode(episode_id)
    await db.delete_episode(episode_id)
    details = f"{episode['movie_title']} — {episode['episode_number']}-QISM" if episode else f"Qism ID: {episode_id}"
    await record_admin_action(db, call.from_user, "🗑 Qism o‘chirildi", details)
    await safe_edit(call, "✅ Qism o‘chirildi.", admin_menu())


def redact_error_message(value: str) -> str:
    value = re.sub(r"postgres(?:ql)?://\\S+", "[DATABASE_URL REDACTED]", value, flags=re.I)
    value = re.sub(r"\\b\\d{8,12}:[A-Za-z0-9_-]{20,}\\b", "[BOT_TOKEN REDACTED]", value)
    value = re.sub(
        r"(?i)(token|password|secret|authorization|database_url)\\s*[=:]\\s*\\S+",
        r"\\1=[REDACTED]",
        value,
    )
    return value[:500]


@router.error()
async def global_error_handler(event: ErrorEvent, bot: Bot, admin_id: int):
    error = event.exception
    logging.getLogger(__name__).exception(
        "Unhandled bot update error",
        exc_info=(type(error), error, error.__traceback__),
    )
    raw_message = redact_error_message(str(error) or "Tafsilot mavjud emas")
    signature = f"{type(error).__name__}:{raw_message}"
    now = time.monotonic()
    if now - _last_error_alerts.get(signature, 0) < ERROR_ALERT_COOLDOWN:
        return True
    _last_error_alerts[signature] = now
    update = event.update
    source = (
        getattr(update, "message", None)
        or getattr(update, "callback_query", None)
        or getattr(update, "edited_message", None)
    )
    user = getattr(source, "from_user", None)
    user_line = f"\n👤 Foydalanuvchi ID: <code>{user.id}</code>" if user else ""
    timestamp = __import__("datetime").datetime.now(LONDON_TZ).strftime("%d.%m.%Y %H:%M:%S")
    try:
        await bot.send_message(
            admin_id,
            "🚨 <b>AIKINO_UZ_BOT xatosi</b>\n\n"
            f"🕒 Vaqt: <b>{timestamp}</b>\n"
            f"⚠️ Turi: <code>{escape(type(error).__name__)}</code>"
            f"{user_line}\n"
            f"📝 Xabar: <code>{escape(raw_message)}</code>\n\n"
            "Bir xil xato 5 daqiqa ichida qayta yuborilmaydi.",
        )
    except TelegramAPIError:
        logging.getLogger(__name__).exception("Admin error alert could not be delivered")
    return True


@router.message()
async def fallback(message: Message, admin_id: int):
    await message.answer("Kerakli bo‘limni tugmalar orqali tanlang:", reply_markup=main_menu(is_admin(message.from_user.id, admin_id)))
