from __future__ import annotations

import hashlib
import hmac
import html
import json
import time
from io import BytesIO
from urllib.parse import parse_qsl

from aiohttp import ClientSession, web
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice

from .database import Database
from .storage import R2Storage

POSTER_CACHE: dict[int, bytes] = {}


def _stream_signature(bot_token: str, episode_id: int, user_id: int, expires: int) -> str:
    payload = f"{episode_id}:{user_id}:{expires}".encode()
    return hmac.new(bot_token.encode(), payload, hashlib.sha256).hexdigest()


def _verify_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict | None:
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", "")
        auth_date = int(pairs.get("auth_date", "0"))
        if not received_hash or auth_date < int(time.time()) - max_age:
            return None
        data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        return json.loads(pairs.get("user", "{}"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _request_user(request: web.Request) -> dict | None:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    bot: Bot = request.app["bot"]
    return _verify_init_data(init_data, bot.token)


def _movie_json(movie) -> dict:
    views = int(movie["view_count"] or 0)
    if views >= 10000:
        badge = "TOP"
    elif views >= 5000:
        badge = "TREND"
    elif views >= 1000:
        badge = "HOT"
    else:
        badge = ""
    return {
        "id": int(movie["id"]),
        "title": movie["title"],
        "description": movie["description"] or "",
        "is_vip": bool(movie["is_vip"]),
        "views": views,
        "badge": badge,
        "episode_count": int(movie["episode_count"] or 0) if "episode_count" in movie.keys() else 0,
        "poster_url": (
            f"/app/poster/{movie['id']}?v={hashlib.md5(str(movie['poster_file_id']).encode()).hexdigest()[:8]}"
            if movie["poster_file_id"]
            else None
        ),
    }


async def app_page(request: web.Request) -> web.Response:
    return web.Response(text=MINI_APP_HTML, content_type="text/html")


async def api_catalog(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    try:
        offset = max(0, int(request.query.get("offset", "0")))
        limit = max(1, min(int(request.query.get("limit", "36")), 50))
    except ValueError:
        raise web.HTTPBadRequest()
    movies = await db.miniapp_movies(offset, limit)
    total = await db.miniapp_movie_count()
    payload = [_movie_json(movie) for movie in movies]
    return web.json_response({
        "movies": payload,
        "offset": offset,
        "limit": limit,
        "total": total,
        "has_more": offset + len(payload) < total,
    })


async def api_movie(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    bot: Bot = request.app["bot"]
    admin_id: int = request.app["admin_id"]
    storage: R2Storage = request.app["storage"]
    try:
        movie_id = int(request.match_info["movie_id"])
    except ValueError:
        raise web.HTTPBadRequest()

    movie = await db.movie_with_stats(movie_id)
    if not movie:
        raise web.HTTPNotFound()

    user = _request_user(request)
    user_id = int(user["id"]) if user else 0
    is_admin = user_id == admin_id
    if movie["is_vip"] and not is_admin:
        if not user_id or not await db.is_vip_user(user_id):
            return web.json_response({"error": "vip_required"}, status=403)

    episodes = await db.miniapp_episodes(movie_id, bool(movie["is_vip"]), 0, 200)
    data = _movie_json(movie)
    data["processing"] = bool(movie["is_vip"] and not episodes)

    resume = await db.movie_watch_progress(user_id, movie_id) if user_id else None
    if resume:
        data["resume"] = {
            "episode_id": int(resume["episode_id"]),
            "current_time": float(resume["position_seconds"] or 0),
            "duration": float(resume["duration"] or 0),
            "last_watched_at": resume["last_watched_at"].isoformat(),
        }
    else:
        data["resume"] = None

    # Telegram fallback links are signed for 6h. R2 links are created only when a
    # movie is opened and expire after 1h; VIP movies never fall back to an
    # unfinished Telegram/local file.
    expires = int(time.time()) + 6 * 60 * 60
    payload = []
    for ep in episodes:
        item = {
            "id": int(ep["id"]),
            "number": int(ep["episode_number"]),
            "storage_status": ep["storage_status"] if "storage_status" in ep.keys() else "pending",
        }
        if storage.enabled and "r2_key" in ep.keys() and ep["r2_key"]:
            item["stream_url"] = storage.presigned_get(ep["r2_key"], expires=3600)
            item["stream_source"] = "r2"
        elif not movie["is_vip"] and user_id:
            sig = _stream_signature(bot.token, int(ep["id"]), user_id, expires)
            item["stream_url"] = f"/app/stream/{ep['id']}?u={user_id}&e={expires}&s={sig}"
            item["stream_source"] = "telegram"
        else:
            item["stream_url"] = None
            item["stream_source"] = None
        payload.append(item)

    data["episodes"] = payload
    data["stream_locked"] = False
    return web.json_response(data)


async def api_me(request: web.Request) -> web.Response:
    user = _request_user(request)
    if not user:
        return web.json_response({"authenticated": False})
    db: Database = request.app["db"]
    user_id = int(user["id"])
    admin_id: int = request.app["admin_id"]
    await db.record_app_visit_once(user_id)
    language = await db.user_language(user_id)
    vip = await db.vip_user(user_id)
    progress = await db.watch_progress(user_id)
    favorites = await db.favorite_movies(user_id, 100)
    return web.json_response({
        "authenticated": True,
        "user": {
            "id": user_id,
            "first_name": user.get("first_name", ""),
            "last_name": user.get("last_name", ""),
            "username": user.get("username", ""),
        },
        "is_admin": user_id == admin_id,
        "language": language,
        "vip": bool(vip) or user_id == admin_id,
        "vip_expires_at": vip["expires_at"].isoformat() if vip else None,
        "favorite_ids": [int(movie["id"]) for movie in favorites],
        "continue": {
            "episode_id": int(progress["id"]),
            "movie_id": int(progress["movie_id"]),
            "movie_title": progress["movie_title"],
            "episode_number": int(progress["episode_number"]),
            "position_seconds": float(progress["position_seconds"] or 0),
            "duration_seconds": float(progress["duration_seconds"] or 0),
        } if progress else None,
    })


async def api_set_language(request: web.Request) -> web.Response:
    user = _request_user(request)
    if not user:
        raise web.HTTPUnauthorized()
    db: Database = request.app["db"]
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest()
    language = str(data.get("language", "")).lower()
    if language not in {"uz", "ru", "en"}:
        return web.json_response({"error": "unsupported_language"}, status=400)
    await db.set_user_language(int(user["id"]), language)
    return web.json_response({"ok": True, "language": language})


async def api_payment_info(request: web.Request) -> web.Response:
    user = _request_user(request)
    if not user:
        raise web.HTTPUnauthorized()
    db: Database = request.app["db"]
    user_id = int(user["id"])
    manual = await db.manual_payment_settings()
    stars_enabled = await db.stars_payments_enabled()
    plans = await db.stars_plans()
    accepted = await db.has_accepted_payment_terms(user_id)
    subscription = await db.active_subscription_payment(user_id)
    digits = "".join(ch for ch in manual["card_number"] if ch.isdigit())
    grouped = " ".join(digits[i:i + 4] for i in range(0, len(digits), 4))
    return web.json_response({
        "stars": {
            "enabled": stars_enabled,
            "plans": [{"days": d, "stars": s, "recurring": d == 30} for d, s in plans],
            "terms_accepted": accepted,
            "subscription_active": bool(subscription),
        },
        "manual": {
            "enabled": bool(manual["enabled"]),
            "card_number": grouped if manual["enabled"] else "",
            "card_holder": manual["card_holder"] if manual["enabled"] else "",
            "plans": [
                {"days": int(days), "price_uzs": int(price)}
                for days, price in manual["plans"]
            ],
        },
    })


async def api_accept_payment_terms(request: web.Request) -> web.Response:
    user = _request_user(request)
    if not user:
        raise web.HTTPUnauthorized()
    db: Database = request.app["db"]
    await db.accept_payment_terms(int(user["id"]))
    return web.json_response({"ok": True})


async def api_stars_invoice(request: web.Request) -> web.Response:
    user = _request_user(request)
    if not user:
        raise web.HTTPUnauthorized()
    db: Database = request.app["db"]
    bot: Bot = request.app["bot"]
    user_id = int(user["id"])
    if not await db.has_accepted_payment_terms(user_id):
        return web.json_response({"error": "terms_required"}, status=403)
    if not await db.stars_payments_enabled():
        return web.json_response({"error": "stars_disabled"}, status=503)
    try:
        days = int(request.match_info["days"])
    except ValueError:
        raise web.HTTPBadRequest()
    plans = await db.stars_plans()
    match = next(((d, s) for d, s in plans if d == days), None)
    if not match:
        return web.json_response({"error": "plan_not_found"}, status=404)
    _, stars = match
    kind = "sub" if days == 30 else "once"
    payload = f"vipstars:{user_id}:{days}:{stars}:{kind}"
    kwargs = dict(
        title=f"AIKINOUZ VIP — {days} kun",
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
    return web.json_response({
        "invoice_url": invoice_url,
        "days": days,
        "stars": stars,
        "recurring": kind == "sub",
    })


async def api_manual_receipt(request: web.Request) -> web.Response:
    user = _request_user(request)
    if not user:
        raise web.HTTPUnauthorized()
    db: Database = request.app["db"]
    bot: Bot = request.app["bot"]
    admin_id: int = request.app["admin_id"]
    settings = await db.manual_payment_settings()
    if not settings["enabled"]:
        return web.json_response({"error": "manual_disabled"}, status=503)

    selected_days = None
    filename = "receipt.jpg"
    content_type = ""
    chunks = []
    total = 0
    try:
        reader = await request.multipart()
        while True:
            field = await reader.next()
            if field is None:
                break
            if field.name == "vip_days":
                try:
                    selected_days = int((await field.text()).strip())
                except (TypeError, ValueError):
                    selected_days = None
            elif field.name == "receipt" and field.filename:
                filename = field.filename or "receipt.jpg"
                content_type = (field.headers.get("Content-Type") or "").lower()
                while True:
                    chunk = await field.read_chunk(size=256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 8 * 1024 * 1024:
                        return web.json_response({"error": "too_large"}, status=400)
                    chunks.append(chunk)
    except Exception:
        raise web.HTTPBadRequest()

    plans = [(int(days), int(price)) for days, price in settings["plans"]]
    match = next(((days, price) for days, price in plans if days == selected_days), None)
    if not match:
        return web.json_response({"error": "plan_required"}, status=400)
    vip_days, amount_uzs = match

    if not chunks:
        return web.json_response({"error": "receipt_required"}, status=400)
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        return web.json_response({"error": "image_only"}, status=400)
    data = b"".join(chunks)

    user_id = int(user["id"])
    state, payment = await db.create_manual_payment_request(
        user_id,
        amount_uzs,
        vip_days,
    )
    if state == "duplicate":
        return web.json_response({"error": "pending_exists"}, status=409)

    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"adm:payapprove:{payment['id']}"),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"adm:payreject:{payment['id']}"),
    ]])
    full_name = " ".join(
        x for x in [user.get("first_name", ""), user.get("last_name", "")] if x
    ).strip() or "Foydalanuvchi"
    if vip_days == 180:
        plan_label = "6 oy"
    elif vip_days == 365:
        plan_label = "1 yil"
    else:
        plan_label = f"{vip_days} kun"
    caption = (
        "💳 <b>Mini App — yangi karta to‘lovi</b>\n\n"
        f"👤 {html.escape(full_name)}\n"
        f"🆔 Telegram ID: <code>{user_id}</code>\n"
        f"📦 Paket: <b>{plan_label}</b>\n"
        f"💰 Summa: <b>{amount_uzs:,} so‘m</b>\n\n"
        "Bank ilovasida pul tushganini tekshirib, qaror bering."
    )
    try:
        sent = await bot.send_photo(
            admin_id,
            BufferedInputFile(data, filename=filename),
            caption=caption,
            reply_markup=markup,
        )
        await db.set_manual_payment_receipt(payment["id"], sent.photo[-1].file_id)
    except TelegramAPIError:
        await db.review_manual_payment(payment["id"], "rejected")
        return web.json_response({"error": "send_failed"}, status=502)
    return web.json_response({
        "ok": True,
        "payment_id": int(payment["id"]),
        "vip_days": vip_days,
        "amount_uzs": amount_uzs,
    })

async def api_toggle_favorite(request: web.Request) -> web.Response:
    user = _request_user(request)
    if not user:
        raise web.HTTPUnauthorized()
    db: Database = request.app["db"]
    try:
        movie_id = int(request.match_info["movie_id"])
    except ValueError:
        raise web.HTTPBadRequest()
    movie = await db.movie(movie_id)
    if not movie:
        raise web.HTTPNotFound()
    if movie["is_vip"] and not await db.is_vip_user(int(user["id"])):
        return web.json_response({"error": "vip_required"}, status=403)
    added = await db.toggle_movie_favorite(int(user["id"]), movie_id)
    return web.json_response({"favorite": added})


async def api_admin_stats(request: web.Request) -> web.Response:
    user = _request_user(request)
    if not user:
        raise web.HTTPUnauthorized()
    admin_id: int = request.app["admin_id"]
    if int(user["id"]) != admin_id:
        raise web.HTTPForbidden()
    db: Database = request.app["db"]
    stats = await db.app_statistics(admin_id)
    top_movies = await db.app_top_movies(admin_id, 5)
    vip_movies = await db.vip_movies_watch_stats(20)
    return web.json_response({
        "total_users": int(stats["total_users"] or 0),
        "total_sessions": int(stats["total_sessions"] or 0),
        "today_users": int(stats["today_users"] or 0),
        "today_sessions": int(stats["today_sessions"] or 0),
        "users_7d": int(stats["users_7d"] or 0),
        "users_30d": int(stats["users_30d"] or 0),
        "total_viewers": int(stats["total_viewers"] or 0),
        "total_views": int(stats["total_views"] or 0),
        "today_viewers": int(stats["today_viewers"] or 0),
        "today_views": int(stats["today_views"] or 0),
        "active_vips": int(stats["active_vips"] or 0),
        "top_movies": [{
            "id": int(movie["id"]),
            "title": movie["title"],
            "emoji": movie["emoji"],
            "views": int(movie["view_count"] or 0),
            "viewers": int(movie["unique_viewers"] or 0),
        } for movie in top_movies],
        "vip_movies": [{
            "id": int(movie["id"]),
            "title": movie["title"],
            "views": int(movie["views"] or 0),
            "unique_viewers": int(movie["unique_viewers"] or 0),
            "average_watch_time": float(movie["average_watch_time"] or 0),
            "completed_views": int(movie["completed_views"] or 0),
        } for movie in vip_movies],
    })


async def api_support(request: web.Request) -> web.Response:
    user = _request_user(request)
    if not user:
        raise web.HTTPUnauthorized()
    bot: Bot = request.app["bot"]
    admin_id: int = request.app["admin_id"]
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest()
    message = str(data.get("message", "")).strip()
    if len(message) < 3:
        return web.json_response({"error": "message_too_short"}, status=400)
    if len(message) > 2000:
        return web.json_response({"error": "message_too_long"}, status=400)
    full_name = " ".join(x for x in [user.get("first_name", ""), user.get("last_name", "")] if x).strip()
    username = f"@{user.get('username')}" if user.get("username") else "username yo‘q"
    safe_full_name = html.escape(full_name or "Foydalanuvchi")
    safe_username = html.escape(username)
    safe_message = html.escape(message)
    text = (
        "🛟 <b>AIKINOUZ SUPPORT</b>\n\n"
        f"👤 <b>{safe_full_name}</b>\n"
        f"🔗 {safe_username}\n"
        f"🆔 <code>{int(user.get('id'))}</code>\n\n"
        f"💬 <b>Xabar:</b>\n{safe_message}"
    )
    await bot.send_message(admin_id, text)
    return web.json_response({"ok": True})


async def api_watch_progress(request: web.Request) -> web.Response:
    user = _request_user(request)
    if not user:
        raise web.HTTPUnauthorized()
    db: Database = request.app["db"]
    admin_id: int = request.app["admin_id"]
    try:
        episode_id = int(request.match_info["episode_id"])
        data = await request.json()
        position = max(0.0, min(float(data.get("position", 0) or 0), 86400.0))
        duration = max(0.0, min(float(data.get("duration", 0) or 0), 86400.0))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest()
    ep = await db.episode(episode_id)
    if not ep or not ep["file_id"]:
        raise web.HTTPNotFound()
    user_id = int(user["id"])
    if ep["movie_is_vip"] and user_id != admin_id and not await db.is_vip_user(user_id):
        raise web.HTTPForbidden()
    await db.save_watch_progress(user_id, episode_id, position, duration)
    if str(data.get("event", "")) == "start":
        await db.record_episode_view_once(user_id, episode_id)
        await db.record_app_watch_once(user_id, episode_id)
    return web.json_response({"ok": True})


async def stream_episode(request: web.Request) -> web.StreamResponse:
    db: Database = request.app["db"]
    bot: Bot = request.app["bot"]
    admin_id: int = request.app["admin_id"]
    try:
        episode_id = int(request.match_info["episode_id"])
        user_id = int(request.query.get("u", "0"))
        expires = int(request.query.get("e", "0"))
        signature = request.query.get("s", "")
    except ValueError:
        raise web.HTTPForbidden()

    if not user_id or expires < int(time.time()):
        raise web.HTTPForbidden()
    expected = _stream_signature(bot.token, episode_id, user_id, expires)
    if not hmac.compare_digest(signature, expected):
        raise web.HTTPForbidden()

    ep = await db.episode(episode_id)
    if not ep or not ep["file_id"]:
        raise web.HTTPNotFound()
    if ep["movie_is_vip"] and user_id != admin_id and not await db.is_vip_user(user_id):
        raise web.HTTPForbidden()

    try:
        tg_file = await bot.get_file(ep["file_id"])
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception(
            "Mini App stream get_file failed: episode_id=%s user_id=%s error=%s",
            episode_id,
            user_id,
            exc,
        )
        raise web.HTTPBadGateway(text="Telegram video faylini ochib bo‘lmadi")

    local_path = str(tg_file.file_path or "")
    if local_path and __import__("os").path.isabs(local_path) and __import__("os").path.isfile(local_path):
        try:
            await db.save_watch_progress(user_id, episode_id)
            await db.record_episode_view(user_id, episode_id)
            await db.record_app_watch_once(user_id, episode_id)
        except Exception:
            pass
        return web.FileResponse(
            path=local_path,
            headers={
                "Content-Type": ep["mime_type"] or "video/mp4",
                "Cache-Control": "private, no-store",
            },
        )

    file_url = f"https://api.telegram.org/file/bot{bot.token}/{tg_file.file_path}"
    range_header = request.headers.get("Range")
    upstream_headers = {"Range": range_header} if range_header else {}

    try:
        async with ClientSession() as session:
            async with session.get(file_url, headers=upstream_headers) as upstream:
                if upstream.status not in (200, 206):
                    import logging
                    body = await upstream.text()
                    logging.getLogger(__name__).error(
                        "Mini App upstream stream failed: episode_id=%s status=%s body=%s",
                        episode_id,
                        upstream.status,
                        body[:300],
                    )
                    raise web.HTTPBadGateway(text="Video stream vaqtincha mavjud emas")
                headers = {
                    "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
                    "Accept-Ranges": upstream.headers.get("Accept-Ranges", "bytes"),
                    "Cache-Control": "private, no-store",
                }
                for key in ("Content-Length", "Content-Range"):
                    if key in upstream.headers:
                        headers[key] = upstream.headers[key]
                response = web.StreamResponse(status=upstream.status, headers=headers)
                await response.prepare(request)

                first_range = not range_header or range_header.startswith("bytes=0-")
                if first_range:
                    try:
                        await db.save_watch_progress(user_id, episode_id)
                        await db.record_episode_view(user_id, episode_id)
                        await db.record_app_watch_once(user_id, episode_id)
                    except Exception:
                        pass

                async for chunk in upstream.content.iter_chunked(256 * 1024):
                    await response.write(chunk)
                await response.write_eof()
                return response
    except web.HTTPException:
        raise
    except Exception:
        raise web.HTTPBadGateway(text="Video stream xatosi")


async def poster(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    bot: Bot = request.app["bot"]
    try:
        movie_id = int(request.match_info["movie_id"])
    except ValueError:
        raise web.HTTPBadRequest()
    if movie_id in POSTER_CACHE:
        return web.Response(body=POSTER_CACHE[movie_id], content_type="image/jpeg")
    movie = await db.movie(movie_id)
    if not movie or not movie["poster_file_id"]:
        raise web.HTTPNotFound()
    buf = BytesIO()
    try:
        tg_file = await bot.get_file(movie["poster_file_id"])
        await bot.download_file(tg_file.file_path, destination=buf)
    except Exception:
        raise web.HTTPNotFound()
    data = buf.getvalue()
    if not data:
        raise web.HTTPNotFound()
    if len(POSTER_CACHE) > 80:
        POSTER_CACHE.clear()
    POSTER_CACHE[movie_id] = data
    return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})


