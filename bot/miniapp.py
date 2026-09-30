from __future__ import annotations

import hashlib
import hmac
import json
import time
from io import BytesIO
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot

from .database import Database

POSTER_CACHE: dict[int, bytes] = {}


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
        "poster_url": f"/app/poster/{movie['id']}" if movie["poster_file_id"] else None,
    }


async def app_page(request: web.Request) -> web.Response:
    return web.Response(text=MINI_APP_HTML, content_type="text/html")


async def api_catalog(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    movies = await db.miniapp_movies(100)
    payload = [_movie_json(movie) for movie in movies]
    return web.json_response({"movies": payload})


async def api_movie(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    try:
        movie_id = int(request.match_info["movie_id"])
    except ValueError:
        raise web.HTTPBadRequest()
    movie = await db.movie_with_stats(movie_id)
    if not movie:
        raise web.HTTPNotFound()
    episodes = await db.episodes(movie_id, 0, 200)
    data = _movie_json(movie)
    data["episodes"] = [
        {"id": int(ep["id"]), "number": int(ep["episode_number"])}
        for ep in episodes
    ]
    return web.json_response(data)


async def api_me(request: web.Request) -> web.Response:
    user = _request_user(request)
    if not user:
        return web.json_response({"authenticated": False})
    db: Database = request.app["db"]
    user_id = int(user["id"])
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
        "vip": bool(vip),
        "vip_expires_at": vip["expires_at"].isoformat() if vip else None,
        "favorite_ids": [int(movie["id"]) for movie in favorites],
        "continue": {
            "episode_id": int(progress["id"]),
            "movie_id": int(progress["movie_id"]),
            "movie_title": progress["movie_title"],
            "episode_number": int(progress["episode_number"]),
        } if progress else None,
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
    text = (
        "🛟 <b>AIKINOUZ SUPPORT</b>\n\n"
        f"👤 <b>{full_name or 'Foydalanuvchi'}</b>\n"
        f"🔗 {username}\n"
        f"🆔 <code>{user.get('id')}</code>\n\n"
        f"💬 <b>Xabar:</b>\n{message}"
    )
    await bot.send_message(admin_id, text)
    return web.json_response({"ok": True})


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


def register_miniapp_routes(app: web.Application) -> None:
    app.router.add_get("/app", app_page)
    app.router.add_get("/app/", app_page)
    app.router.add_get("/app/api/catalog", api_catalog)
    app.router.add_get("/app/api/movie/{movie_id}", api_movie)
    app.router.add_get("/app/api/me", api_me)
    app.router.add_post("/app/api/favorite/{movie_id}", api_toggle_favorite)
    app.router.add_post("/app/api/support", api_support)
    app.router.add_get("/app/poster/{movie_id}", poster)


MINI_APP_HTML = r"""<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#050505">
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
.crown{width:42px;height:42px;border-radius:14px;background:linear-gradient(145deg,#ffe58a,#a96913);display:grid;place-items:center;color:#1b1102;font-size:23px;box-shadow:0 0 28px #d99d2d33}
.logo{font-weight:950;letter-spacing:1.1px;color:#f6ca63;font-size:20px}
.sub{font-size:10px;color:#a88b54;letter-spacing:1.8px}
.headRight{display:flex;align-items:center;gap:8px}
.avatar,.closeBtn{width:38px;height:38px;border-radius:12px;border:1px solid #3a2d18;background:#101010;color:#f4c75c;display:grid;place-items:center;font-weight:900}
.closeBtn{color:#ddd;font-size:21px}

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

.profile{padding:10px 14px 24px}.profileCard{border:1px solid #49391e;background:linear-gradient(145deg,#15110b,#0c0c0c);border-radius:20px;padding:18px}.profileName{font-size:21px;font-weight:950}.status{font-size:12px;color:#efc054;margin-top:5px}
.profileMenu{margin-top:14px;display:grid;gap:8px}.profileItem{width:100%;display:flex;justify-content:space-between;align-items:center;padding:14px 15px;border-radius:14px;border:1px solid #27231c;background:#0e0e0e;color:#fff;text-align:left;font-weight:800}.profileItem.gold{color:#f4ca61;border-color:#57411d}
.supportCard{margin:8px 14px 20px;border:1px solid #5a431e;background:linear-gradient(145deg,#17120b,#0b0b0b);border-radius:22px;padding:18px}.supportTitle{font-size:24px;font-weight:950;color:#f5ca62}.supportMeta{display:grid;gap:9px;margin-top:14px;color:#d2d2d2;font-size:13px;line-height:1.45}.supportMeta b{color:#f0c45d}.supportForm textarea{width:100%;min-height:130px;background:#0e0e0e;color:#fff;border:1px solid #332d24;border-radius:14px;padding:13px;margin-top:8px}.supportSend{width:100%;margin-top:10px;border:0;border-radius:13px;padding:13px;background:linear-gradient(135deg,#ffe17a,#b87518);font-weight:950;color:#171003}.supportNote{font-size:11px;color:#8f8f8f;margin-top:8px}
.empty{padding:28px 14px;color:#888;text-align:center}

.bottom{position:fixed;left:0;right:0;bottom:0;z-index:40;display:grid;grid-template-columns:repeat(5,1fr);background:#070707f4;backdrop-filter:blur(20px);border-top:1px solid #201b13;padding:7px 6px calc(9px + env(safe-area-inset-bottom))}
.navBtn{border:0;background:none;color:#858585;font-size:10px;font-weight:800;padding:5px 1px}.navBtn b{display:block;font-size:21px;margin-bottom:2px}.navBtn.active{color:#f4c75c}
.hidden{display:none!important}
</style>
</head>
<body>
<div class="app">
<header class="top">
  <div class="brand">
    <div class="brandmark"><div class="crown">♛</div><div><div class="logo">AIKINOUZ</div><div class="sub">PREMIUM CINEMA</div></div></div>
    <div class="headRight"><div id="avatar" class="avatar">A</div><button id="closeApp" class="closeBtn">×</button></div>
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
</main>

<main id="detail" class="page">
  <div class="pageTop"><button class="backBtn" data-open="catalog">‹</button><div class="pageTitle">Kino sahifasi</div></div>
  <div id="detailContent"></div>
</main>

<main id="vip" class="page">
  <div class="pageTop"><button class="backBtn" data-open="home">‹</button><div class="pageTitle">AIKINOUZ VIP</div></div>
  <section class="vipHero">
    <div class="big">♛</div><h1>AIKINOUZ VIP</h1><div class="sub">PREMIUM KOLLEKSIYA</div>
    <div class="benefits"><div class="benefit"><b>💎</b>Eksklyuziv</div><div class="benefit"><b>🎬</b>Premium kino</div><div class="benefit"><b>⚡</b>Tez kirish</div></div>
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
      <button class="profileItem" id="favoritesBtn"><span>♡ Sevimlilar</span><span>›</span></button>
      <button class="profileItem gold" data-open="support"><span>🛟 AIKINOUZ SUPPORT</span><span>›</span></button>
    </div>
    <div class="sectionHead" style="margin-top:20px"><h2>❤️ Sevimlilar</h2></div>
    <div id="favoritesGrid" class="catalog" style="padding:0"></div>
  </div>
</main>

<main id="support" class="page">
  <div class="pageTop"><button class="backBtn" data-open="profile">‹</button><div class="pageTitle">Support</div></div>
  <section class="supportCard">
    <div class="supportTitle">👑 AIKINOUZ</div><div class="sub">PREMIUM KINO PLATFORMASI</div>
    <div class="supportMeta">
      <div>🏢 <b>Kompaniya:</b> AIKINOUZ</div>
      <div>👑 <b>Kompaniya prezidenti:</b><br>BOBURMIRZO GAZIEV MAKHAMMATTOLIBJON UGLI</div>
      <div>📧 <b>Email:</b> boburshox1311m@gmail.com</div>
      <div>🧩 <b>Project:</b> AIKINOUZ / AIKINO_UZ_BOT</div>
      <div>© 2026 AIKINOUZ. All rights reserved.</div>
      <div><b>Project owner / author:</b><br>BOBURMIRZO GAZIEV MAKHAMMATTOLIBJON UGLI</div>
    </div>
    <div class="supportForm"><h3>💬 Adminga yozish</h3><textarea id="supportMessage" maxlength="2000" placeholder="Savol, muammo yoki taklifingizni yozing..."></textarea><button id="supportSend" class="supportSend">📨 XABARNI YUBORISH</button><div id="supportStatus" class="supportNote">Xabaringiz AIKINOUZ adminiga yuboriladi.</div></div>
  </section>
</main>

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
  var catalogFilter = 'all';
  var featuredId = 0;

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
    return '<div class="card movieCard" data-movie="'+m.id+'"><div class="poster">'+poster+badge(m)+'</div><div class="cardTitle">'+esc(m.title)+'</div><div class="cardMeta">👁 '+fmt(m.views)+' · '+m.episode_count+' qism</div></div>';
  }

  function show(id){
    document.querySelectorAll('.page').forEach(function(x){x.classList.remove('active')});
    var el=document.getElementById(id); if(el) el.classList.add('active');
    document.querySelectorAll('.navBtn').forEach(function(x){x.classList.toggle('active',x.getAttribute('data-nav')===id)});
    window.scrollTo(0,0);
    if(id==='profile') renderProfile();
    if(id==='search') renderSearch();
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

  function renderHome(){
    var trend=filtered(movies,'trend','').slice(0,8);
    var normal=movies.filter(function(m){return !m.is_vip}).slice(0,8);
    var vip=movies.filter(function(m){return m.is_vip}).slice(0,8);
    document.getElementById('trendRow').innerHTML=(trend.length?trend:normal).map(card).join('');
    document.getElementById('newRow').innerHTML=normal.map(card).join('');
    document.getElementById('vipRow').innerHTML=vip.length?vip.map(card).join(''):'<div class="empty">VIP kinolar hozircha yo‘q.</div>';
    document.getElementById('vipGrid').innerHTML=vip.length?vip.map(card).join(''):'<div class="empty">VIP kinolar hozircha qo‘shilmagan.</div>';

    var f=(trend[0]||normal[0]||movies[0]);
    if(f){
      featuredId=f.id;
      var hero=document.getElementById('hero');
      if(f.poster_url) hero.style.backgroundImage='url("'+f.poster_url+'")';
      document.getElementById('heroTitle').textContent=f.title;
      document.getElementById('heroMeta').textContent='👁 '+fmt(f.views)+' ko‘rish · 🎞 '+f.episode_count+' qism'+(f.badge?' · 🔥 '+f.badge:'')+(f.is_vip?' · 💎 VIP':'');
      document.getElementById('heroDesc').textContent=f.description||'AIKINOUZ premium kino kolleksiyasi.';
    }
  }

  function renderCatalog(){
    var q=document.getElementById('searchInput').value.trim();
    var list=filtered(movies,catalogFilter,q);
    document.getElementById('catalogGrid').innerHTML=list.length?list.map(card).join(''):'<div class="empty">Kino topilmadi.</div>';
  }
  function renderSearch(){
    var q=document.getElementById('searchOnly').value.trim();
    var list=filtered(movies,'all',q);
    document.getElementById('searchGrid').innerHTML=list.length?list.map(card).join(''):'<div class="empty">Kino topilmadi.</div>';
  }

  function openMovie(id){
    show('detail');
    document.getElementById('detailContent').innerHTML='<div class="empty">Yuklanmoqda...</div>';
    api('/app/api/movie/'+id).then(function(m){
      if(m._http){throw new Error('movie')}
      var fav=me.favorite_ids && me.favorite_ids.indexOf(m.id)>=0;
      var poster=m.poster_url?'<img src="'+m.poster_url+'" alt="">':'<div class="posterFallback">🎬</div>';
      var eps=(m.episodes||[]).map(function(e){return '<div class="episode"><span><b>'+e.number+'-QISM</b></span><button class="epOpen" data-ep="'+e.id+'">▶ Ochish</button></div>'}).join('');
      document.getElementById('detailContent').innerHTML=
        '<div class="detailPoster">'+poster+'</div>'+
        '<div class="detailBody"><h1>'+esc(m.title)+'</h1>'+
        '<div class="chips"><span class="chip">👁 '+fmt(m.views)+' ko‘rish</span><span class="chip">🎞 '+m.episode_count+' qism</span>'+(m.badge?'<span class="chip">🔥 '+esc(m.badge)+'</span>':'')+(m.is_vip?'<span class="chip">💎 VIP</span>':'')+'</div>'+
        '<div class="desc">'+esc(m.description||'AIKINOUZ premium kino kolleksiyasi.')+'</div>'+
        '<div class="detailActions"><button id="watchFirst" class="goldBtn">▶ Tomosha qilish</button><button id="favMovie" class="darkBtn">'+(fav?'♥ Sevimlida':'♡ Sevimlilar')+'</button></div>'+
        '<div class="tabs"><button class="tab active">Qismlar</button><button class="tab">Tavsif</button></div>'+
        '<div class="episodes">'+(eps||'<div class="empty">Qismlar hozircha yo‘q.</div>')+'</div></div>';

      var first=(m.episodes||[])[0];
      var w=document.getElementById('watchFirst'); if(w) w.onclick=function(){if(first) openEpisode(first.id)};
      var fv=document.getElementById('favMovie'); if(fv) fv.onclick=function(){toggleFavorite(m.id)};
      document.querySelectorAll('.epOpen').forEach(function(b){b.onclick=function(){openEpisode(Number(b.getAttribute('data-ep')))}});
    }).catch(function(){document.getElementById('detailContent').innerHTML='<div class="empty">Kino ma’lumotini yuklab bo‘lmadi.</div>'});
  }

  function openEpisode(id){
    if(!id)return;
    var url='https://t.me/AIKINO_UZ_BOT?start=ep_'+id;
    try{if(tg&&tg.openTelegramLink){tg.openTelegramLink(url);return}}catch(e){}
    window.location.href=url;
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
      return;
    }
    var u=me.user||{};
    cardEl.innerHTML='<div class="profileName">'+esc((u.first_name||'')+' '+(u.last_name||''))+'</div><div class="status">'+(me.vip?'💎 VIP ACTIVE':'✨ STANDARD')+(me.continue?' · ▶ '+esc(me.continue.movie_title)+' '+me.continue.episode_number+'-qism':'')+'</div>';
    var fav=movies.filter(function(m){return (me.favorite_ids||[]).indexOf(m.id)>=0});
    favEl.innerHTML=fav.length?fav.map(card).join(''):'<div class="empty">Hozircha sevimli kinolar yo‘q.</div>';
  }

  function sendSupport(){
    var box=document.getElementById('supportMessage'), btn=document.getElementById('supportSend'), st=document.getElementById('supportStatus');
    var message=(box.value||'').trim();
    if(!initData){st.textContent='Support Telegram ichida ishlaydi.';return}
    if(message.length<3){st.textContent='Xabarni biroz to‘liqroq yozing.';return}
    btn.disabled=true;btn.textContent='⏳ YUBORILMOQDA...';st.textContent='Xabar yuborilmoqda...';
    api('/app/api/support',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:message})}).then(function(r){
      if(r.ok){box.value='';st.textContent='✅ Xabaringiz adminga yuborildi.'} else st.textContent='Xabar yuborilmadi. Qayta urinib ko‘ring.';
    }).catch(function(){st.textContent='Xabar yuborilmadi. Internetni tekshiring.'}).finally(function(){btn.disabled=false;btn.textContent='📨 XABARNI YUBORISH'});
  }

  document.addEventListener('click',function(e){
    var open=e.target.closest('[data-open]');
    if(open){var target=open.getAttribute('data-open');var f=open.getAttribute('data-filter');if(f)setFilter(f);show(target);return}
    var nav=e.target.closest('[data-nav]');
    if(nav){show(nav.getAttribute('data-nav'));return}
    var filter=e.target.closest('.filterBtn');
    if(filter){setFilter(filter.getAttribute('data-filter'));return}
    var movie=e.target.closest('.movieCard');
    if(movie){openMovie(Number(movie.getAttribute('data-movie')));return}
  });

  document.getElementById('searchInput').addEventListener('input',renderCatalog);
  document.getElementById('searchOnly').addEventListener('input',renderSearch);
  document.getElementById('heroWatch').addEventListener('click',function(){if(featuredId)openMovie(featuredId)});
  document.getElementById('heroCatalog').addEventListener('click',function(){show('catalog')});
  document.getElementById('closeApp').addEventListener('click',function(){try{if(tg)tg.close();else history.back()}catch(e){history.back()}});
  document.getElementById('supportSend').addEventListener('click',sendSupport);
  document.getElementById('continueBtn').addEventListener('click',function(){if(me.continue)openEpisode(me.continue.episode_id);else alert('Hali tomosha boshlangan kino yo‘q.')});
  document.getElementById('favoritesBtn').addEventListener('click',function(){show('profile');setTimeout(function(){document.getElementById('favoritesGrid').scrollIntoView({behavior:'smooth'})},50)});

  Promise.all([api('/app/api/catalog'),api('/app/api/me')]).then(function(res){
    movies=res[0].movies||[];
    me=res[1]||{authenticated:false,favorite_ids:[]};
    if(me.authenticated&&me.user&&me.user.first_name)document.getElementById('avatar').textContent=me.user.first_name.charAt(0).toUpperCase();
    renderHome();renderCatalog();renderSearch();renderProfile();
  }).catch(function(){
    document.getElementById('trendRow').innerHTML='<div class="empty">Kinolarni yuklab bo‘lmadi. Appni qayta oching.</div>';
  });
})();
</script>
</body>
</html>"""\n