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
:root{--bg:#050505;--panel:#101010;--panel2:#17120b;--gold:#f5c451;--gold2:#9b6817;--red:#d9232e;--text:#fff;--muted:#9d9d9d;--line:#2b2418}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
body:before{content:"";position:fixed;inset:0;pointer-events:none;background:radial-gradient(circle at 85% -10%,#6d421c55,transparent 34%),radial-gradient(circle at -10% 40%,#8a11172b,transparent 32%)}
.app{min-height:var(--tg-viewport-stable-height,100vh);padding:calc(env(safe-area-inset-top) + 4px) 0 calc(88px + env(safe-area-inset-bottom))}
.top{position:sticky;top:0;z-index:20;background:#050505ee;backdrop-filter:blur(20px);padding:10px 14px 10px;border-bottom:1px solid #1d1a14}
.brand{display:flex;align-items:center;justify-content:space-between;gap:10px}.brandmark{display:flex;align-items:center;gap:10px}.brandActions{display:flex;align-items:center;gap:8px}.crown{width:40px;height:40px;border-radius:14px;background:linear-gradient(145deg,#ffe88c,#aa6914);display:grid;place-items:center;color:#130c02;font-size:22px;box-shadow:0 0 28px #e7a92735}
.logo{font-weight:950;letter-spacing:1.1px;color:#f8d16e;font-size:20px}.sub{font-size:10px;color:#a48b58;letter-spacing:1.7px}.avatar{width:38px;height:38px;border-radius:50%;border:1px solid #6e521e;background:#17130c;display:grid;place-items:center;font-weight:800;color:#f3cb67}.closeBtn{width:38px;height:38px;border-radius:12px;border:1px solid #2a241b;background:#111;color:#ddd;font-size:20px;display:grid;place-items:center}
.hero{margin:14px 16px 8px;border-radius:26px;min-height:260px;padding:24px;display:flex;align-items:flex-end;position:relative;overflow:hidden;background:linear-gradient(135deg,#2a1909,#0b0b0b 55%,#351011);border:1px solid #4f3a1b;box-shadow:0 18px 60px #0008}
.hero:after{content:"";position:absolute;inset:0;background:linear-gradient(0deg,#050505dd,transparent 70%)}
.heroContent{position:relative;z-index:2}.eyebrow{color:#f0bc48;font-weight:800;font-size:12px;letter-spacing:1.7px}.hero h1{margin:7px 0 8px;font-size:29px;line-height:1.02}.hero p{margin:0;color:#c6c6c6;max-width:330px;line-height:1.4;font-size:14px}
.heroBtn{margin-top:16px;border:0;border-radius:14px;padding:14px 19px;background:linear-gradient(135deg,#ffe07a,#b97618);font-weight:950;color:#1a1003;font-size:14px;box-shadow:0 10px 28px #b9761838}
.section{padding:12px 16px 2px}.sectionHead{display:flex;justify-content:space-between;align-items:center;margin:0 0 10px}.section h2{font-size:19px;margin:0}.sectionHead span{font-size:12px;color:#c49842}
.row{display:flex;gap:11px;overflow:auto;padding-bottom:6px;scrollbar-width:none}.row::-webkit-scrollbar{display:none}
.card{width:132px;min-width:132px}.poster{width:132px;height:185px;border-radius:16px;overflow:hidden;background:linear-gradient(145deg,#25190b,#151515);border:1px solid #2d261b;position:relative}
.poster img{width:100%;height:100%;object-fit:cover;display:block}.posterFallback{height:100%;display:grid;place-items:center;color:#d9ad52;font-size:34px}
.badge{position:absolute;top:7px;left:7px;border-radius:8px;padding:4px 7px;font-size:10px;font-weight:900;background:#d7212d;color:white}.vip{left:auto;right:7px;background:#e8b73b;color:#171006}
.title{font-weight:750;font-size:13px;margin-top:7px;line-height:1.22;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.meta{font-size:11px;color:#8f8f8f;margin-top:3px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.tile{background:#101010;border:1px solid #24201a;border-radius:16px;padding:14px 10px;text-align:center;font-size:12px;font-weight:700}.tile b{display:block;font-size:22px;margin-bottom:6px}
.bottom{position:fixed;bottom:0;left:0;right:0;z-index:30;background:#080808f2;backdrop-filter:blur(20px);border-top:1px solid #211d17;padding:8px 8px calc(10px + env(safe-area-inset-bottom));display:grid;grid-template-columns:repeat(5,1fr)}
.nav{border:0;background:transparent;color:#888;font-size:10px;padding:6px 2px;font-weight:700}.nav b{display:block;font-size:22px;margin-bottom:3px}.nav.active{color:#f2c351}
.view{display:none}.view.active{display:block}.pageTop{padding:18px 16px 8px;display:flex;align-items:center;gap:12px}.back{border:0;background:#171717;color:#fff;width:38px;height:38px;border-radius:12px;font-size:20px}.pageTitle{font-weight:900;font-size:22px}
.catalog{display:grid;grid-template-columns:repeat(2,1fr);gap:13px;padding:10px 16px 22px}.catalog .card{width:auto;min-width:0}.catalog .poster{width:100%;height:235px}
.search{margin:8px 16px 4px;display:flex;gap:8px}.search input{flex:1;padding:13px 14px;border-radius:14px;background:#111;border:1px solid #2c2c2c;color:white;font-size:15px}
.detailHero{margin:0 16px;border-radius:22px;overflow:hidden;background:#111;border:1px solid #282018}.detailHero img{width:100%;height:430px;object-fit:cover;display:block}.detailBody{padding:16px}.detailBody h1{font-size:28px;margin:0 0 8px}.chips{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 14px}.chip{font-size:11px;border:1px solid #4b3b1f;background:#17130d;color:#e4be64;border-radius:999px;padding:6px 9px}.desc{color:#c2c2c2;line-height:1.55;font-size:14px}.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:15px 0}.goldBtn,.darkBtn{border:0;border-radius:13px;padding:13px;font-weight:850}.goldBtn{background:linear-gradient(135deg,#ffd96f,#b97618);color:#1a1003}.darkBtn{background:#171717;color:#fff;border:1px solid #2b2b2b}
.episodes{display:grid;gap:8px}.episode{display:flex;justify-content:space-between;align-items:center;background:#111;border:1px solid #242424;border-radius:14px;padding:13px}.episode button{border:0;border-radius:10px;background:#2b2111;color:#f2c75d;padding:8px 11px;font-weight:800}
.profile{padding:18px 16px}.profileCard{border:1px solid #332819;background:linear-gradient(145deg,#15110b,#0d0d0d);border-radius:20px;padding:18px}.profileName{font-size:20px;font-weight:900}.status{color:#f0bd4d;font-size:12px;margin-top:5px}.empty{padding:28px 16px;text-align:center;color:#888}
.supportBtn{width:100%;margin-top:20px;border:1px solid #5e451d;background:linear-gradient(135deg,#17120b,#0c0c0c);color:#f3c95f;border-radius:16px;padding:15px 16px;font-weight:900;font-size:15px;text-align:left}
.supportCard{margin:10px 16px 16px;border:1px solid #59421c;background:linear-gradient(145deg,#17120b,#0b0b0b);border-radius:22px;padding:18px;box-shadow:0 18px 50px #0007}.supportTitle{font-size:24px;font-weight:950;color:#f4ca62}.supportMeta{margin-top:14px;display:grid;gap:9px;color:#d1d1d1;font-size:13px;line-height:1.45}.supportMeta b{color:#f1c45c}.supportForm{margin-top:18px}.supportForm textarea{width:100%;min-height:130px;resize:vertical;border-radius:15px;border:1px solid #383027;background:#0d0d0d;color:#fff;padding:14px;font:inherit}.supportSend{width:100%;margin-top:10px;border:0;border-radius:14px;padding:14px;background:linear-gradient(135deg,#ffe07a,#b97618);color:#1a1003;font-weight:950}.supportNote{font-size:11px;color:#8f8f8f;margin-top:8px;line-height:1.4}
</style>
</head>
<body>
<div class="app">
  <header class="top">
    <div class="brand"><div class="brandmark"><div class="crown">♛</div><div><div class="logo">AIKINOUZ</div><div class="sub">PREMIUM CINEMA</div></div></div><div class="brandActions"><div class="avatar" id="avatar">A</div><button class="closeBtn" onclick="closeApp()" aria-label="Yopish">×</button></div></div>
  </header>

  <main id="home" class="view active">
    <section class="hero"><div class="heroContent"><div class="eyebrow">AIKINOUZ PREMIERE</div><h1>Premium kino olamiga xush kelibsiz</h1><p>Trenddagi seriallar, yangi qismlar va maxsus VIP kolleksiya — to‘liq ekran rejimida bir joyda.</p><button class="heroBtn" onclick="showView('catalog')">🎬 TOMOSHANI BOSHLASH</button></div></section>
    <section class="section"><div class="sectionHead"><h2>🔥 Trendda</h2><span>1000+ ko‘rish</span></div><div id="trendRow" class="row"></div></section>
    <section class="section"><div class="sectionHead"><h2>🆕 Yangi kinolar</h2><span onclick="showView('catalog')">Barchasi ›</span></div><div id="newRow" class="row"></div></section>
    <section class="section"><div class="sectionHead"><h2>Tez kirish</h2></div><div class="grid">
      <div class="tile" onclick="showView('catalog')"><b>🎬</b>Katalog</div>
      <div class="tile" onclick="showView('vip')"><b>💎</b>VIP</div>
      <div class="tile" onclick="showView('profile')"><b>❤️</b>Sevimlilar</div>
    </div></section>
  </main>

  <main id="catalog" class="view">
    <div class="pageTop"><button class="back" onclick="showView('home')">‹</button><div class="pageTitle">Kino katalogi</div></div>
    <div class="search"><input id="searchInput" placeholder="Kino yoki serial qidirish..." oninput="renderCatalog()"></div>
    <div id="catalogGrid" class="catalog"></div>
  </main>

  <main id="detail" class="view"><div class="pageTop"><button class="back" onclick="showView('catalog')">‹</button><div class="pageTitle">Kino</div></div><div id="detailContent"></div></main>

  <main id="vip" class="view">
    <div class="pageTop"><button class="back" onclick="showView('home')">‹</button><div class="pageTitle">AIKINOUZ VIP</div></div>
    <section class="hero" style="min-height:190px"><div class="heroContent"><div class="eyebrow">💎 EXCLUSIVE</div><h1>VIP kolleksiya</h1><p>Maxsus kinolar, premium kontent va yangi qismlarga tez kirish.</p></div></section>
    <div id="vipGrid" class="catalog"></div>
  </main>

  <main id="profile" class="view">
    <div class="pageTop"><button class="back" onclick="showView('home')">‹</button><div class="pageTitle">Profilim</div></div>
    <div class="profile">
      <div id="profileCard" class="profileCard"></div>
      <div class="sectionHead" style="margin-top:22px"><h2>❤️ Sevimlilar</h2></div>
      <div id="favoritesGrid" class="catalog" style="padding:0"></div>
      <button class="supportBtn" onclick="showView('support')">🛟 AIKINOUZ SUPPORT <span style="float:right">›</span></button>
    </div>
  </main>

  <main id="support" class="view">
    <div class="pageTop"><button class="back" onclick="showView('profile')">‹</button><div class="pageTitle">AIKINOUZ Support</div></div>
    <section class="supportCard">
      <div class="supportTitle">👑 AIKINOUZ</div>
      <div class="sub" style="margin-top:3px">PREMIUM KINO PLATFORMASI</div>
      <div class="supportMeta">
        <div>🏢 <b>Kompaniya:</b> AIKINOUZ</div>
        <div>👑 <b>Kompaniya prezidenti:</b><br>BOBURMIRZO GAZIEV MAKHAMMATTOLIBJON UGLI</div>
        <div>📧 <b>Email:</b> boburshox1311m@gmail.com</div>
        <div>🧩 <b>Project:</b> AIKINOUZ / AIKINO_UZ_BOT</div>
        <div>© 2026 AIKINOUZ. All rights reserved.</div>
        <div><b>Project owner / author:</b><br>BOBURMIRZO GAZIEV MAKHAMMATTOLIBJON UGLI</div>
      </div>
      <div class="supportForm">
        <h3>💬 Adminga yozish</h3>
        <textarea id="supportMessage" maxlength="2000" placeholder="Savol, muammo yoki taklifingizni yozing..."></textarea>
        <button id="supportSendBtn" class="supportSend" onclick="sendSupport()">📨 XABARNI YUBORISH</button>
        <div id="supportStatus" class="supportNote">Xabaringiz AIKINOUZ adminiga to‘g‘ridan-to‘g‘ri yuboriladi.</div>
      </div>
    </section>
  </main>

  <nav class="bottom">
    <button class="nav active" data-view="home" onclick="showView('home')"><b>⌂</b>Bosh sahifa</button>
    <button class="nav" data-view="catalog" onclick="showView('catalog')"><b>▦</b>Katalog</button>
    <button class="nav" data-view="vip" onclick="showView('vip')"><b>♛</b>VIP</button>
    <button class="nav" data-view="catalog" onclick="showView('catalog');document.getElementById('searchInput').focus()"><b>⌕</b>Qidiruv</button>
    <button class="nav" data-view="profile" onclick="showView('profile')"><b>●</b>Profilim</button>
  </nav>
</div>
<script>
const tg=window.Telegram?.WebApp;
if(tg){
  tg.ready();
  tg.expand();
  try{tg.setHeaderColor('#050505')}catch(e){}
  try{tg.setBackgroundColor('#050505')}catch(e){}
  try{tg.setBottomBarColor('#080808')}catch(e){}
  try{
    if(typeof tg.requestFullscreen==='function') tg.requestFullscreen();
  }catch(e){
    tg.expand();
  }
}
function closeApp(){
  if(tg?.close) tg.close();
  else history.back();
}
const initData=tg?.initData||''; let movies=[]; let me={authenticated:false,favorite_ids:[]};
function syncViewport(){
  const h=tg?.viewportStableHeight||tg?.viewportHeight||window.innerHeight;
  document.documentElement.style.setProperty('--app-height',h+'px');
}
syncViewport();
if(tg?.onEvent){
  tg.onEvent('viewportChanged',syncViewport);
  tg.onEvent('fullscreenChanged',syncViewport);
}
const api=(url,opt={})=>fetch(url,{...opt,headers:{...(opt.headers||{}),'X-Telegram-Init-Data':initData}}).then(r=>r.json());

function badgeHTML(m){let out='';if(m.badge)out+=`<span class="badge">${m.badge}</span>`;if(m.is_vip)out+=`<span class="badge vip">VIP</span>`;return out}
function cardHTML(m){return `<div class="card" onclick="openMovie(${m.id})"><div class="poster">${m.poster_url?`<img src="${m.poster_url}" loading="lazy">`:'<div class="posterFallback">🎬</div>'}${badgeHTML(m)}</div><div class="title">${escapeHtml(m.title)}</div><div class="meta">👁 ${m.views.toLocaleString()} · ${m.episode_count} qism</div></div>`}
function escapeHtml(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function showView(id){document.querySelectorAll('.view').forEach(x=>x.classList.remove('active'));document.getElementById(id).classList.add('active');document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x.dataset.view===id));window.scrollTo(0,0);if(id==='profile')renderProfile()}
function renderHome(){const trend=movies.filter(m=>m.views>=1000).sort((a,b)=>b.views-a.views).slice(0,8);document.getElementById('trendRow').innerHTML=(trend.length?trend:movies.slice(0,6)).map(cardHTML).join('');document.getElementById('newRow').innerHTML=movies.filter(m=>!m.is_vip).slice(0,8).map(cardHTML).join('');document.getElementById('vipGrid').innerHTML=movies.filter(m=>m.is_vip).map(cardHTML).join('')||'<div class="empty">VIP kinolar hozircha qo‘shilmagan.</div>'}
function renderCatalog(){const q=(document.getElementById('searchInput')?.value||'').toLowerCase().trim();const list=movies.filter(m=>!q||m.title.toLowerCase().includes(q));document.getElementById('catalogGrid').innerHTML=list.map(cardHTML).join('')||'<div class="empty">Kino topilmadi.</div>'}
async function openMovie(id){showView('detail');document.getElementById('detailContent').innerHTML='<div class="empty">Yuklanmoqda...</div>';const m=await api('/app/api/movie/'+id);const fav=me.favorite_ids?.includes(m.id);document.getElementById('detailContent').innerHTML=`<div class="detailHero">${m.poster_url?`<img src="${m.poster_url}">`:'<div class="posterFallback" style="height:360px">🎬</div>'}</div><div class="detailBody"><h1>${escapeHtml(m.title)}</h1><div class="chips"><span class="chip">👁 ${m.views.toLocaleString()} ko‘rish</span><span class="chip">🎞 ${m.episode_count} qism</span>${m.badge?`<span class="chip">🔥 ${m.badge}</span>`:''}${m.is_vip?'<span class="chip">💎 VIP</span>':''}</div><div class="desc">${escapeHtml(m.description)||'AIKINOUZ kino kolleksiyasi.'}</div><div class="actions"><button class="goldBtn" onclick="openFirstEpisode(${m.id},${m.episodes[0]?.id||0})">▶ Tomosha qilish</button><button class="darkBtn" onclick="toggleFavorite(${m.id})">${fav?'♥ Sevimlida':'♡ Sevimlilar'}</button></div><h3>Qismlar</h3><div class="episodes">${m.episodes.map(e=>`<div class="episode"><span><b>${e.number}-QISM</b></span><button onclick="openEpisode(${e.id})">▶ Ochish</button></div>`).join('')||'<div class="empty">Qismlar yo‘q.</div>'}</div></div>`}
}
function openEpisode(id){if(!id)return;const url='https://t.me/AIKINO_UZ_BOT?start=ep_'+id;if(tg?.openTelegramLink)tg.openTelegramLink(url);else location.href=url}
function openFirstEpisode(movieId,id){if(id)openEpisode(id)}
async function toggleFavorite(id){if(!initData){alert('Sevimlilar Telegram ichida ishlaydi.');return}const r=await api('/app/api/favorite/'+id,{method:'POST'});if(r.error==='vip_required'){alert('Bu kino uchun VIP kerak.');return}if(r.favorite&&!me.favorite_ids.includes(id))me.favorite_ids.push(id);if(!r.favorite)me.favorite_ids=me.favorite_ids.filter(x=>x!==id);openMovie(id)}
async function renderProfile(){if(!me.authenticated){document.getElementById('profileCard').innerHTML='<div class="profileName">Telegram ichida oching</div><div class="status">Profil va sevimlilar uchun Mini App bot ichidan ochilishi kerak.</div>';document.getElementById('favoritesGrid').innerHTML='';return}const u=me.user;document.getElementById('profileCard').innerHTML=`<div class="profileName">${escapeHtml((u.first_name||'')+' '+(u.last_name||''))}</div><div class="status">${me.vip?'💎 VIP ACTIVE':'✨ STANDARD'}${me.continue?` · ▶ ${escapeHtml(me.continue.movie_title)} ${me.continue.episode_number}-qism`:''}</div>`;document.getElementById('favoritesGrid').innerHTML=movies.filter(m=>me.favorite_ids.includes(m.id)).map(cardHTML).join('')||'<div class="empty">Hozircha sevimli kinolar yo‘q.</div>'}
async function sendSupport(){
  const box=document.getElementById('supportMessage');
  const btn=document.getElementById('supportSendBtn');
  const status=document.getElementById('supportStatus');
  const message=(box?.value||'').trim();
  if(!initData){status.textContent='Support Telegram ichida ochilganda ishlaydi.';return}
  if(message.length<3){status.textContent='Xabarni biroz to‘liqroq yozing.';return}
  btn.disabled=true;btn.textContent='⏳ YUBORILMOQDA...';status.textContent='Xabar yuborilmoqda...';
  try{
    const r=await api('/app/api/support',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message})});
    if(r.ok){
      box.value='';
      status.textContent='✅ Xabaringiz adminga yuborildi.';
      if(tg?.HapticFeedback) try{tg.HapticFeedback.notificationOccurred('success')}catch(e){}
    }else{
      status.textContent='Xabar yuborilmadi. Qayta urinib ko‘ring.';
    }
  }catch(e){
    status.textContent='Xabar yuborilmadi. Internetni tekshirib qayta urinib ko‘ring.';
  }finally{
    btn.disabled=false;btn.textContent='📨 XABARNI YUBORISH';
  }
}
async function boot(){const [catalog,user]=await Promise.all([api('/app/api/catalog'),api('/app/api/me')]);movies=catalog.movies||[];me=user||{authenticated:false,favorite_ids:[]};if(me.authenticated)document.getElementById('avatar').textContent=(me.user.first_name||'A')[0].toUpperCase();renderHome();renderCatalog()}
boot().catch(()=>{document.getElementById('catalogGrid').innerHTML='<div class="empty">Ma’lumot yuklanmadi. Qayta ochib ko‘ring.</div>'})
</script>
</body></html>"""
