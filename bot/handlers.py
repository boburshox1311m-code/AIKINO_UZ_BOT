from __future__ import annotations

import math
import re
import logging
from typing import Any, Awaitable, Callable

import asyncpg
from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .database import Database

router = Router()
PAGE_MOVIES = 8
PAGE_EPISODES = 10


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
    episode_number = State()
    episode_video = State()
    bulk_start_number = State()
    bulk_videos = State()
    rename_movie = State()
    renumber_episode = State()
    replace_video = State()


class SearchFlow(StatesGroup):
    query = State()


def main_menu(is_admin=False):
    rows = [
        [InlineKeyboardButton(text="🎬 Kinolar", callback_data="movies:0")],
        [InlineKeyboardButton(text="🔥 Yangi qismlar", callback_data="latest")],
        [InlineKeyboardButton(text="🔎 Kino qidirish", callback_data="search")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🔐 Admin panel", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")]])


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
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")],
    ])


async def safe_edit(call: CallbackQuery, text: str, markup=None):
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await call.message.answer(text, reply_markup=markup)
    await call.answer()


def is_admin(user_id: int, admin_id: int):
    return user_id == admin_id


async def episode_markup(db: Database, ep):
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
    rows.append([InlineKeyboardButton(text="🎬 Barcha qismlar", callback_data=f"movie:{ep['movie_id']}")])
    rows.append([InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_episode_message(message: Message, db: Database, episode_id: int):
    ep = await db.episode(episode_id)
    if not ep or not ep["file_id"]:
        return False
    caption = ep["caption"] or f"{ep['movie_emoji']} <b>{ep['movie_title']}</b> — {ep['episode_number']}-QISM"
    await message.answer_video(
        ep["file_id"],
        caption=caption,
        reply_markup=await episode_markup(db, ep),
        supports_streaming=True,
        protect_content=True,
    )
    return True


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, admin_id: int, db: Database):
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("ep_"):
        try:
            if await send_episode_message(message, db, int(parts[1][3:])):
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
        if not await send_episode_message(call.message, db, int(target[3:])):
            await call.message.answer("Video topilmadi.", reply_markup=main_menu())
        return
    await safe_edit(
        call,
        "✅ <b>Obuna tasdiqlandi!</b>\n\nKerakli bo‘limni tanlang:",
        main_menu(is_admin(call.from_user.id, admin_id)),
    )


async def movie_keyboard(db: Database, page: int, prefix="movie", back="home"):
    count = await db.movie_count()
    page = max(0, min(page, max(0, math.ceil(count / PAGE_MOVIES) - 1)))
    items = await db.movies(page * PAGE_MOVIES, PAGE_MOVIES)
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


async def episode_keyboard(db: Database, movie_id: int, page: int):
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
    b.row(InlineKeyboardButton(text="🎬 Kinolar", callback_data="movies:0"), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home"))
    return b.as_markup(), count


@router.callback_query(F.data.startswith("movie:"))
async def select_movie(call: CallbackQuery, db: Database):
    movie_id = int(call.data.split(":")[1])
    await render_movie(call, db, movie_id, 0)


@router.callback_query(F.data.startswith("moviepage:"))
async def movie_page(call: CallbackQuery, db: Database):
    _, movie_id, page = call.data.split(":")
    await render_movie(call, db, int(movie_id), int(page))


async def render_movie(call, db, movie_id, page):
    movie = await db.movie(movie_id)
    if not movie:
        return await safe_edit(call, "Kino topilmadi.", main_menu())
    kb, count = await episode_keyboard(db, movie_id, page)
    text = f"{movie['emoji']} <b>{movie['title']}</b>\n\nQismni tanlang:" if count else f"{movie['emoji']} <b>{movie['title']}</b>\n\nHozircha qismlar yo‘q."
    await safe_edit(call, text, kb)


@router.callback_query(F.data.startswith("ep:"))
async def send_episode(call: CallbackQuery, db: Database):
    ep = await db.episode(int(call.data.split(":")[1]))
    if not ep or not ep["file_id"]:
        return await call.answer("Video topilmadi.", show_alert=True)
    await send_episode_message(call.message, db, ep["id"])
    await call.answer()


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
    await state.clear()
    await message.answer(f"✅ <b>{movie['emoji']} {movie['title']}</b> qo‘shildi.", reply_markup=admin_menu())


async def admin_movie_picker(db: Database, action: str, back="admin"):
    items = await db.movies(0, 100)
    b = InlineKeyboardBuilder()
    for m in items:
        b.button(text=f"{m['emoji']} {m['title']}", callback_data=f"{action}:{m['id']}")
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
    await state.clear()
    await message.answer(f"✅ {data['episode_number']}-QISM videosi saqlandi va foydalanuvchilarga ochildi.", reply_markup=admin_menu())
    if channel_id:
        ep = await db.episode(data["episode_id"])
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
    except asyncpg.UniqueViolationError:
        return await message.answer("Bu nom band. Boshqa nom yozing:", reply_markup=cancel_kb())
    await state.clear()
    await message.answer("✅ Kino nomi o‘zgartirildi.", reply_markup=admin_menu())


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
    await db.delete_movie(int(call.data.rsplit(":", 1)[1]))
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
    await db.delete_episode(int(call.data.rsplit(":", 1)[1]))
    await safe_edit(call, "✅ Qism o‘chirildi.", admin_menu())


@router.message()
async def fallback(message: Message, admin_id: int):
    await message.answer("Kerakli bo‘limni tugmalar orqali tanlang:", reply_markup=main_menu(is_admin(message.from_user.id, admin_id)))