async def brand_logo(request: web.Request) -> web.Response:
    # AIKINOUZ official emblem: circular gold mark, stylised A and cinema film strip.
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<defs>
  <linearGradient id="gold" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#fff0a2"/>
    <stop offset=".22" stop-color="#f6ca5b"/>
    <stop offset=".52" stop-color="#a96713"/>
    <stop offset=".76" stop-color="#ffe88b"/>
    <stop offset="1" stop-color="#c3821d"/>
  </linearGradient>
  <linearGradient id="gold2" x1="0" y1="1" x2="1" y2="0">
    <stop offset="0" stop-color="#9c5c0b"/>
    <stop offset=".45" stop-color="#f7d66f"/>
    <stop offset="1" stop-color="#b16d11"/>
  </linearGradient>
  <filter id="glow"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
</defs>
<rect width="512" height="512" rx="72" fill="#070604"/>
<circle cx="256" cy="256" r="226" fill="#090704" stroke="url(#gold)" stroke-width="10"/>
<circle cx="256" cy="256" r="209" fill="none" stroke="#d6a13b" stroke-width="3"/>
<!-- stylised A -->
<path d="M143 332 L251 105 Q257 92 263 105 L369 332 H311 L279 260 H215 L184 332 Z
         M236 214 H259 L248 185 Z" fill="url(#gold)" stroke="#8d5209" stroke-width="3"/>
<!-- cinema film strip sweeping across A -->
<path d="M105 300 C173 285 230 248 287 208 C333 176 381 170 416 190
         L401 238 C365 218 331 222 294 246 C226 291 179 316 112 329 Z"
      fill="url(#gold2)" stroke="#e7bd58" stroke-width="3"/>
<g fill="#15100a" opacity=".95">
  <rect x="129" y="294" width="18" height="10" rx="2" transform="rotate(-12 138 299)"/>
  <rect x="160" y="282" width="18" height="10" rx="2" transform="rotate(-17 169 287)"/>
  <rect x="191" y="266" width="18" height="10" rx="2" transform="rotate(-23 200 271)"/>
  <rect x="223" y="246" width="18" height="10" rx="2" transform="rotate(-29 232 251)"/>
  <rect x="256" y="224" width="18" height="10" rx="2" transform="rotate(-33 265 229)"/>
  <rect x="290" y="205" width="18" height="10" rx="2" transform="rotate(-24 299 210)"/>
  <rect x="324" y="190" width="18" height="10" rx="2" transform="rotate(-15 333 195)"/>
  <rect x="358" y="188" width="18" height="10" rx="2" transform="rotate(2 367 193)"/>
  <rect x="387" y="199" width="18" height="10" rx="2" transform="rotate(14 396 204)"/>
  <rect x="132" y="315" width="18" height="10" rx="2" transform="rotate(-12 141 320)"/>
  <rect x="165" y="303" width="18" height="10" rx="2" transform="rotate(-17 174 308)"/>
  <rect x="198" y="286" width="18" height="10" rx="2" transform="rotate(-23 207 291)"/>
  <rect x="231" y="266" width="18" height="10" rx="2" transform="rotate(-29 240 271)"/>
  <rect x="264" y="244" width="18" height="10" rx="2" transform="rotate(-33 273 249)"/>
  <rect x="298" y="226" width="18" height="10" rx="2" transform="rotate(-24 307 231)"/>
  <rect x="333" y="211" width="18" height="10" rx="2" transform="rotate(-15 342 216)"/>
  <rect x="367" y="210" width="18" height="10" rx="2" transform="rotate(2 376 215)"/>
</g>
<text x="256" y="405" text-anchor="middle" font-family="Arial,Helvetica,sans-serif"
      font-size="56" font-weight="900" letter-spacing="6" fill="url(#gold)" filter="url(#glow)">AIKINOUZ</text>
</svg>"""
    return web.Response(text=svg, content_type="image/svg+xml", headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"})


def register_miniapp_routes(app: web.Application) -> None:
    app.router.add_get("/app/logo.svg", brand_logo)
    app.router.add_get("/app", app_page)
    app.router.add_get("/app/", app_page)
    app.router.add_get("/app/api/catalog", api_catalog)
    app.router.add_get("/app/api/movie/{movie_id}", api_movie)
    app.router.add_get("/app/api/me", api_me)
    app.router.add_post("/app/api/language", api_set_language)
    app.router.add_get("/app/api/payment-info", api_payment_info)
    app.router.add_get("/app/api/admin/stats", api_admin_stats)
    app.router.add_post("/app/api/payment-terms/accept", api_accept_payment_terms)
    app.router.add_post("/app/api/stars-invoice/{days}", api_stars_invoice)
    app.router.add_post("/app/api/manual-receipt", api_manual_receipt)
    app.router.add_post("/app/api/favorite/{movie_id}", api_toggle_favorite)
    app.router.add_post("/app/api/support", api_support)
    app.router.add_post("/app/api/watch/{episode_id}", api_watch_progress)
    app.router.add_get("/app/stream/{episode_id}", stream_episode)
    app.router.add_get("/app/poster/{movie_id}", poster)
    app.router.add_get("/favicon.ico", brand_logo)


MINI_APP_HTML = r"""<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#050505">
<link rel="icon" href="/app/logo.svg" type="image/svg+xml">
<title>AIKINOUZ</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#050505;--panel:#0d0d0d;--panel2:#121212;--gold:#f4c75c;--gold2:#b77a1d;--red:#d92631;--text:#fff;--muted:#989898;--line:#272117}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
body{min-height:100vh;background:
radial-gradient(circle at 100% 0,#6c421b2e,transparent 30%),
radial-gradient(circle at 0 40%,#5f0d1422,transparent 30%),
#050505}
button,input,textarea{font:inherit}
button{cursor:pointer}
.app{min-height:100vh;padding-top:calc(max(env(safe-area-inset-top),var(--tg-content-safe-area-inset-top,0px)) + 42px);padding-bottom:calc(84px + env(safe-area-inset-bottom))}
.top{position:sticky;top:0;z-index:30;background:#050505f3;backdrop-filter:blur(22px);border-bottom:1px solid #1f1a13;padding:10px 14px}
.brand{display:flex;align-items:center;justify-content:space-between;gap:10px}
.brandmark{display:flex;align-items:center;gap:10px}
.brandLogo{width:50px;height:50px;border-radius:50%;object-fit:contain;border:1px solid #7d5a20;background:radial-gradient(circle,#15100a 0%,#050505 72%);padding:2px;box-shadow:0 0 24px #d99d2d40,0 0 0 1px #2b1d0b inset}
.crown{width:42px;height:42px;border-radius:14px;background:linear-gradient(145deg,#ffe58a,#a96913);display:grid;place-items:center;color:#1b1102;font-size:23px;box-shadow:0 0 28px #d99d2d33}
.logo{font-weight:950;letter-spacing:1.1px;color:#f6ca63;font-size:20px}
.sub{font-size:10px;color:#a88b54;letter-spacing:1.8px}
.headRight{display:flex;align-items:center;gap:8px}
.avatar,.closeBtn{width:38px;height:38px;border-radius:12px;border:1px solid #3a2d18;background:#101010;color:#f4c75c;display:grid;place-items:center;font-weight:900}
.closeBtn{color:#ddd;font-size:21px}
.langBtn{height:38px;min-width:54px;padding:0 10px;border-radius:12px;border:1px solid #3a2d18;background:#101010;color:#f4c75c;font-weight:950}.langChoices{padding:12px 14px 24px;display:grid;gap:10px}.langChoice{width:100%;padding:16px;border-radius:16px;border:1px solid #44351d;background:#111;color:#fff;text-align:left;font-weight:900}.langChoice.active{border-color:#f4c75c;color:#f4c75c;background:#1b150b}

.hero{margin:14px 14px 10px;border:1px solid #57401c;border-radius:24px;min-height:305px;position:relative;overflow:hidden;background:#111 center/cover no-repeat;box-shadow:0 20px 60px #0009}
.heroShade{position:absolute;inset:0;background:linear-gradient(0deg,#050505f7 0%,#0505058c 48%,#05050522 78%)}
.heroContent{position:absolute;left:20px;right:20px;bottom:20px;z-index:2}
.eyebrow{font-size:11px;letter-spacing:1.8px;font-weight:900;color:#f2c355}
.hero h1{font-size:28px;line-height:1.02;margin:7px 0 7px;max-width:90%}
.heroMeta{font-size:12px;color:#d0d0d0;margin-bottom:9px}
.heroDesc{font-size:13px;color:#bebebe;line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.heroActions{display:flex;gap:8px;margin-top:14px}
.goldBtn,.darkBtn{border-radius:13px;padding:12px 15px;font-weight:900;border:0}
.goldBtn{background:linear-gradient(135deg,#ffe17b,#bd7a1b);color:#171003}
.darkBtn{background:#111c;color:#fff;border:1px solid #373024}

.section{padding:12px 14px 4px}
.sectionHead{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.sectionHead h2{font-size:19px;margin:0}.sectionHead button{border:0;background:none;color:#c99a3f;font-size:12px}
.row{display:flex;gap:11px;overflow-x:auto;padding-bottom:6px;scrollbar-width:none}.row::-webkit-scrollbar{display:none}
.card{width:144px;min-width:144px}
.poster{height:205px;border-radius:16px;overflow:hidden;background:#151515;position:relative;border:1px solid #2a241a;box-shadow:0 9px 24px #0008}
.poster img{width:100%;height:100%;object-fit:cover;display:block}
.posterFallback{height:100%;display:grid;place-items:center;font-size:36px;color:#dcb451;background:linear-gradient(145deg,#1f170c,#111)}
.badge{position:absolute;top:7px;left:7px;border-radius:8px;padding:5px 7px;font-size:9px;font-weight:950;background:var(--red);color:#fff}
.badge.vip{left:auto;right:7px;background:#e8b83e;color:#181003}
.cardTitle{font-size:13px;font-weight:850;margin-top:7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cardMeta{font-size:11px;color:#909090;margin-top:3px}

.page{display:none}.page.active{display:block}
.pageTop{display:flex;align-items:center;gap:10px;padding:14px 14px 8px}
.backBtn{width:38px;height:38px;border-radius:12px;border:1px solid #2d271d;background:#101010;color:#fff;font-size:23px}
.pageTitle{font-size:22px;font-weight:950}
.filterBar{display:flex;gap:8px;overflow-x:auto;padding:6px 14px 10px;scrollbar-width:none}.filterBar::-webkit-scrollbar{display:none}
.filterBtn{white-space:nowrap;border-radius:999px;padding:8px 12px;border:1px solid #3a3020;background:#0e0e0e;color:#aaa;font-size:12px;font-weight:800}
.filterBtn.active{background:linear-gradient(135deg,#f7d36b,#b77518);color:#171003;border-color:transparent}
.searchBox{margin:4px 14px 8px}.searchBox input{width:100%;padding:13px 14px;border-radius:14px;border:1px solid #2b2b2b;background:#0f0f0f;color:#fff}
.catalog{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;padding:8px 14px 24px}
.catalog .card{width:auto;min-width:0}.catalog .poster{height:246px}
.catalog .cardTitle{white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;line-height:1.2;min-height:31px}

.detailPoster{margin:4px 14px 0;height:440px;border-radius:22px;overflow:hidden;border:1px solid #372d1c;background:#111}
.detailPoster img{width:100%;height:100%;object-fit:cover}
.detailBody{padding:16px 14px 24px}.detailBody h1{font-size:28px;line-height:1.05;margin:0 0 8px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 14px}.chip{border:1px solid #4c3b1e;background:#15110b;color:#e7c164;border-radius:999px;padding:6px 9px;font-size:11px}
.desc{color:#c2c2c2;font-size:14px;line-height:1.55}.detailActions{display:grid;grid-template-columns:1.35fr .65fr;gap:8px;margin:16px 0}
.tabs{display:flex;gap:16px;border-bottom:1px solid #222;margin-top:8px}.tab{padding:10px 0;border:0;background:none;color:#8d8d8d;font-weight:800}.tab.active{color:#f3c85f;border-bottom:2px solid #f3c85f}
.episodes{display:grid;gap:8px;margin-top:12px}.episode{display:flex;align-items:center;justify-content:space-between;padding:13px;background:#0f0f0f;border:1px solid #242424;border-radius:14px}.episode button{border:1px solid #4d3a1d;background:#1a140b;color:#f2c45b;border-radius:10px;padding:8px 10px;font-weight:850}

.vipHero{margin:8px 14px 14px;border:1px solid #6b4e20;border-radius:24px;padding:24px;background:radial-gradient(circle at top right,#6d461a55,transparent 45%),#0d0d0d;text-align:center}
.vipHero .big{font-size:48px}.vipHero h1{margin:8px 0 5px;color:#f6ca61}.benefits{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:16px}.benefit{background:#111;border:1px solid #2e271d;border-radius:14px;padding:12px 7px;font-size:11px}.benefit b{display:block;font-size:22px;margin-bottom:5px}
.vipGate{margin:12px 14px 24px;border:1px solid #6b4e20;border-radius:22px;padding:22px 18px;text-align:center;background:linear-gradient(145deg,#181108,#0b0b0b);box-shadow:0 18px 50px #0008}.vipGate .vipLock{font-size:42px}.vipGate h2{margin:10px 0 7px;color:#f5ca62;font-size:21px}.vipGate p{margin:0;color:#b9b9b9;font-size:13px;line-height:1.55}

.profile{padding:10px 14px 24px}.profileCard{border:1px solid #49391e;background:linear-gradient(145deg,#15110b,#0c0c0c);border-radius:20px;padding:18px}.profileName{font-size:21px;font-weight:950}.status{font-size:12px;color:#efc054;margin-top:5px}
.profileMenu{margin-top:14px;display:grid;gap:8px}.statsWrap{padding:8px 14px 28px}.statsGrid{display:grid;grid-template-columns:repeat(2,1fr);gap:9px}.statCard{border:1px solid #49391e;background:linear-gradient(145deg,#15110b,#0b0b0b);border-radius:17px;padding:14px}.statValue{font-size:27px;font-weight:950;color:#f5ca62}.statLabel{font-size:11px;color:#aaa;margin-top:4px}.statsSection{margin-top:14px;border:1px solid #32291e;background:#0d0d0d;border-radius:18px;padding:15px}.statsSection h3{margin:0 0 10px;color:#f5ca62}.topMovieRow{display:flex;justify-content:space-between;gap:10px;padding:9px 0;border-bottom:1px solid #222;font-size:12px}.topMovieRow:last-child{border-bottom:0}.statsRefresh{width:100%;margin-top:12px;border:0;border-radius:13px;padding:12px;background:linear-gradient(135deg,#ffe17a,#b87518);font-weight:950;color:#171003}.profileItem{width:100%;display:flex;justify-content:space-between;align-items:center;padding:14px 15px;border-radius:14px;border:1px solid #27231c;background:#0e0e0e;color:#fff;text-align:left;font-weight:800}.profileItem.gold{color:#f4ca61;border-color:#57411d}.payPage{padding:8px 14px 28px}.payCard{border:1px solid #49391e;background:linear-gradient(145deg,#15110b,#0b0b0b);border-radius:20px;padding:17px;margin-bottom:12px}.payTitle{font-size:20px;font-weight:950;color:#f5ca62}.payDesc{font-size:12px;color:#aaa;line-height:1.5;margin-top:6px}.starPlans{display:grid;gap:8px;margin-top:13px}.starPlan{width:100%;border:1px solid #60471e;background:#161108;color:#f7cc64;border-radius:14px;padding:13px;text-align:left;font-weight:900;display:flex;justify-content:space-between;align-items:center}.manualPlan.active{outline:2px solid #f5ca62;background:#211708}.termsBox{margin-top:12px;border:1px solid #333;background:#0e0e0e;border-radius:13px;padding:12px;font-size:11px;color:#bbb;line-height:1.5}.payPrimary{width:100%;border:0;border-radius:13px;padding:13px;background:linear-gradient(135deg,#ffe17a,#b87518);font-weight:950;color:#171003;margin-top:10px}.cardNumber{font-size:20px;letter-spacing:1.2px;font-weight:900;color:#ffe080;margin-top:13px}.receiptInput{width:100%;margin-top:12px;background:#0d0d0d;border:1px solid #342b20;border-radius:12px;padding:11px;color:#ddd}.payStatus{font-size:12px;color:#aaa;margin-top:9px;line-height:1.45}
.supportCard{margin:8px 14px 20px;border:1px solid #5a431e;background:linear-gradient(145deg,#17120b,#0b0b0b);border-radius:22px;padding:18px}.publicCompany{margin:18px 14px 22px;border:1px solid #6b4e20;background:radial-gradient(circle at top right,#6d461a55,transparent 45%),#0b0b0b;border-radius:22px;padding:18px}.publicCompanyHead{display:flex;align-items:center;gap:13px}.publicCompanyHead img{width:76px;height:76px;border-radius:50%;object-fit:contain;padding:3px;background:radial-gradient(circle,#171009,#050505 74%);border:1px solid #8a6326;box-shadow:0 0 24px #d99d2d38}.publicCompanyTitle{font-size:22px;font-weight:950;color:#f5ca62}.publicCompanySub{font-size:10px;letter-spacing:1.7px;color:#b89b61;margin-top:2px}.publicCompanyInfo{display:grid;gap:7px;margin-top:14px;color:#d8d8d8;font-size:12px;line-height:1.45}.publicCompanyInfo b{color:#f1c55f}.supportTitle{font-size:24px;font-weight:950;color:#f5ca62}.supportMeta{display:grid;gap:9px;margin-top:14px;color:#d2d2d2;font-size:13px;line-height:1.45}.supportMeta b{color:#f0c45d}.presidentRow{display:flex;align-items:center;gap:10px}.presidentMiniLogo{width:44px;height:44px;flex:none;border-radius:50%;object-fit:contain;padding:2px;border:1px solid #8a6227;background:radial-gradient(circle,#171009,#050505 75%);box-shadow:0 0 18px #d99d2d38}.presidentText{min-width:0}.presidentLabel{display:block;color:#f0c45d;font-weight:900;font-size:12px;letter-spacing:.2px}.presidentName{display:block;margin-top:2px;color:#eee;font-size:12px;line-height:1.35}.supportForm textarea{width:100%;min-height:130px;background:#0e0e0e;color:#fff;border:1px solid #332d24;border-radius:14px;padding:13px;margin-top:8px}.supportSend{width:100%;margin-top:10px;border:0;border-radius:13px;padding:13px;background:linear-gradient(135deg,#ffe17a,#b87518);font-weight:950;color:#171003}.supportNote{font-size:11px;color:#8f8f8f;margin-top:8px}
.empty{padding:28px 14px;color:#888;text-align:center}

.inlinePlayerWrap{display:none;position:sticky;top:72px;z-index:25;background:#000;border-bottom:1px solid #2f2618;box-shadow:0 18px 38px #000b}
.inlinePlayerWrap.active{display:block}
.inlinePlayerMount{width:100%;background:#000}
.inlinePlayerMeta{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:9px 12px;background:linear-gradient(180deg,#0d0d0d,#080808);border-top:1px solid #ffffff0d}
.inlinePlayerMetaText{min-width:0}.inlinePlayerMetaTitle{font-size:13px;font-weight:950;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.inlinePlayerMetaSub{font-size:10px;color:#9f9f9f;margin-top:2px}
.inlinePlayerBadge{flex:none;border:1px solid #6f5121;color:#f4c75c;background:#1a140a;border-radius:999px;padding:6px 9px;font-size:10px;font-weight:900}
.videoStage.inlineStage{position:relative;inset:auto;width:100%;aspect-ratio:16/9;max-height:62vh;min-height:190px;background:#000}
.videoStage.inlineStage .playerTop{padding:10px 10px 8px}
.videoStage.inlineStage .playerClose{width:34px;height:34px;font-size:19px}
.videoStage.inlineStage .playerMovieTitle{font-size:13px}.videoStage.inlineStage .playerEpisodeTitle{font-size:9px}
.videoStage.inlineStage .centerCluster{gap:16px}
.videoStage.inlineStage .circleControl.playMain{width:62px;height:62px;font-size:27px}
.videoStage.inlineStage .circleControl.nextMain{width:42px;height:42px;font-size:16px}
.videoStage.inlineStage .playerBottom{padding:0 8px 2px}
.videoStage.inlineStage .compactTime{font-size:10px}
.videoStage.inlineStage .playerActionBtn{width:30px;height:30px;font-size:14px}
.videoStage.inlineStage .skipFeedback.left{left:8%}.videoStage.inlineStage .skipFeedback.right{right:8%}
.videoStage.inlineStage .playlistDrawer{max-height:74%}
.recommendBlock{padding:4px 14px 28px}.recommendHead{display:flex;align-items:center;justify-content:space-between;margin:18px 0 10px}.recommendHead h2{margin:0;font-size:19px;color:#f5ca62}.recommendGrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.recommendGrid .card{width:auto;min-width:0}
@media (orientation:landscape){
  .videoStage.inlineStage{aspect-ratio:16/9;max-height:78vh}
}
.playerOverlay{position:fixed;inset:0;z-index:1000;background:#000;display:none;overflow:hidden}
.playerOverlay.active{display:block}
.videoStage{position:absolute;inset:0;background:#000;display:flex;align-items:center;justify-content:center}
.videoStage video{width:100%;height:100%;object-fit:contain;background:#000}
.playerControls{position:absolute;inset:0;background:linear-gradient(180deg,#000a 0%,transparent 28%,transparent 64%,#000d 100%);opacity:1;transition:opacity .2s ease}
.playerControls.hiddenControls{opacity:0;pointer-events:none}
.playerTop{position:absolute;left:0;right:0;top:0;display:flex;align-items:center;gap:11px;padding:calc(96px + env(safe-area-inset-top)) 16px 10px}
.playerClose{width:40px;height:40px;border:1px solid #ffffff22;border-radius:50%;background:#1119;color:#fff;font-size:22px;backdrop-filter:blur(14px);box-shadow:0 8px 26px #0007}
.playerHeading{min-width:0;text-shadow:0 2px 12px #000}.playerMovieTitle{font-size:16px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.playerEpisodeTitle{font-size:11px;color:#d7d7d7;margin-top:2px}
.playerCenter{position:absolute;inset:0;pointer-events:none}
.centerCluster{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);display:flex;align-items:center;gap:24px;pointer-events:auto}
.circleControl{border:0;background:#1010108f;color:#fff;display:grid;place-items:center;backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);box-shadow:0 8px 26px #0007;transition:transform .12s ease,background .12s ease,opacity .15s ease}
.circleControl:active{transform:scale(.92);background:#202020b8}
.circleControl.playMain{width:82px;height:82px;border-radius:50%;font-size:34px;background:#111a}
.circleControl.nextMain{width:52px;height:52px;border-radius:50%;font-size:20px;background:#1118}
.circleControl.nextMain:disabled{opacity:.28;pointer-events:none}
.hiddenPlayerControl{display:none!important}
.skipFeedback{position:absolute;top:50%;transform:translateY(-50%) scale(.88);min-width:74px;padding:13px 15px;border-radius:999px;background:#101010a8;color:#fff;font-size:18px;font-weight:950;text-align:center;opacity:0;pointer-events:none;backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);transition:opacity .12s ease,transform .12s ease}
.skipFeedback.left{left:11%}.skipFeedback.right{right:11%}
.skipFeedback.show{opacity:1;transform:translateY(-50%) scale(1)}
.playerBottom{position:absolute;left:0;right:0;bottom:0;padding:0 10px calc(4px + env(safe-area-inset-bottom));pointer-events:auto;background:linear-gradient(180deg,transparent,#000a)}
.playerUtilityRow{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:0 4px 3px}
.compactTime{display:flex;align-items:center;gap:4px;font-size:11px;color:#fff;text-shadow:0 1px 6px #000;white-space:nowrap}
.timeSep{opacity:.65}
.progress{display:block;width:100%;height:16px;margin:0;accent-color:#ff1f35;cursor:pointer;background:transparent}
.progress::-webkit-slider-runnable-track{height:3px;background:linear-gradient(to right,#ff1f35 0,#ff1f35 var(--progress-pct,0%),#ffffff73 var(--progress-pct,0%),#ffffff73 100%);border-radius:999px}
.progress::-webkit-slider-thumb{-webkit-appearance:none;width:12px;height:12px;border-radius:50%;background:#ff1f35;margin-top:-4.5px;box-shadow:0 0 0 2px #fff0}
.progress::-moz-range-track{height:3px;background:#ffffff73;border-radius:999px}.progress::-moz-range-progress{height:3px;background:#ff1f35;border-radius:999px}.progress::-moz-range-thumb{width:12px;height:12px;border:0;border-radius:50%;background:#ff1f35}
.playerActions{display:flex;align-items:center;justify-content:flex-end;gap:5px;margin:0}
.playerActionBtn{border:0;background:#1117;color:#fff;border-radius:50%;width:34px;height:34px;padding:0;display:grid;place-items:center;font-weight:900;font-size:16px;backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);box-shadow:none}
.playerActionBtn:active{transform:scale(.91);background:#222a}
.playerActionBtn.iconOnly{font-size:17px}
.playerActionBtn.fullscreenIcon{font-size:20px}
.hiddenVolume{display:none}
.volumeWrap{display:flex;align-items:center;gap:6px}.volumeRange{width:92px;accent-color:#f4c75c}
.loadMore{display:block;margin:12px auto 28px;border:1px solid #5c451f;background:#151007;color:#f4c75c;border-radius:14px;padding:12px 18px;font-weight:900}
.vipStatsRow{display:grid;grid-template-columns:1fr auto;gap:8px;padding:10px 0;border-bottom:1px solid #24201a}.vipStatsRow:last-child{border-bottom:0}.vipStatsMeta{font-size:11px;color:#aaa;line-height:1.5;text-align:right}
.playlistDrawer{position:absolute;left:0;right:0;bottom:0;z-index:3;max-height:58%;background:#0b0b0bf8;border-top:1px solid #3b3021;border-radius:20px 20px 0 0;transform:translateY(105%);transition:transform .25s ease;overflow:auto;padding:14px 14px calc(18px + env(safe-area-inset-bottom))}
.playlistDrawer.open{transform:translateY(0)}
.drawerHead{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}.drawerHead h3{margin:0;color:#f4c75c}.drawerClose{border:0;background:#191919;color:#fff;width:34px;height:34px;border-radius:10px}
.playlistItems{display:grid;gap:8px}.playlistItem{display:flex;justify-content:space-between;align-items:center;border:1px solid #29251d;background:#111;color:#fff;border-radius:13px;padding:12px;text-align:left}.playlistItem.active{border-color:#b77a1d;background:#1c160d;color:#f5ca61}
.playerError{position:absolute;inset:0;display:none;place-items:center;text-align:center;padding:30px;color:#fff;background:#050505}.playerError.show{display:grid}
@media (orientation:landscape){
  .playerTop{padding-top:calc(54px + env(safe-area-inset-top))}
  .playerBottom{padding-bottom:calc(8px + env(safe-area-inset-bottom))}
  .playlistDrawer{left:auto;top:0;right:0;bottom:0;width:min(380px,42vw);max-height:none;border-radius:20px 0 0 20px;border-top:0;border-left:1px solid #3b3021;transform:translateX(105%)}
  .playlistDrawer.open{transform:translateX(0)}
}

.bottom{position:fixed;left:0;right:0;bottom:0;z-index:40;display:grid;grid-template-columns:repeat(5,1fr);background:#070707f4;backdrop-filter:blur(20px);border-top:1px solid #201b13;padding:7px 6px calc(9px + env(safe-area-inset-bottom))}
.navBtn{border:0;background:none;color:#858585;font-size:10px;font-weight:800;padding:5px 1px}.navBtn b{display:block;font-size:21px;margin-bottom:2px}.navBtn.active{color:#f4c75c}
.hidden{display:none!important}
</style>
</head>
<body>
<div class="app">
<header class="top">
  <div class="brand">
    <div class="brandmark"><img class="brandLogo" src="/app/logo.svg" alt="AIKINOUZ logo"><div><div class="logo">AIKINOUZ</div><div class="sub">PREMIUM CINEMA</div></div></div>
    <div class="headRight"><button id="langBtn" class="langBtn">🌐 UZ</button><div id="avatar" class="avatar">A</div><button id="closeApp" class="closeBtn">×</button></div>
  </div>
</header>

<main id="home" class="page active">
  <section id="hero" class="hero">
    <div class="heroShade"></div>
    <div class="heroContent">
      <div class="eyebrow">AIKINOUZ PREMIERE</div>
      <h1 id="heroTitle">Premium kino olami</h1>
      <div id="heroMeta" class="heroMeta">Yangi va mashhur kinolar bir joyda</div>
      <div id="heroDesc" class="heroDesc">AIKINOUZ bilan sevimli seriallaringizni tomosha qiling.</div>
      <div class="heroActions"><button id="heroWatch" class="goldBtn">▶ Tomosha qilish</button><button id="heroCatalog" class="darkBtn">Katalog</button></div>
    </div>
  </section>
  <section class="section"><div class="sectionHead"><h2>🔥 Trend kinolar</h2><button data-open="catalog" data-filter="trend">Barchasi ›</button></div><div id="trendRow" class="row"></div></section>
  <section class="section"><div class="sectionHead"><h2>🆕 Yangi kinolar</h2><button data-open="catalog" data-filter="new">Barchasi ›</button></div><div id="newRow" class="row"></div></section>
  <section class="section"><div class="sectionHead"><h2>💎 VIP tanlov</h2><button data-open="vip">Ko‘rish ›</button></div><div id="vipRow" class="row"></div></section>
  <section class="publicCompany">
    <div class="publicCompanyHead"><img src="/app/logo.svg" alt="AIKINOUZ"><div><div class="publicCompanyTitle">AIKINOUZ</div><div class="publicCompanySub">PREMIUM KINO PLATFORMASI</div></div></div>
    <div class="publicCompanyInfo">
      <div class="presidentRow"><img class="presidentMiniLogo" src="/app/logo.svg" alt="AIKINOUZ"><div class="presidentText"><span class="presidentLabel">AIKINOUZ KOMPANIYA PRESIDENT:</span><span class="presidentName">BOBURMIRZO GAZIEV MAKHAMMATTOLIBJON O‘G‘LI</span></div></div>
      <div>📧 <b>Rasmiy aloqa:</b> boburshox1311m@gmail.com</div>
      <div>🚀 <b>Versiya:</b> v1.0</div>
      <div>© 2026 AIKINOUZ. All rights reserved.</div>
    </div>
  </section>
</main>

<main id="language" class="page">
  <div class="pageTop"><button class="backBtn" data-open="home">‹</button><div id="languagePageTitle" class="pageTitle">Til / Язык / Language</div></div>
  <div class="langChoices">
    <button class="langChoice" data-language="uz">🇺🇿 O‘zbekcha</button>
    <button class="langChoice" data-language="ru">🇷🇺 Русский</button>
    <button class="langChoice" data-language="en">🇬🇧 English</button>
  </div>
</main>

<main id="catalog" class="page">
  <div class="pageTop"><button class="backBtn" data-open="home">‹</button><div class="pageTitle">Kinolar</div></div>
  <div class="filterBar">
    <button class="filterBtn active" data-filter="all">Barchasi</button>
    <button class="filterBtn" data-filter="trend">🔥 Trend</button>
    <button class="filterBtn" data-filter="new">🆕 Yangi</button>
    <button class="filterBtn" data-filter="vip">💎 VIP</button>
  </div>
  <div class="searchBox"><input id="searchInput" placeholder="Kino yoki serial qidiring..."></div>
  <div id="catalogGrid" class="catalog"></div>
  <button id="loadMoreCatalog" class="loadMore" style="display:none">Yana yuklash</button>
</main>

<main id="detail" class="page">
  <div class="pageTop"><button class="backBtn" data-open="catalog">‹</button><div class="pageTitle">Kino sahifasi</div></div>
  <div id="inlinePlayerWrap" class="inlinePlayerWrap">
    <div id="inlinePlayerMount" class="inlinePlayerMount"></div>
    <div class="inlinePlayerMeta">
      <div class="inlinePlayerMetaText"><div id="inlinePlayerMetaTitle" class="inlinePlayerMetaTitle">AIKINOUZ</div><div id="inlinePlayerMetaSub" class="inlinePlayerMetaSub">Premium player</div></div>
      <div class="inlinePlayerBadge">AIKINOUZ PLAYER</div>
    </div>
  </div>
  <div id="detailContent"></div>
</main>

<main id="vip" class="page">
  <div class="pageTop"><button class="backBtn" data-open="home">‹</button><div class="pageTitle">AIKINOUZ VIP</div></div>
  <section class="vipHero">
    <div class="big">♛</div><h1>AIKINOUZ VIP</h1><div class="sub">PREMIUM KOLLEKSIYA</div>
    <div class="benefits"><div class="benefit"><b>💎</b>Eksklyuziv</div><div class="benefit"><b>🎬</b>Premium kino</div><div class="benefit"><b>⚡</b>Tez kirish</div></div>
  </section>
  <section id="vipGate" class="vipGate" style="display:none">
    <div class="vipLock">🔒</div>
    <h2>VIP kinolar uchun obuna kerak</h2>
    <p>AIKINOUZ VIP bo‘limidagi premium kinolarni ko‘rish uchun VIP obuna sotib oling.</p>
    <button class="payPrimary" data-open="payments">⭐ VIP OBUNA SOTIB OLISH</button>
  </section>
  <div id="vipGrid" class="catalog"></div>
</main>

<main id="search" class="page">
  <div class="pageTop"><button class="backBtn" data-open="home">‹</button><div class="pageTitle">Qidiruv</div></div>
  <div class="searchBox"><input id="searchOnly" placeholder="Kino yoki serial nomini yozing..."></div>
  <div id="searchGrid" class="catalog"></div>
</main>

<main id="profile" class="page">
  <div class="pageTop"><button class="backBtn" data-open="home">‹</button><div class="pageTitle">Profil</div></div>
  <div class="profile">
    <div id="profileCard" class="profileCard"></div>
    <div class="profileMenu">
      <button class="profileItem" id="continueBtn"><span>▶ Davom ettirish</span><span>›</span></button>
      <button class="profileItem gold" data-open="payments"><span>⭐ VIP / To‘lov</span><span>›</span></button>
      <button class="profileItem" id="favoritesBtn"><span>♡ Sevimlilar</span><span>›</span></button>
      <button class="profileItem gold" id="adminStatsBtn" data-open="appstats" style="display:none"><span>📊 APP STATISTIKA</span><span>›</span></button>
      <button class="profileItem gold" data-open="support"><span>🛟 AIKINOUZ SUPPORT</span><span>›</span></button>
    </div>
    <div class="sectionHead" style="margin-top:20px"><h2>❤️ Sevimlilar</h2></div>
    <div id="favoritesGrid" class="catalog" style="padding:0"></div>
  </div>
</main>

<main id="appstats" class="page">
  <div class="pageTop"><button class="backBtn" data-open="profile">‹</button><div class="pageTitle">APP STATISTIKA</div></div>
  <div class="statsWrap">
    <div id="statsGrid" class="statsGrid"><div class="empty">Statistika yuklanmoqda...</div></div>
    <section class="statsSection">
      <h3>🔥 TOP kinolar</h3>
      <div id="topMoviesStats"><div class="payStatus">Yuklanmoqda...</div></div>
    </section>
    <section class="statsSection">
      <h3>💎 VIP kino statistikasi</h3>
      <div id="vipMoviesStats"><div class="payStatus">Yuklanmoqda...</div></div>
    </section>
    <button id="statsRefresh" class="statsRefresh">🔄 Yangilash</button>
    <div class="payStatus">Hisob London vaqti bo‘yicha. App kirishlari 30 daqiqalik sessiya sifatida sanaladi.</div>
  </div>
</main>

<main id="payments" class="page">
  <div class="pageTop"><button class="backBtn" data-open="profile">‹</button><div class="pageTitle">VIP / To‘lov</div></div>
  <div class="payPage">
    <section class="payCard">
      <div class="payTitle">⭐ Telegram Stars orqali VIP</div>
      <div class="payDesc">To‘lov Telegram ichida amalga oshadi. 30 kunlik paket avtomatik yangilanadigan obuna.</div>
      <div id="starsTerms" class="termsBox" style="display:none">
        To‘lovni bosish orqali VIP xizmatidan foydalanish shartlariga rozilik bildirasiz. Stars to‘lovlari Telegram orqali amalga oshiriladi; 30 kunlik paket avtomatik yangilanadi va bot orqali bekor qilinishi mumkin.
        <button id="acceptTermsBtn" class="payPrimary">✅ Shartlarga roziman</button>
      </div>
      <div id="starPlans" class="starPlans"><div class="payStatus">Paketlar yuklanmoqda...</div></div>
      <div id="starsStatus" class="payStatus"></div>
    </section>
    <section class="payCard">
      <div class="payTitle">💳 Karta orqali VIP to‘lov</div>
      <div id="manualPaymentBox"><div class="payStatus">Karta ma’lumoti yuklanmoqda...</div></div>
      <input id="receiptInput" class="receiptInput" type="file" accept="image/jpeg,image/png,image/webp">
      <button id="sendReceiptBtn" class="payPrimary">📤 Chek rasmini yuborish</button>
      <div id="receiptStatus" class="payStatus">Chek sizning Telegram ID’ingiz bilan adminga yuboriladi.</div>
    </section>
  </div>
</main>

<main id="support" class="page">
  <div class="pageTop"><button class="backBtn" data-open="profile">‹</button><div class="pageTitle">Support</div></div>
  <section class="supportCard">
    <div class="publicCompanyHead"><img src="/app/logo.svg" alt="AIKINOUZ"><div><div class="supportTitle">AIKINOUZ</div><div class="sub">PREMIUM KINO PLATFORMASI</div></div></div>
    <div class="supportMeta">
      <div>🏢 <b>Brend / loyiha:</b> AIKINOUZ</div>
      <div class="presidentRow"><img class="presidentMiniLogo" src="/app/logo.svg" alt="AIKINOUZ"><div class="presidentText"><span class="presidentLabel">AIKINOUZ KOMPANIYA PRESIDENT:</span><span class="presidentName">BOBURMIRZO GAZIEV MAKHAMMATTOLIBJON O‘G‘LI</span></div></div>
      <div>📧 <b>Rasmiy aloqa:</b> boburshox1311m@gmail.com</div>
      <div>🧩 <b>Platforma:</b> AIKINOUZ / AIKINO_UZ_BOT</div>
      <div>🚀 <b>Versiya:</b> v1.0</div>
      <div>© 2026 AIKINOUZ. All rights reserved.</div>
      <div><b>Project owner / author:</b><br>BOBURMIRZO GAZIEV MAKHAMMATTOLIBJON O‘G‘LI</div>
    </div>
    <div class="supportForm"><h3>💬 Adminga yozish</h3><textarea id="supportMessage" maxlength="2000" placeholder="Savol, muammo yoki taklifingizni yozing..."></textarea><button id="supportSend" class="supportSend">📨 XABARNI YUBORISH</button><div id="supportStatus" class="supportNote">Xabaringiz AIKINOUZ adminiga yuboriladi.</div></div>
  </section>
</main>

<div id="playerOverlay" class="playerOverlay">
  <div id="videoStage" class="videoStage">
    <video id="playerVideo" playsinline preload="metadata"></video>
    <div id="playerError" class="playerError"><div><div style="font-size:42px">⚠️</div><h3>Video stream ochilmadi</h3><p id="playerErrorText">Bu video Telegram stream limitidan katta bo‘lishi mumkin.</p></div></div>
    <div id="playerControls" class="playerControls">
      <div class="playerTop">
        <button id="playerClose" class="playerClose">×</button>
        <div class="playerHeading"><div id="playerMovieTitle" class="playerMovieTitle">AIKINOUZ</div><div id="playerEpisodeTitle" class="playerEpisodeTitle">1-qism</div></div>
      </div>
      <div class="playerCenter">
        <div id="skipFeedbackLeft" class="skipFeedback left">↶ 10</div>
        <div id="skipFeedbackRight" class="skipFeedback right">10 ↷</div>
        <div class="centerCluster">
          <button id="playPause" class="circleControl playMain" aria-label="Play/Pause">▶</button>
          <button id="nextEpisode" class="circleControl nextMain" aria-label="Keyingi qism">▶|</button>
        </div>
        <button id="prevEpisode" class="hiddenPlayerControl" aria-hidden="true" tabindex="-1"></button>
        <button id="back10" class="hiddenPlayerControl" aria-hidden="true" tabindex="-1"></button>
        <button id="forward10" class="hiddenPlayerControl" aria-hidden="true" tabindex="-1"></button>
      </div>
      <div class="playerBottom">
        <div class="playerUtilityRow">
          <div class="compactTime"><span id="currentTime">0:00</span><span class="timeSep">/</span><span id="durationTime">0:00</span></div>
          <div class="playerActions">
            <button id="playlistToggle" class="playerActionBtn iconOnly" aria-label="Qismlar">☰</button>
            <button id="muteToggle" class="playerActionBtn iconOnly" aria-label="Ovoz">🔊</button>
            <input id="volumeRange" class="volumeRange hiddenVolume" type="range" min="0" max="1" step="0.05" value="1" aria-label="Volume">
            <button id="playerFullscreen" class="playerActionBtn iconOnly fullscreenIcon" aria-label="To‘liq ekran">⛶</button>
          </div>
        </div>
        <input id="progressBar" class="progress" type="range" min="0" max="1000" value="0" aria-label="Video progress">
      </div>
    </div>
    <div id="playlistDrawer" class="playlistDrawer">
      <div class="drawerHead"><h3>🎞 Qismlar</h3><button id="playlistClose" class="drawerClose">×</button></div>
      <div id="playlistItems" class="playlistItems"></div>
    </div>
  </div>
</div>

<nav class="bottom">
  <button class="navBtn active" data-nav="home"><b>⌂</b>Bosh sahifa</button>
  <button class="navBtn" data-nav="catalog"><b>▦</b>Kinolar</button>
  <button class="navBtn" data-nav="search"><b>⌕</b>Qidiruv</button>
  <button class="navBtn" data-nav="vip"><b>♛</b>VIP</button>
  <button class="navBtn" data-nav="profile"><b>●</b>Profil</button>
</nav>
</div>

<script>
(function(){
  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if(tg){
    try{tg.ready()}catch(e){}
    try{tg.expand()}catch(e){}
    try{tg.setHeaderColor('#050505')}catch(e){}
    try{tg.setBackgroundColor('#050505')}catch(e){}
    try{if(typeof tg.requestFullscreen==='function') tg.requestFullscreen()}catch(e){}
  }

  var initData = tg && tg.initData ? tg.initData : '';
  var movies = [];
  var me = {authenticated:false,favorite_ids:[]};
  var appLang='uz';
  var I18N={
    uz:{
      home:'Bosh sahifa',catalog:'Kinolar',search:'Qidiruv',vip:'VIP',profile:'Profil',
      watch:'▶ Tomosha qilish',catalogBtn:'Katalog',trend:'🔥 Trend kinolar',newMovies:'🆕 Yangi kinolar',vipPick:'💎 VIP tanlov',
      all:'Barchasi ›',view:'Ko‘rish ›',allFilter:'Barchasi',newFilter:'🆕 Yangi',searchPh:'Kino yoki serial qidiring...',
      moviePage:'Kino sahifasi',vipCollection:'PREMIUM KOLLEKSIYA',exclusive:'Eksklyuziv',premiumMovie:'Premium kino',fast:'Tez kirish',
      vipNeed:'VIP kinolar uchun obuna kerak',vipNeedDesc:'AIKINOUZ VIP bo‘limidagi premium kinolarni ko‘rish uchun VIP obuna sotib oling.',
      buyVip:'⭐ VIP OBUNA SOTIB OLISH',continue:'▶ Davom ettirish',payments:'⭐ VIP / To‘lov',favorites:'♡ Sevimlilar',
      support:'🛟 AIKINOUZ SUPPORT',paymentTitle:'VIP / To‘lov',starsTitle:'⭐ Telegram Stars orqali VIP',
      starsDesc:'To‘lov Telegram ichida amalga oshadi. 30 kunlik paket avtomatik yangilanadigan obuna.',
      manualTitle:'💳 Karta orqali VIP to‘lov',sendReceipt:'📤 Chek rasmini yuborish',
      receiptHint:'Chek sizning Telegram ID’ingiz bilan adminga yuboriladi.',supportTitle:'Support',
      supportSend:'📨 XABARNI YUBORISH',supportHint:'Xabaringiz AIKINOUZ adminiga yuboriladi.',
      standard:'✨ STANDARD',vipActive:'💎 VIP ACTIVE',noFav:'Hozircha sevimli kinolar yo‘q.',
      noVipMovies:'VIP kinolar hozircha qo‘shilmagan.',noMovies:'Kino topilmadi.',episodes:'qism',views:'ko‘rish',
      watchNow:'▶ Tomosha',choosePlan:'VIP paketini tanlang:',cardOwner:'Karta egasi:',receiptInstruction:'Tanlangan paket summasini kartaga o‘tkazing, so‘ng chek rasmini yuboring.',
      days:'kun',months6:'6 oy',year1:'1 yil',auto:'Avtomatik',termsAccept:'✅ Shartlarga roziman',
      stats:'📊 APP STATISTIKA',languageTitle:'Til / Язык / Language',
      termsText:'To‘lovni bosish orqali VIP xizmatidan foydalanish shartlariga rozilik bildirasiz. Stars to‘lovlari Telegram orqali amalga oshiriladi; 30 kunlik paket avtomatik yangilanadi va bot orqali bekor qilinishi mumkin.',
      loadingPlans:'Paketlar yuklanmoqda...',loadingCard:'Karta ma’lumoti yuklanmoqda...',telegramOnly:'VIP to‘lov Telegram Mini App ichida ishlaydi.',telegramLogin:'Telegram orqali kiring.',
      starsOff:'Stars to‘lovi hozircha o‘chiq.',manualOff:'Karta orqali to‘lov hozircha o‘chiq.',noPlans:'Stars paketlari mavjud emas.',
      paymentPreparing:'⏳ To‘lov oynasi tayyorlanmoqda...',termsFirst:'Avval to‘lov shartlariga rozilik bering.',paymentOpenFail:'To‘lov oynasi ochilmadi.',
      paymentPaid:'✅ To‘lov qabul qilindi. VIP faollashtirilmoqda...',paymentCancelled:'To‘lov bekor qilindi.',paymentFailed:'To‘lov amalga oshmadi.',
      chooseReceipt:'Avval chek rasmini tanlang.',chooseVipPlan:'Avval VIP paketini tanlang.',receiptTooLarge:'Chek rasmi 8 MB dan kichik bo‘lsin.',
      receiptSending:'Chek adminga yuborilmoqda...',receiptSent:'✅ Chek adminga yuborildi. Tasdiqlangach VIP avtomatik faollashadi.',
      pendingReceipt:'⏳ Oldingi chekingiz hali admin tomonidan tekshirilmoqda.',receiptFail:'Chek yuborilmadi. Rasmni tekshirib qayta urinib ko‘ring.',
      messageShort:'Xabarni biroz to‘liqroq yozing.',messageSending:'Xabar yuborilmoqda...',messageSent:'✅ Xabaringiz adminga yuborildi.',messageFail:'Xabar yuborilmadi. Qayta urinib ko‘ring.',loadMore:'Yana yuklash'
    },
    ru:{
      home:'Главная',catalog:'Фильмы',search:'Поиск',vip:'VIP',profile:'Профиль',
      watch:'▶ Смотреть',catalogBtn:'Каталог',trend:'🔥 В тренде',newMovies:'🆕 Новые фильмы',vipPick:'💎 VIP подборка',
      all:'Все ›',view:'Смотреть ›',allFilter:'Все',newFilter:'🆕 Новые',searchPh:'Найти фильм или сериал...',
      moviePage:'Страница фильма',vipCollection:'ПРЕМИАЛЬНАЯ КОЛЛЕКЦИЯ',exclusive:'Эксклюзив',premiumMovie:'Премиум кино',fast:'Быстрый доступ',
      vipNeed:'Для VIP-фильмов нужна подписка',vipNeedDesc:'Оформите VIP-подписку, чтобы смотреть премиальные фильмы AIKINOUZ.',
      buyVip:'⭐ КУПИТЬ VIP',continue:'▶ Продолжить просмотр',payments:'⭐ VIP / Оплата',favorites:'♡ Избранное',
      support:'🛟 AIKINOUZ SUPPORT',paymentTitle:'VIP / Оплата',starsTitle:'⭐ VIP через Telegram Stars',
      starsDesc:'Оплата проходит внутри Telegram. Пакет на 30 дней продлевается автоматически.',
      manualTitle:'💳 VIP оплата картой',sendReceipt:'📤 Отправить чек',
      receiptHint:'Чек будет отправлен администратору вместе с вашим Telegram ID.',supportTitle:'Поддержка',
      supportSend:'📨 ОТПРАВИТЬ СООБЩЕНИЕ',supportHint:'Сообщение будет отправлено администратору AIKINOUZ.',
      standard:'✨ STANDARD',vipActive:'💎 VIP ACTIVE',noFav:'Избранных фильмов пока нет.',
      noVipMovies:'VIP-фильмов пока нет.',noMovies:'Фильм не найден.',episodes:'серий',views:'просмотров',
      watchNow:'▶ Смотреть',choosePlan:'Выберите VIP-пакет:',cardOwner:'Владелец карты:',receiptInstruction:'Переведите сумму выбранного пакета на карту, затем отправьте фото чека.',
      days:'дней',months6:'6 месяцев',year1:'1 год',auto:'Автопродление',termsAccept:'✅ Я согласен с условиями',
      stats:'📊 СТАТИСТИКА APP',languageTitle:'Язык',
      termsText:'Нажимая оплату, вы соглашаетесь с условиями VIP-сервиса. Оплата Stars проходит через Telegram; 30-дневный пакет продлевается автоматически и может быть отменён через бот.',
      loadingPlans:'Пакеты загружаются...',loadingCard:'Данные карты загружаются...',telegramOnly:'VIP-оплата работает внутри Telegram Mini App.',telegramLogin:'Откройте через Telegram.',
      starsOff:'Оплата Stars временно отключена.',manualOff:'Оплата картой временно отключена.',noPlans:'Пакеты Stars недоступны.',
      paymentPreparing:'⏳ Подготавливаем окно оплаты...',termsFirst:'Сначала примите условия оплаты.',paymentOpenFail:'Не удалось открыть оплату.',
      paymentPaid:'✅ Оплата принята. VIP активируется...',paymentCancelled:'Оплата отменена.',paymentFailed:'Оплата не прошла.',
      chooseReceipt:'Сначала выберите фото чека.',chooseVipPlan:'Сначала выберите VIP-пакет.',receiptTooLarge:'Фото чека должно быть меньше 8 МБ.',
      receiptSending:'Чек отправляется администратору...',receiptSent:'✅ Чек отправлен. После подтверждения VIP активируется автоматически.',
      pendingReceipt:'⏳ Предыдущий чек ещё проверяется администратором.',receiptFail:'Не удалось отправить чек. Проверьте изображение и попробуйте снова.',
      messageShort:'Напишите сообщение подробнее.',messageSending:'Сообщение отправляется...',messageSent:'✅ Сообщение отправлено администратору.',messageFail:'Не удалось отправить сообщение. Попробуйте ещё раз.',loadMore:'Загрузить ещё'
    },
    en:{
      home:'Home',catalog:'Movies',search:'Search',vip:'VIP',profile:'Profile',
      watch:'▶ Watch',catalogBtn:'Catalog',trend:'🔥 Trending',newMovies:'🆕 New movies',vipPick:'💎 VIP picks',
      all:'View all ›',view:'View ›',allFilter:'All',newFilter:'🆕 New',searchPh:'Search movies or series...',
      moviePage:'Movie page',vipCollection:'PREMIUM COLLECTION',exclusive:'Exclusive',premiumMovie:'Premium movies',fast:'Fast access',
      vipNeed:'VIP subscription required',vipNeedDesc:'Buy a VIP subscription to watch premium movies in AIKINOUZ VIP.',
      buyVip:'⭐ BUY VIP',continue:'▶ Continue watching',payments:'⭐ VIP / Payment',favorites:'♡ Favorites',
      support:'🛟 AIKINOUZ SUPPORT',paymentTitle:'VIP / Payment',starsTitle:'⭐ VIP with Telegram Stars',
      starsDesc:'Payment is completed inside Telegram. The 30-day plan renews automatically.',
      manualTitle:'💳 VIP card payment',sendReceipt:'📤 Send receipt',
      receiptHint:'Your receipt will be sent to the admin with your Telegram ID.',supportTitle:'Support',
      supportSend:'📨 SEND MESSAGE',supportHint:'Your message will be sent to the AIKINOUZ admin.',
      standard:'✨ STANDARD',vipActive:'💎 VIP ACTIVE',noFav:'No favorite movies yet.',
      noVipMovies:'No VIP movies yet.',noMovies:'Movie not found.',episodes:'episodes',views:'views',
      watchNow:'▶ Watch',choosePlan:'Choose a VIP plan:',cardOwner:'Card holder:',receiptInstruction:'Transfer the selected plan amount to the card, then upload the receipt.',
      days:'days',months6:'6 months',year1:'1 year',auto:'Auto-renew',termsAccept:'✅ I agree to the terms',
      stats:'📊 APP STATISTICS',languageTitle:'Language',
      termsText:'By proceeding with payment, you agree to the VIP service terms. Stars payments are processed by Telegram; the 30-day plan renews automatically and can be cancelled through the bot.',
      loadingPlans:'Loading plans...',loadingCard:'Loading card details...',telegramOnly:'VIP payment works inside Telegram Mini App.',telegramLogin:'Open through Telegram.',
      starsOff:'Stars payments are currently disabled.',manualOff:'Card payments are currently disabled.',noPlans:'No Stars plans are available.',
      paymentPreparing:'⏳ Preparing payment window...',termsFirst:'Please accept the payment terms first.',paymentOpenFail:'Could not open payment.',
      paymentPaid:'✅ Payment received. Activating VIP...',paymentCancelled:'Payment cancelled.',paymentFailed:'Payment failed.',
      chooseReceipt:'Choose a receipt image first.',chooseVipPlan:'Choose a VIP plan first.',receiptTooLarge:'Receipt image must be under 8 MB.',
      receiptSending:'Sending receipt to admin...',receiptSent:'✅ Receipt sent. VIP will activate automatically after approval.',
      pendingReceipt:'⏳ Your previous receipt is still under review.',receiptFail:'Could not send the receipt. Check the image and try again.',
      messageShort:'Please write a little more detail.',messageSending:'Sending message...',messageSent:'✅ Your message was sent to the admin.',messageFail:'Could not send the message. Try again.',loadMore:'Load more'
    }
  };
  function tr(k){return (I18N[appLang]&&I18N[appLang][k])||I18N.uz[k]||k}

  var catalogFilter = 'all';
  var catalogOffset = 0;
  var catalogPageSize = 36;
  var catalogHasMore = false;
  var featuredId = 0;
  var currentMovieData = null;
  var currentEpisodeIndex = -1;
  var playerHideTimer = null;
  var lastProgressSave = 0;
  var resumeAppliedEpisodeId = 0;

  function applyLanguage(lang){
    appLang=(lang==='ru'||lang==='en')?lang:'uz';
    document.documentElement.lang=appLang;
    document.getElementById('langBtn').textContent='🌐 '+appLang.toUpperCase();
    document.querySelectorAll('.langChoice').forEach(function(x){x.classList.toggle('active',x.getAttribute('data-language')===appLang)});
    var lp=document.getElementById('languagePageTitle'); if(lp)lp.textContent=tr('languageTitle');

    var nav=document.querySelectorAll('.navBtn');
    if(nav[0])nav[0].innerHTML='<b>⌂</b>'+tr('home');
    if(nav[1])nav[1].innerHTML='<b>▦</b>'+tr('catalog');
    if(nav[2])nav[2].innerHTML='<b>⌕</b>'+tr('search');
    if(nav[3])nav[3].innerHTML='<b>♛</b>'+tr('vip');
    if(nav[4])nav[4].innerHTML='<b>●</b>'+tr('profile');

    document.getElementById('heroWatch').textContent=tr('watch');
    document.getElementById('heroCatalog').textContent=tr('catalogBtn');
    var sh=document.querySelectorAll('#home .sectionHead');
    if(sh[0]){sh[0].querySelector('h2').textContent=tr('trend');sh[0].querySelector('button').textContent=tr('all')}
    if(sh[1]){sh[1].querySelector('h2').textContent=tr('newMovies');sh[1].querySelector('button').textContent=tr('all')}
    if(sh[2]){sh[2].querySelector('h2').textContent=tr('vipPick');sh[2].querySelector('button').textContent=tr('view')}

    var catalogTitle=document.querySelector('#catalog .pageTitle');if(catalogTitle)catalogTitle.textContent=tr('catalog');
    var filters=document.querySelectorAll('#catalog .filterBtn');
    if(filters[0])filters[0].textContent=tr('allFilter');
    if(filters[1])filters[1].textContent='🔥 '+(appLang==='ru'?'Тренды':appLang==='en'?'Trending':'Trend');
    if(filters[2])filters[2].textContent=tr('newFilter');
    if(filters[3])filters[3].textContent='💎 VIP';
    document.getElementById('searchInput').placeholder=tr('searchPh');
    document.getElementById('searchOnly').placeholder=tr('searchPh');
    var detailTitle=document.querySelector('#detail .pageTitle');if(detailTitle)detailTitle.textContent=tr('moviePage');
    var searchTitle=document.querySelector('#search .pageTitle');if(searchTitle)searchTitle.textContent=tr('search');
    var profileTitle=document.querySelector('#profile .pageTitle');if(profileTitle)profileTitle.textContent=tr('profile');

    var vipSub=document.querySelector('#vip .vipHero .sub');if(vipSub)vipSub.textContent=tr('vipCollection');
    var benefits=document.querySelectorAll('#vip .benefit');
    if(benefits[0])benefits[0].innerHTML='<b>💎</b>'+tr('exclusive');
    if(benefits[1])benefits[1].innerHTML='<b>🎬</b>'+tr('premiumMovie');
    if(benefits[2])benefits[2].innerHTML='<b>⚡</b>'+tr('fast');
    var gate=document.getElementById('vipGate');
    if(gate){gate.querySelector('h2').textContent=tr('vipNeed');gate.querySelector('p').textContent=tr('vipNeedDesc');gate.querySelector('button').textContent=tr('buyVip')}

    var menu=document.querySelectorAll('#profile .profileItem');
    if(menu[0])menu[0].querySelector('span').textContent=tr('continue');
    if(menu[1])menu[1].querySelector('span').textContent=tr('payments');
    if(menu[2])menu[2].querySelector('span').textContent=tr('favorites');
    if(menu[3]&&menu[3].id==='adminStatsBtn')menu[3].querySelector('span').textContent=tr('stats');
    var supportIndex=menu.length-1;if(menu[supportIndex])menu[supportIndex].querySelector('span').textContent=tr('support');

    var pt=document.querySelector('#payments .pageTitle');if(pt)pt.textContent=tr('paymentTitle');
    var payTitles=document.querySelectorAll('#payments .payTitle');
    if(payTitles[0])payTitles[0].textContent=tr('starsTitle');
    if(payTitles[1])payTitles[1].textContent=tr('manualTitle');
    var payDesc=document.querySelector('#payments .payCard .payDesc');if(payDesc)payDesc.textContent=tr('starsDesc');
    document.getElementById('sendReceiptBtn').textContent=tr('sendReceipt');
    document.getElementById('receiptStatus').textContent=tr('receiptHint');
    document.getElementById('acceptTermsBtn').textContent=tr('termsAccept');
    var termsBox=document.getElementById('starsTerms');
    if(termsBox)termsBox.innerHTML=tr('termsText')+'<button id="acceptTermsBtn" class="payPrimary">'+tr('termsAccept')+'</button>';

    var st=document.querySelector('#support .pageTitle');if(st)st.textContent=tr('supportTitle');
    document.getElementById('supportSend').textContent=tr('supportSend');
    document.getElementById('supportStatus').textContent=tr('supportHint');
    var lm=document.getElementById('loadMoreCatalog');if(lm)lm.textContent=tr('loadMore');
    renderHome();renderCatalog();renderSearch();renderProfile();renderVipAccess();
  }

  function changeLanguage(lang){
    if(lang!=='uz'&&lang!=='ru'&&lang!=='en')return;
    if(!initData){applyLanguage(lang);show('home');return}
    api('/app/api/language',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({language:lang})})
      .then(function(r){if(r.ok){me.language=lang;applyLanguage(lang);show('home')}})
      .catch(function(){});
  }

  function api(url,opt){
    opt = opt || {};
    opt.headers = opt.headers || {};
    opt.headers['X-Telegram-Init-Data'] = initData;
    return fetch(url,opt).then(function(r){
      return r.json().then(function(j){ if(!r.ok) j._http=r.status; return j; });
    });
  }

  function esc(s){
    return String(s||'').replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]});
  }
  function fmt(n){return Number(n||0).toLocaleString('en-GB')}
  function badge(m){
    var out='';
    if(m.badge) out += '<span class="badge">'+esc(m.badge)+'</span>';
    if(m.is_vip) out += '<span class="badge vip">VIP</span>';
    return out;
  }
  function card(m){
    var poster=m.poster_url?'<img src="'+m.poster_url+'" loading="lazy" alt="">':'<div class="posterFallback">🎬</div>';
    return '<div class="card movieCard" data-movie="'+m.id+'"><div class="poster">'+poster+badge(m)+'</div><div class="cardTitle">'+esc(m.title)+'</div><div class="cardMeta">👁 '+fmt(m.views)+' '+tr('views')+' · '+m.episode_count+' '+tr('episodes')+'</div></div>';
  }

  function show(id){
    if(id!=='detail' && playerIsInline && inlinePlayerWrap.classList.contains('active')) closePlayer();
    document.querySelectorAll('.page').forEach(function(x){x.classList.remove('active')});
    var el=document.getElementById(id); if(el) el.classList.add('active');
    document.querySelectorAll('.navBtn').forEach(function(x){x.classList.toggle('active',x.getAttribute('data-nav')===id)});
    window.scrollTo(0,0);
    if(id==='profile') renderProfile();
    if(id==='search') renderSearch();
    if(id==='vip') renderVipAccess();
    if(id==='appstats') loadAdminStats();
  }

  function setFilter(f){
    catalogFilter=f||'all';
    document.querySelectorAll('.filterBtn').forEach(function(x){x.classList.toggle('active',x.getAttribute('data-filter')===catalogFilter)});
    renderCatalog();
  }

  function filtered(source,filter,q){
    var list=source.slice();
    if(filter==='trend') list=list.filter(function(m){return m.views>=1000}).sort(function(a,b){return b.views-a.views});
    if(filter==='vip') list=list.filter(function(m){return m.is_vip});
    if(filter==='new') list=list.slice();
    if(q){q=q.toLowerCase();list=list.filter(function(m){return m.title.toLowerCase().indexOf(q)>=0})}
    return list;
  }

  function renderVipAccess(){
    var gate=document.getElementById('vipGate');
    var grid=document.getElementById('vipGrid');
    var vipMovies=movies.filter(function(m){return m.is_vip});
    if(me&&me.authenticated&&me.vip){
      gate.style.display='none';
      grid.style.display='grid';
      grid.innerHTML=vipMovies.length?vipMovies.map(card).join(''):'<div class="empty">'+tr('noVipMovies')+'</div>';
    }else{
      grid.style.display='none';
      gate.style.display='block';
    }
  }

  function renderHome(){
    var trend=filtered(movies,'trend','').slice(0,8);
    var normal=movies.filter(function(m){return !m.is_vip}).slice(0,8);
    var vip=movies.filter(function(m){return m.is_vip}).slice(0,8);
    document.getElementById('trendRow').innerHTML=(trend.length?trend:normal).map(card).join('');
    document.getElementById('newRow').innerHTML=normal.map(card).join('');
    document.getElementById('vipRow').innerHTML=vip.length?vip.map(card).join(''):'<div class="empty">VIP kinolar hozircha yo‘q.</div>';
    renderVipAccess();

    var f=(trend[0]||normal[0]||movies[0]);
    if(f){
      featuredId=f.id;
      var hero=document.getElementById('hero');
      if(f.poster_url) hero.style.backgroundImage='url("'+f.poster_url+'")';
      document.getElementById('heroTitle').textContent=f.title;
      document.getElementById('heroMeta').textContent='👁 '+fmt(f.views)+' '+tr('views')+' · 🎞 '+f.episode_count+' '+tr('episodes')+(f.badge?' · 🔥 '+f.badge:'')+(f.is_vip?' · 💎 VIP':'');
      document.getElementById('heroDesc').textContent=f.description||'AIKINOUZ premium kino kolleksiyasi.';
    }
  }

  function renderCatalog(){
    var q=document.getElementById('searchInput').value.trim();
    var list=filtered(movies,catalogFilter,q);
    document.getElementById('catalogGrid').innerHTML=list.length?list.map(card).join(''):'<div class="empty">'+tr('noMovies')+'</div>';
    var more=document.getElementById('loadMoreCatalog');
    if(more)more.style.display=(catalogHasMore&&!q)?'block':'none';
  }

  function loadMoreCatalog(){
    if(!catalogHasMore)return;
    var btn=document.getElementById('loadMoreCatalog');
    if(btn){btn.disabled=true;btn.textContent='…'}
    api('/app/api/catalog?offset='+catalogOffset+'&limit='+catalogPageSize).then(function(r){
      var incoming=r.movies||[];
      var seen={};movies.forEach(function(m){seen[m.id]=true});
      incoming.forEach(function(m){if(!seen[m.id]){movies.push(m);seen[m.id]=true}});
      catalogOffset+=incoming.length;
      catalogHasMore=!!r.has_more;
      renderHome();renderCatalog();renderSearch();renderVipAccess();
    }).finally(function(){if(btn){btn.disabled=false;btn.textContent=tr('loadMore')}});
  }
  function renderSearch(){
    var q=document.getElementById('searchOnly').value.trim();
    var list=filtered(movies,'all',q);
    document.getElementById('searchGrid').innerHTML=list.length?list.map(card).join(''):'<div class="empty">'+tr('noMovies')+'</div>';
  }

  function openMovie(id){
    show('detail');
    document.getElementById('detailContent').innerHTML='<div class="empty">Yuklanmoqda...</div>';
    api('/app/api/movie/'+id).then(function(m){
      if(m.error==='vip_required'){
        show('payments');
        loadPayments();
        return;
      }
      if(m._http){throw new Error('movie')}
      var fav=me.favorite_ids && me.favorite_ids.indexOf(m.id)>=0;
      var poster=m.poster_url?'<img src="'+m.poster_url+'" alt="">':'<div class="posterFallback">🎬</div>';
      var eps=(m.episodes||[]).map(function(e,i){var epLabel=appLang==='ru'?e.number+'-СЕРИЯ':(appLang==='en'?'EPISODE '+e.number:e.number+'-QISM');return '<div class="episode"><span><b>'+epLabel+'</b></span><button class="epOpen" data-index="'+i+'">'+tr('watchNow')+'</button></div>'}).join('');
      document.getElementById('detailContent').innerHTML=
        '<div class="detailPoster">'+poster+'</div>'+
        '<div class="detailBody"><h1>'+esc(m.title)+'</h1>'+
        '<div class="chips"><span class="chip">👁 '+fmt(m.views)+' ko‘rish</span><span class="chip">🎞 '+m.episode_count+' qism</span>'+(m.badge?'<span class="chip">🔥 '+esc(m.badge)+'</span>':'')+(m.is_vip?'<span class="chip">💎 VIP</span>':'')+'</div>'+
        '<div class="desc">'+esc(m.description||'AIKINOUZ premium kino kolleksiyasi.')+'</div>'+
        '<div class="detailActions"><button id="watchFirst" class="goldBtn">▶ Tomosha qilish</button><button id="favMovie" class="darkBtn">'+(fav?'♥ Sevimlida':'♡ Sevimlilar')+'</button></div>'+
        '<div class="tabs"><button class="tab active">Qismlar</button><button class="tab">Tavsif</button></div>'+
        '<div class="episodes">'+(eps||'<div class="empty">Qismlar hozircha yo‘q.</div>')+'</div></div>'+
        '<section class="recommendBlock"><div class="recommendHead"><h2>'+(appLang==='ru'?'Рекомендуем':appLang==='en'?'Recommended for you':'Sizga tavsiya')+'</h2></div>'+
        '<div id="recommendGrid" class="recommendGrid"></div></section>';

      currentMovieData=m;
      var recs=movies.filter(function(x){
        if(x.id===m.id)return false;
        if(m.is_vip)return !!x.is_vip;
        return !x.is_vip;
      }).slice(0,8);
      var recGrid=document.getElementById('recommendGrid');
      if(recGrid)recGrid.innerHTML=recs.length?recs.map(card).join(''):'<div class="empty">'+tr('noMovies')+'</div>';
      var first=(m.episodes||[])[0];
      var w=document.getElementById('watchFirst'); if(w) w.onclick=function(){if(first) openPlayer(m,0)};
      var fv=document.getElementById('favMovie'); if(fv) fv.onclick=function(){toggleFavorite(m.id)};
      document.querySelectorAll('.epOpen').forEach(function(b){b.onclick=function(){openPlayer(m,Number(b.getAttribute('data-index')))}});
    }).catch(function(){document.getElementById('detailContent').innerHTML='<div class="empty">Kino ma’lumotini yuklab bo‘lmadi.</div>'});
  }

  var playerOverlay=document.getElementById('playerOverlay');
  var playerVideo=document.getElementById('playerVideo');
  var playerControls=document.getElementById('playerControls');
  var playlistDrawer=document.getElementById('playlistDrawer');
  var playerError=document.getElementById('playerError');
  var videoStage=document.getElementById('videoStage');
  var inlinePlayerWrap=document.getElementById('inlinePlayerWrap');
  var inlinePlayerMount=document.getElementById('inlinePlayerMount');
  var playerHome=playerOverlay;
  var playerIsInline=false;
  var stageTapTimer=null;
  var lastTapAt=0;
  var lastTapSide='';
  var accumulatedSkip=0;
  var skipFeedbackTimer=null;

  function timeText(sec){
    sec=Math.max(0,Math.floor(Number(sec)||0));
    var h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60),s=sec%60;
    return h>0?h+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0'):m+':'+String(s).padStart(2,'0');
  }
  function sendWatchProgress(force,eventName){
    if(!initData||!currentMovieData||currentEpisodeIndex<0)return;
    var ep=(currentMovieData.episodes||[])[currentEpisodeIndex];
    if(!ep)return;
    var now=Date.now();
    if(!force && now-lastProgressSave<10000)return;
    lastProgressSave=now;
    api('/app/api/watch/'+ep.id,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        position:Number(playerVideo.currentTime||0),
        duration:Number(playerVideo.duration||0),
        event:eventName||'progress'
      }),
      keepalive:true
    }).catch(function(){});
  }

  function showPlayerControls(autoHide){
    playerControls.classList.remove('hiddenControls');
    clearTimeout(playerHideTimer);
    if(autoHide && !playerVideo.paused){
      playerHideTimer=setTimeout(function(){
        if(!playlistDrawer.classList.contains('open')) playerControls.classList.add('hiddenControls');
      },2600);
    }
  }
  function hidePlayerControls(){
    clearTimeout(playerHideTimer);
    if(!playerVideo.paused && !playlistDrawer.classList.contains('open'))playerControls.classList.add('hiddenControls');
  }
  function flashSkip(side,total){
    var el=document.getElementById(side==='left'?'skipFeedbackLeft':'skipFeedbackRight');
    if(!el)return;
    el.textContent=side==='left'?'↶ '+total:total+' ↷';
    el.classList.add('show');
    clearTimeout(skipFeedbackTimer);
    skipFeedbackTimer=setTimeout(function(){el.classList.remove('show')},650);
  }
  function seekByDoubleTap(side){
    var now=Date.now();
    if(lastTapSide===side && now-lastTapAt<1300)accumulatedSkip+=10;
    else accumulatedSkip=10;
    lastTapSide=side;
    lastTapAt=now;
    if(side==='left')playerVideo.currentTime=Math.max(0,(playerVideo.currentTime||0)-10);
    else playerVideo.currentTime=Math.min(playerVideo.duration||Infinity,(playerVideo.currentTime||0)+10);
    flashSkip(side,accumulatedSkip);
    showPlayerControls(true);
  }
  function renderPlaylist(){
    if(!currentMovieData)return;
    document.getElementById('playlistItems').innerHTML=(currentMovieData.episodes||[]).map(function(e,i){
      return '<button class="playlistItem '+(i===currentEpisodeIndex?'active':'')+'" data-play-index="'+i+'"><span><b>'+e.number+'-QISM</b></span><span>'+(i===currentEpisodeIndex?'▶ Hozir':'Ochish')+'</span></button>';
    }).join('');
  }
  function loadPlayerEpisode(index,autoplay){
    if(!currentMovieData||!currentMovieData.episodes||!currentMovieData.episodes[index])return;
    var ep=currentMovieData.episodes[index];
    if(!ep.stream_url){
      alert(currentMovieData.stream_locked?'Bu kino uchun VIP kerak.':'Video stream Telegram ichida ochilganda ishlaydi.');
      return;
    }
    currentEpisodeIndex=index;
    var nextBtn=document.getElementById('nextEpisode');
    if(nextBtn)nextBtn.disabled=index>=currentMovieData.episodes.length-1;
    lastProgressSave=0;
    resumeAppliedEpisodeId=0;
    playerError.classList.remove('show');
    document.getElementById('playerMovieTitle').textContent=currentMovieData.title;
    document.getElementById('playerEpisodeTitle').textContent=ep.number+'-qism';
    if(playerIsInline){
      document.getElementById('inlinePlayerMetaTitle').textContent=currentMovieData.title||'AIKINOUZ';
      document.getElementById('inlinePlayerMetaSub').textContent=(appLang==='ru'?'Серия ':appLang==='en'?'Episode ':'Qism ')+ep.number;
    }
    document.getElementById('progressBar').value=0;
    document.getElementById('progressBar').style.setProperty('--progress-pct','0%');
    document.getElementById('currentTime').textContent='0:00';
    playerVideo.src=ep.stream_url;
    playerVideo.load();
    renderPlaylist();
    showPlayerControls(true);
    if(autoplay!==false){
      var p=playerVideo.play(); if(p&&p.catch)p.catch(function(){showPlayerControls(false)});
    }
  }
  function attachInlinePlayer(movie,index){
    if(!inlinePlayerMount||!inlinePlayerWrap)return false;
    if(videoStage.parentNode!==inlinePlayerMount)inlinePlayerMount.appendChild(videoStage);
    videoStage.classList.add('inlineStage');
    playerOverlay.classList.remove('active');
    inlinePlayerWrap.classList.add('active');
    playerIsInline=true;
    document.body.style.overflow='';
    document.getElementById('inlinePlayerMetaTitle').textContent=movie.title||'AIKINOUZ';
    var ep=(movie.episodes||[])[index];
    document.getElementById('inlinePlayerMetaSub').textContent=ep?((appLang==='ru'?'Серия ':appLang==='en'?'Episode ':'Qism ')+ep.number):'Premium player';
    return true;
  }
  function openPlayer(movie,index){
    currentMovieData=movie;
    var inlineOk=attachInlinePlayer(movie,index);
    if(!inlineOk){
      if(videoStage.parentNode!==playerOverlay)playerOverlay.appendChild(videoStage);
      videoStage.classList.remove('inlineStage');
      playerOverlay.classList.add('active');
      document.body.style.overflow='hidden';
      playerIsInline=false;
    }
    loadPlayerEpisode(index,true);
    if(inlineOk){
      setTimeout(function(){inlinePlayerWrap.scrollIntoView({behavior:'smooth',block:'start'})},40);
    }
  }
  function closePlayer(){
    clearTimeout(playerHideTimer);
    sendWatchProgress(true,'progress');
    playerVideo.pause();
    playerVideo.removeAttribute('src');
    playerVideo.load();
    playlistDrawer.classList.remove('open');
    playerOverlay.classList.remove('active');
    inlinePlayerWrap.classList.remove('active');
    if(videoStage.parentNode!==playerOverlay)playerOverlay.appendChild(videoStage);
    videoStage.classList.remove('inlineStage');
    playerIsInline=false;
    document.body.style.overflow='';
    showPlayerControls(false);
  }
  function togglePlay(){
    if(playerVideo.paused){var p=playerVideo.play();if(p&&p.catch)p.catch(function(){});}
    else playerVideo.pause();
  }

  function toggleFavorite(id){
    if(!initData){alert('Sevimlilar Telegram ichida ishlaydi.');return}
    api('/app/api/favorite/'+id,{method:'POST'}).then(function(r){
      if(r.error==='vip_required'){alert('Bu kino uchun VIP kerak.');return}
      me.favorite_ids=me.favorite_ids||[];
      var i=me.favorite_ids.indexOf(id);
      if(r.favorite&&i<0)me.favorite_ids.push(id);
      if(!r.favorite&&i>=0)me.favorite_ids.splice(i,1);
      openMovie(id);
    });
  }

  function renderProfile(){
    var cardEl=document.getElementById('profileCard');
    var favEl=document.getElementById('favoritesGrid');
    if(!me.authenticated){
      cardEl.innerHTML='<div class="profileName">Telegram orqali kiring</div><div class="status">Profil va sevimlilar bot ichidan ochilganda ishlaydi.</div>';
      favEl.innerHTML='<div class="empty">Profil ma’lumoti mavjud emas.</div>';
      var sb=document.getElementById('adminStatsBtn');if(sb)sb.style.display='none';
      return;
    }
    var u=me.user||{};
    var statsBtn=document.getElementById('adminStatsBtn');
    if(statsBtn)statsBtn.style.display=me.is_admin?'flex':'none';
    cardEl.innerHTML='<div class="profileName">'+esc((u.first_name||'')+' '+(u.last_name||''))+'</div><div class="status">'+(me.vip?tr('vipActive'):tr('standard'))+(me.continue?' · ▶ '+esc(me.continue.movie_title)+' '+me.continue.episode_number:'')+'</div>';
    var fav=movies.filter(function(m){return (me.favorite_ids||[]).indexOf(m.id)>=0});
    favEl.innerHTML=fav.length?fav.map(card).join(''):'<div class="empty">'+tr('noFav')+'</div>';
  }

  function loadAdminStats(){
    var grid=document.getElementById('statsGrid');
    var top=document.getElementById('topMoviesStats');
    var vipStats=document.getElementById('vipMoviesStats');
    if(!me.is_admin){
      grid.innerHTML='<div class="empty">Ruxsat yo‘q.</div>';
      top.innerHTML='';
      if(vipStats)vipStats.innerHTML='';
      return;
    }
    grid.innerHTML='<div class="empty">Statistika yuklanmoqda...</div>';
    api('/app/api/admin/stats').then(function(s){
      if(s._http){throw new Error('stats')}
      grid.innerHTML=
        '<div class="statCard"><div class="statValue">'+fmt(s.today_users)+'</div><div class="statLabel">Bugun kirgan odam</div></div>'+
        '<div class="statCard"><div class="statValue">'+fmt(s.today_sessions)+'</div><div class="statLabel">Bugungi kirishlar</div></div>'+
        '<div class="statCard"><div class="statValue">'+fmt(s.users_7d)+'</div><div class="statLabel">7 kunlik foydalanuvchi</div></div>'+
        '<div class="statCard"><div class="statValue">'+fmt(s.users_30d)+'</div><div class="statLabel">30 kunlik foydalanuvchi</div></div>'+
        '<div class="statCard"><div class="statValue">'+fmt(s.total_users)+'</div><div class="statLabel">Jami app foydalanuvchi</div></div>'+
        '<div class="statCard"><div class="statValue">'+fmt(s.total_sessions)+'</div><div class="statLabel">Jami app kirishlari</div></div>'+
        '<div class="statCard"><div class="statValue">'+fmt(s.today_viewers)+'</div><div class="statLabel">Bugun video ko‘rganlar</div></div>'+
        '<div class="statCard"><div class="statValue">'+fmt(s.today_views)+'</div><div class="statLabel">Bugungi ko‘rishlar</div></div>'+
        '<div class="statCard"><div class="statValue">'+fmt(s.total_viewers)+'</div><div class="statLabel">Jami tomoshabinlar</div></div>'+
        '<div class="statCard"><div class="statValue">'+fmt(s.total_views)+'</div><div class="statLabel">Jami app ko‘rishlari</div></div>'+
        '<div class="statCard"><div class="statValue">'+fmt(s.active_vips)+'</div><div class="statLabel">Faol VIP</div></div>';
      var rows=(s.top_movies||[]).map(function(m,i){
        return '<div class="topMovieRow"><span>'+(i+1)+'. '+esc(m.emoji||'🎬')+' '+esc(m.title)+'</span><span><b>'+fmt(m.views)+'</b> ko‘rish · '+fmt(m.viewers)+' odam</span></div>';
      }).join('');
      top.innerHTML=rows||'<div class="payStatus">Hozircha app ko‘rishlari yo‘q.</div>';
      var vipRows=(s.vip_movies||[]).map(function(m){
        return '<div class="vipStatsRow"><div><b>'+esc(m.title)+'</b></div><div class="vipStatsMeta">👁 '+fmt(m.views)+' · 👤 '+fmt(m.unique_viewers)+'<br>⏱ '+timeText(m.average_watch_time)+' · ✅ '+fmt(m.completed_views)+'</div></div>';
      }).join('');
      if(vipStats)vipStats.innerHTML=vipRows||'<div class="payStatus">Hozircha VIP kino ko‘rishlari yo‘q.</div>';
    }).catch(function(){
      grid.innerHTML='<div class="empty">Statistikani yuklab bo‘lmadi.</div>';
      top.innerHTML='';
      if(vipStats)vipStats.innerHTML='';
    });
  }

  var paymentInfo=null;
  var selectedManualDays=0;

  function moneyUzs(n){
    return Number(n||0).toLocaleString('en-GB').replace(/,/g,' ')+' so‘m';
  }

  function loadPayments(){
    var plans=document.getElementById('starPlans');
    var manual=document.getElementById('manualPaymentBox');
    if(!initData){
      plans.innerHTML='<div class="payStatus">'+tr('telegramOnly')+'</div>';
      manual.innerHTML='<div class="payStatus">'+tr('telegramLogin')+'</div>';
      document.getElementById('sendReceiptBtn').disabled=true;
      return;
    }
    api('/app/api/payment-info').then(function(info){
      paymentInfo=info;
      if(info.stars&&info.stars.enabled){
        var terms=document.getElementById('starsTerms');
        terms.style.display=info.stars.terms_accepted?'none':'block';
        plans.innerHTML=(info.stars.plans||[]).map(function(p){
          return '<button class="starPlan" data-star-days="'+p.days+'" '+(info.stars.terms_accepted?'':'disabled')+'><span>💎 '+p.days+' kun'+(p.recurring?' · Avtomatik':'')+'</span><span>⭐ '+p.stars+'</span></button>';
        }).join('')||'<div class="payStatus">'+tr('noPlans')+'</div>';
        document.getElementById('starsStatus').textContent=info.stars.subscription_active?'🔄 Faol avtomatik Stars obunangiz mavjud.':'';
      }else{
        document.getElementById('starsTerms').style.display='none';
        plans.innerHTML='<div class="payStatus">'+tr('starsOff')+'</div>';
      }
      if(info.manual&&info.manual.enabled){
        var plans=(info.manual.plans||[]);
        selectedManualDays=plans.length?Number(plans[0].days):0;
        var planButtons=plans.map(function(p){
          var label=p.days===180?tr('months6'):(p.days===365?tr('year1'):p.days+' '+tr('days'));
          return '<button class="starPlan manualPlan '+(Number(p.days)===selectedManualDays?'active':'')+'" data-manual-days="'+p.days+'"><span>💳 '+label+'</span><span>'+moneyUzs(p.price_uzs)+'</span></button>';
        }).join('');
        manual.innerHTML=
          '<div class="payDesc">'+tr('choosePlan')+'</div>'+
          '<div class="starPlans">'+planButtons+'</div>'+
          '<div class="cardNumber">'+esc(info.manual.card_number)+'</div>'+
          '<div class="payDesc">'+tr('cardOwner')+' <b>'+esc(info.manual.card_holder)+'</b></div>'+
          '<div class="payDesc">'+tr('receiptInstruction')+'</div>';
        document.getElementById('receiptInput').style.display='block';
        document.getElementById('sendReceiptBtn').style.display='block';
      }else{
        manual.innerHTML='<div class="payStatus">'+tr('manualOff')+'</div>';
        document.getElementById('receiptInput').style.display='none';
        document.getElementById('sendReceiptBtn').style.display='none';
      }
    }).catch(function(){
      plans.innerHTML='<div class="payStatus">To‘lov ma’lumotini yuklab bo‘lmadi.</div>';
      manual.innerHTML='<div class="payStatus">To‘lov ma’lumotini yuklab bo‘lmadi.</div>';
    });
  }

  function acceptPaymentTerms(){
    var btn=document.getElementById('acceptTermsBtn');
    btn.disabled=true;
    api('/app/api/payment-terms/accept',{method:'POST'}).then(function(r){
      if(r.ok)loadPayments();
    }).finally(function(){btn.disabled=false});
  }

  function openStarsInvoice(days){
    var st=document.getElementById('starsStatus');
    st.textContent=tr('paymentPreparing');
    api('/app/api/stars-invoice/'+days,{method:'POST'}).then(function(r){
      if(r.error==='terms_required'){st.textContent=tr('termsFirst');loadPayments();return}
      if(!r.invoice_url){st.textContent=tr('paymentOpenFail');return}
      if(tg&&typeof tg.openInvoice==='function'){
        tg.openInvoice(r.invoice_url,function(status){
          if(status==='paid'){
            st.textContent=tr('paymentPaid');
            setTimeout(function(){
              api('/app/api/me').then(function(v){me=v;renderProfile();renderVipAccess();});
            },1200);
          }else if(status==='cancelled'){st.textContent=tr('paymentCancelled')}
          else if(status==='failed'){st.textContent=tr('paymentFailed')}
        });
      }else{
        window.location.href=r.invoice_url;
      }
    }).catch(function(){st.textContent='Stars to‘lovini ochib bo‘lmadi.'});
  }

  function sendManualReceipt(){
    var input=document.getElementById('receiptInput');
    var status=document.getElementById('receiptStatus');
    var btn=document.getElementById('sendReceiptBtn');
    if(!input.files||!input.files[0]){status.textContent=tr('chooseReceipt');return}
    var file=input.files[0];
    if(file.size>8*1024*1024){status.textContent=tr('receiptTooLarge');return}
    if(!selectedManualDays){status.textContent=tr('chooseVipPlan');return}
    var form=new FormData();
    form.append('vip_days',String(selectedManualDays));
    form.append('receipt',file,file.name);
    btn.disabled=true;btn.textContent='⏳ YUBORILMOQDA...';status.textContent=tr('receiptSending');
    fetch('/app/api/manual-receipt',{method:'POST',headers:{'X-Telegram-Init-Data':initData},body:form})
      .then(function(r){return r.json().then(function(j){j._http=r.status;return j})})
      .then(function(r){
        if(r.ok){input.value='';status.textContent=tr('receiptSent')}
        else if(r.error==='pending_exists'){status.textContent=tr('pendingReceipt')}
        else if(r.error==='manual_disabled'){status.textContent=tr('manualOff')}
        else status.textContent=tr('receiptFail');
      }).catch(function(){status.textContent='Chek yuborilmadi. Internetni tekshiring.'})
      .finally(function(){btn.disabled=false;btn.textContent='📤 Chek rasmini yuborish'});
  }

  function sendSupport(){
    var box=document.getElementById('supportMessage'), btn=document.getElementById('supportSend'), st=document.getElementById('supportStatus');
    var message=(box.value||'').trim();
    if(!initData){st.textContent='Support Telegram ichida ishlaydi.';return}
    if(message.length<3){st.textContent=tr('messageShort');return}
    btn.disabled=true;btn.textContent='⏳ YUBORILMOQDA...';st.textContent=tr('messageSending');
    api('/app/api/support',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:message})}).then(function(r){
      if(r.ok){box.value='';st.textContent=tr('messageSent')} else st.textContent=tr('messageFail');
    }).catch(function(){st.textContent=tr('messageFail')}).finally(function(){btn.disabled=false;btn.textContent='📨 XABARNI YUBORISH'});
  }

  document.getElementById('langBtn').addEventListener('click',function(){show('language')});
  document.querySelectorAll('[data-language]').forEach(function(btn){
    btn.addEventListener('click',function(){changeLanguage(btn.getAttribute('data-language'))});
  });

  document.addEventListener('click',function(e){
    var termsAccept=e.target.closest('#acceptTermsBtn');
    if(termsAccept){acceptPaymentTerms();return}
    var open=e.target.closest('[data-open]');
    if(open){var target=open.getAttribute('data-open');var f=open.getAttribute('data-filter');if(f)setFilter(f);show(target);if(target==='payments')loadPayments();return}
    var star=e.target.closest('[data-star-days]');
    if(star){openStarsInvoice(Number(star.getAttribute('data-star-days')));return}
    var manualPlan=e.target.closest('[data-manual-days]');
    if(manualPlan){
      selectedManualDays=Number(manualPlan.getAttribute('data-manual-days'));
      document.querySelectorAll('.manualPlan').forEach(function(x){x.classList.toggle('active',Number(x.getAttribute('data-manual-days'))===selectedManualDays)});
      return;
    }
    var nav=e.target.closest('[data-nav]');
    if(nav){show(nav.getAttribute('data-nav'));return}
    var filter=e.target.closest('.filterBtn');
    if(filter){setFilter(filter.getAttribute('data-filter'));return}
    var movie=e.target.closest('.movieCard');
    if(movie){openMovie(Number(movie.getAttribute('data-movie')));return}
  });

  document.getElementById('searchInput').addEventListener('input',renderCatalog);
  document.getElementById('loadMoreCatalog').addEventListener('click',loadMoreCatalog);
  document.getElementById('searchOnly').addEventListener('input',renderSearch);
  document.getElementById('heroWatch').addEventListener('click',function(){if(featuredId)openMovie(featuredId)});
  document.getElementById('heroCatalog').addEventListener('click',function(){show('catalog')});
  document.getElementById('closeApp').addEventListener('click',function(){try{if(tg)tg.close();else history.back()}catch(e){history.back()}});
  document.getElementById('supportSend').addEventListener('click',sendSupport);
  document.getElementById('sendReceiptBtn').addEventListener('click',sendManualReceipt);
  document.getElementById('statsRefresh').addEventListener('click',loadAdminStats);
  document.getElementById('continueBtn').addEventListener('click',function(){
    if(!me.continue){alert('Hali tomosha boshlangan kino yo‘q.');return}
    api('/app/api/movie/'+me.continue.movie_id).then(function(m){
      var idx=(m.episodes||[]).findIndex(function(e){return e.id===me.continue.episode_id});
      openPlayer(m,idx>=0?idx:0);
    });
  });
  document.getElementById('favoritesBtn').addEventListener('click',function(){show('profile');setTimeout(function(){document.getElementById('favoritesGrid').scrollIntoView({behavior:'smooth'})},50)});

  document.getElementById('playerClose').addEventListener('click',function(e){e.stopPropagation();closePlayer()});
  document.getElementById('playPause').addEventListener('click',function(e){e.stopPropagation();togglePlay();showPlayerControls(true)});
  document.getElementById('nextEpisode').addEventListener('click',function(e){e.stopPropagation();if(currentMovieData&&currentEpisodeIndex<currentMovieData.episodes.length-1)loadPlayerEpisode(currentEpisodeIndex+1,true)});
  document.getElementById('playlistToggle').addEventListener('click',function(e){e.stopPropagation();playlistDrawer.classList.add('open');showPlayerControls(false)});
  document.getElementById('playlistClose').addEventListener('click',function(e){e.stopPropagation();playlistDrawer.classList.remove('open');showPlayerControls(true)});
  document.getElementById('playlistItems').addEventListener('click',function(e){var b=e.target.closest('[data-play-index]');if(b){loadPlayerEpisode(Number(b.getAttribute('data-play-index')),true);playlistDrawer.classList.remove('open')}});
  document.getElementById('volumeRange').addEventListener('input',function(e){
    var v=Math.max(0,Math.min(1,Number(e.target.value)||0));
    playerVideo.volume=v;playerVideo.muted=(v===0);
    document.getElementById('muteToggle').textContent=(playerVideo.muted||v===0)?'🔇':(v<0.5?'🔉':'🔊');
  });
  document.getElementById('muteToggle').addEventListener('click',function(e){
    e.stopPropagation();
    playerVideo.muted=!playerVideo.muted;
    document.getElementById('muteToggle').textContent=playerVideo.muted?'🔇':(playerVideo.volume<0.5?'🔉':'🔊');
  });
  document.getElementById('playerFullscreen').addEventListener('click',function(e){
    e.stopPropagation();
    try{
      var p=null;
      if(videoStage.requestFullscreen)p=videoStage.requestFullscreen();
      else if(playerVideo.requestFullscreen)p=playerVideo.requestFullscreen();
      else if(playerVideo.webkitEnterFullscreen)playerVideo.webkitEnterFullscreen();
      if(p&&p.then)p.then(function(){
        try{if(screen.orientation&&screen.orientation.lock)screen.orientation.lock('landscape').catch(function(){})}catch(x){}
      }).catch(function(){});
    }catch(err){}
  });
  document.getElementById('videoStage').addEventListener('pointerup',function(e){
    if(e.target.closest('button')||e.target.closest('input')||e.target.closest('.playlistDrawer'))return;
    var rect=videoStage.getBoundingClientRect();
    var side=(e.clientX-rect.left)<rect.width/2?'left':'right';
    var now=Date.now();
    if(stageTapTimer && lastTapSide===side && now-lastTapAt<320){
      clearTimeout(stageTapTimer);
      stageTapTimer=null;
      seekByDoubleTap(side);
      return;
    }
    lastTapSide=side;
    lastTapAt=now;
    clearTimeout(stageTapTimer);
    stageTapTimer=setTimeout(function(){
      stageTapTimer=null;
      if(playerControls.classList.contains('hiddenControls'))showPlayerControls(true);
      else hidePlayerControls();
    },280);
  });
  playerVideo.addEventListener('play',function(){
    document.getElementById('playPause').textContent='❚❚';
    showPlayerControls(true);
    sendWatchProgress(true,'start');
  });
  playerVideo.addEventListener('pause',function(){
    document.getElementById('playPause').textContent='▶';
    showPlayerControls(false);
    sendWatchProgress(true,'progress');
  });
  playerVideo.addEventListener('loadedmetadata',function(){
    document.getElementById('durationTime').textContent=timeText(playerVideo.duration);
    var ep=currentMovieData&&currentEpisodeIndex>=0?(currentMovieData.episodes||[])[currentEpisodeIndex]:null;
    var movieResume=currentMovieData&&currentMovieData.resume?currentMovieData.resume:null;
    var resume=0;
    if(ep&&movieResume&&movieResume.episode_id===ep.id)resume=Number(movieResume.current_time||0);
    else if(ep&&me.continue&&me.continue.episode_id===ep.id)resume=Number(me.continue.position_seconds||0);
    if(ep&&resumeAppliedEpisodeId!==ep.id){
      if(resume>2 && resume<Math.max(0,(playerVideo.duration||0)-5)){
        try{playerVideo.currentTime=resume}catch(e){}
      }
      resumeAppliedEpisodeId=ep.id;
    }
  });
  playerVideo.addEventListener('timeupdate',function(){
    document.getElementById('currentTime').textContent=timeText(playerVideo.currentTime);
    if(playerVideo.duration){
      var bar=document.getElementById('progressBar');
      var pct=Math.max(0,Math.min(100,(playerVideo.currentTime/playerVideo.duration)*100));
      bar.value=Math.round(pct*10);
      bar.style.setProperty('--progress-pct',pct+'%');
    }
    sendWatchProgress(false,'progress');
  });
  playerVideo.addEventListener('ended',function(){
    sendWatchProgress(true,'progress');
    if(currentMovieData&&currentEpisodeIndex<currentMovieData.episodes.length-1)loadPlayerEpisode(currentEpisodeIndex+1,true);
    else showPlayerControls(false);
  });
  playerVideo.addEventListener('error',function(){playerError.classList.add('show');showPlayerControls(false)});
  document.getElementById('progressBar').addEventListener('input',function(e){
    var pct=Number(e.target.value)/10;
    e.target.style.setProperty('--progress-pct',pct+'%');
    if(playerVideo.duration)playerVideo.currentTime=(Number(e.target.value)/1000)*playerVideo.duration;
  });
  document.addEventListener('visibilitychange',function(){
    if(document.hidden&&(playerOverlay.classList.contains('active')||inlinePlayerWrap.classList.contains('active'))){
      sendWatchProgress(true,'progress');
      playerVideo.pause();
    }
  });

  Promise.all([api('/app/api/catalog?offset=0&limit='+catalogPageSize),api('/app/api/me')]).then(function(res){
    movies=res[0].movies||[];
    catalogOffset=movies.length;
    catalogHasMore=!!res[0].has_more;
    me=res[1]||{authenticated:false,favorite_ids:[]};
    appLang=(me.language==='ru'||me.language==='en')?me.language:'uz';
    if(me.authenticated&&me.user&&me.user.first_name)document.getElementById('avatar').textContent=me.user.first_name.charAt(0).toUpperCase();
    applyLanguage(appLang);
  }).catch(function(){
    document.getElementById('trendRow').innerHTML='<div class="empty">Kinolarni yuklab bo‘lmadi. Appni qayta oching.</div>';
  });
})();
</script>
</body>
</html>"""
