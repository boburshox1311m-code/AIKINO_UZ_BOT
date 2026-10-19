from __future__ import annotations

import hmac
import html
import os
import re
from urllib.parse import urlparse

from aiohttp import web
from aiogram import Bot
from aiogram.types import URLInputFile

from .database import Database


def _authorized(request: web.Request) -> bool:
    expected = os.environ.get('AURA_INTEGRATION_TOKEN', '').strip()
    supplied = request.headers.get('Authorization', '')
    if not expected or not supplied.startswith('Bearer '):
        return False
    return hmac.compare_digest(supplied[7:].strip(), expected)


def _https_url(value: object, *, required: bool = True) -> str | None:
    clean = str(value or '').strip()
    if not clean and not required:
        return None
    parsed = urlparse(clean)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        raise web.HTTPBadRequest(text='invalid_media_url')
    return clean


async def aura_connection(request: web.Request) -> web.Response:
    if not _authorized(request):
        raise web.HTTPUnauthorized()
    bot: Bot = request.app['bot']
    me = await bot.get_me()
    return web.json_response({
        'ok': True,
        'adapter': 'aura_catalog_v1',
        'bot_id': str(me.id),
        'bot_username': me.username or '',
        'title': me.full_name or me.username or 'Telegram katalog boti',
        'capabilities': {
            'series_catalog': True,
            'poster': True,
            'episode_idempotency': True,
            'max_video_mb': 2000,
        },
    })


async def aura_publish(request: web.Request) -> web.Response:
    if not _authorized(request):
        raise web.HTTPUnauthorized()
    db: Database = request.app['db']
    bot: Bot = request.app['bot']
    admin_id: int = request.app['admin_id']
    data = await request.json()
    idempotency_key = str(data.get('idempotency_key') or '').strip()[:255]
    if len(idempotency_key) < 8:
        raise web.HTTPBadRequest(text='idempotency_key_required')
    current = await db.begin_aura_import(idempotency_key)
    if current['status'] == 'published':
        return web.json_response({
            'ok': True, 'idempotent': True,
            'movie_id': int(current['movie_id']),
            'episode_id': int(current['episode_id']),
        })
    try:
        if str(data.get('content_type') or '') != 'series':
            raise web.HTTPUnprocessableEntity(text='catalog_requires_series_content')
        title = re.sub(r'\s+', ' ', str(data.get('title') or '').strip())[:240]
        match = re.search(r'\d+', str(data.get('episode_number') or ''))
        if not title or not match:
            raise web.HTTPUnprocessableEntity(text='movie_title_and_episode_required')
        episode_number = int(match.group())
        if episode_number < 1:
            raise web.HTTPUnprocessableEntity(text='invalid_episode_number')
        video_url = _https_url(data.get('video_url'))
        cover_url = _https_url(data.get('cover_url'), required=False)
        requested_movie_id = str(data.get('catalog_movie_id') or '').strip()
        movie = await db.movie(int(requested_movie_id)) if requested_movie_id.isdigit() else None
        if not movie:
            movie = await db.movie_by_title(title)
        created_movie = False
        if not movie:
            if episode_number != 1:
                raise web.HTTPConflict(text='new_movie_must_start_from_episode_1')
            movie = await db.add_movie(title, '🎬')
            created_movie = True

        if not movie['poster_file_id'] and not cover_url:
            raise web.HTTPUnprocessableEntity(text='new_movie_poster_required')
        if cover_url and (created_movie or bool(data.get('update_catalog_poster')) or not movie['poster_file_id']):
            poster_message = await bot.send_photo(
                admin_id,
                URLInputFile(cover_url, filename='catalog-poster.jpg', timeout=600),
                caption=f"🖼 <b>{html.escape(title)}</b> katalog posteri",
            )
            await db.set_movie_poster(int(movie['id']), poster_message.photo[-1].file_id)
            try:
                await bot.delete_message(admin_id, poster_message.message_id)
            except Exception:
                pass
        caption = str(data.get('caption') or '').strip()
        if not caption:
            caption = f'🎬 {title}\n📺 {episode_number}-qism'
        sent = await bot.send_video(
            admin_id,
            URLInputFile(
                video_url,
                filename=str(data.get('video_filename') or f'{title}-{episode_number}.mp4')[:240],
                timeout=7200,
                chunk_size=1024 * 1024,
            ),
            caption=html.escape(caption[:1024]),
            supports_streaming=True,
        )
        if not sent.video:
            raise RuntimeError('telegram_video_file_id_missing')
        episode = await db.upsert_episode(
            int(movie['id']), episode_number,
            sent.video.file_id, sent.video.file_unique_id, html.escape(caption[:1024]),
        )
        await db.complete_aura_import(int(current['id']), int(movie['id']), int(episode['id']))
        return web.json_response({
            'ok': True, 'idempotent': False,
            'movie_id': int(movie['id']), 'episode_id': int(episode['id']),
            'created_movie': created_movie,
        })
    except web.HTTPException as exc:
        await db.fail_aura_import(int(current['id']), exc.text or type(exc).__name__)
        raise
    except Exception as exc:
        await db.fail_aura_import(int(current['id']), type(exc).__name__)
        raise


def register_aura_routes(app: web.Application) -> None:
    app.router.add_get('/api/aura/v1/connection', aura_connection)
    app.router.add_post('/api/aura/v1/publish', aura_publish)
