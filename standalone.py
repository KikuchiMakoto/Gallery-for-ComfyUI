# -*- coding: utf-8 -*-
"""
Comfinder · スタンドアロン起動ランチャー
========================================
ComfyUI 本体を起動せずに Comfinder サーバーを単体起動します。

使い方:
    python standalone.py [--port 8288] [--output <ComfyUI output ディレクトリ>]
"""
import os, sys, json, subprocess, zipfile, datetime, asyncio, time, tempfile, re
from types import SimpleNamespace

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PLUGIN_DIR)

import gallery_core as G

# ---------------------------------------------------------------------------
# パラメータ解析
# ---------------------------------------------------------------------------
PORT = 8288
HOST = "127.0.0.1"
COMFY_OUTPUT = None
COMFY_MODELS = None
argv = sys.argv[1:]
i = 0
while i < len(argv):
    if argv[i] == "--port" and i + 1 < len(argv):
        PORT = int(argv[i + 1]); i += 2; continue
    if argv[i] == "--host" and i + 1 < len(argv):
        HOST = argv[i + 1]; i += 2; continue
    if argv[i] == "--output" and i + 1 < len(argv):
        COMFY_OUTPUT = argv[i + 1]; i += 2; continue
    if argv[i] == "--models" and i + 1 < len(argv):
        COMFY_MODELS = argv[i + 1]; i += 2; continue
    if argv[i] in ("-h", "--help"):
        print("用法: python standalone.py --output <ComfyUI/output> "
              "[--models <ComfyUI/models>] [--host 127.0.0.1] [--port 8288]")
        print("環境変数 COMFINDER_OUTPUT / COMFINDER_MODELS も利用可能です")
        sys.exit(0)
    i += 1

# ---------------------------------------------------------------------------
# ComfyUI output ディレクトリ（folder_paths 代替）
# ---------------------------------------------------------------------------
_CANDIDATES = [
    COMFY_OUTPUT,
    os.environ.get("COMFINDER_OUTPUT", ""),
    os.environ.get("GALLERY_COMFY_OUTPUT", ""),
]
COMFY_OUTPUT = next((p for p in _CANDIDATES if p and os.path.isdir(p)), "")
if not COMFY_OUTPUT:
    print("[!] ComfyUI output ディレクトリが見つかりません。--output で指定してください:")
    print("    python standalone.py --output /path/to/ComfyUI/output")
    sys.exit(1)

# models ディレクトリ
MODELS_DIR = COMFY_MODELS or os.environ.get("COMFINDER_MODELS", "") or os.environ.get("GALLERY_COMFY_MODELS", "")
if not MODELS_DIR:
    _guess = os.path.join(os.path.dirname(os.path.abspath(COMFY_OUTPUT)), "models")
    MODELS_DIR = _guess if os.path.isdir(_guess) else ""
G.folder_paths = SimpleNamespace(
    get_output_directory=lambda: COMFY_OUTPUT,
    models_dir=MODELS_DIR,
)
print("[*] ComfyUI output:", COMFY_OUTPUT)
print("[*] ComfyUI models:", MODELS_DIR or "(未指定)")

# ---------------------------------------------------------------------------
# ルーティング（ComfyUI プラグインと同一）
# ---------------------------------------------------------------------------
from aiohttp import web

P = "/comfinder"
WEB_DIR = os.path.join(PLUGIN_DIR, "web")


def _int(v, default=None):
    try:
        return int(v) if v not in (None, "") else default
    except Exception:
        return default


def _float(v, default=None):
    try:
        return float(v) if v not in (None, "") else default
    except Exception:
        return default


def _json(resp, code=200):
    return web.json_response(resp, status=code, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def _safe_name(name):
    n = (name or "").replace("\\", "/").lstrip("/")
    parts = [p for p in n.split("/") if p not in ("", ".", "..") and not p.endswith(":")]
    return "/".join(parts)


def _pick_native_folder():
    """用 Win32 SHBrowseForFolder 弹出原生“选择文件夹”对话框（可新建文件夹），返回路径或空串。
    不经过 PowerShell（其 stdout 管道在模态对话框下会挂起），直接用 ctypes 调用，可靠。"""
    try:
        import ctypes
        from ctypes import POINTER, byref
        class BROWSEINFOW(ctypes.Structure):
            _fields_ = [("hwndOwner", ctypes.c_void_p), ("pidlRoot", ctypes.c_void_p),
                        ("pszDisplayName", ctypes.c_wchar_p), ("lpszTitle", ctypes.c_wchar_p),
                        ("ulFlags", ctypes.c_uint), ("lpfn", ctypes.c_void_p), ("lParam", ctypes.c_void_p),
                        ("iImage", ctypes.c_int)]
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # STA
        shell32 = ctypes.windll.shell32
        shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
        shell32.SHBrowseForFolderW.argtypes = [POINTER(BROWSEINFOW)]
        shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        bi = BROWSEINFOW()
        bi.hwndOwner = None
        bi.lpszTitle = "选择打包保存文件夹（可新建）"
        bi.ulFlags = 0x40  # BIF_NEWDIALOGSTYLE
        pidl = shell32.SHBrowseForFolderW(byref(bi))
        if pidl:
            buf = ctypes.create_unicode_buffer(260)
            if shell32.SHGetPathFromIDListW(pidl, buf):
                return buf.value.strip()
        return ""
    except Exception:
        return ""


app = web.Application()


async def page_root_redirect(request):
    raise web.HTTPFound(P + "/")


async def page_index(request):
    fp = os.path.join(WEB_DIR, "index.html")
    if os.path.isfile(fp):
        resp = web.FileResponse(fp)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    return web.Response(status=404, text="index.html not found")


def _pwa_file(filename, content_type=None, no_store=False, sw=False):
    fp = os.path.join(WEB_DIR, filename)
    if not os.path.isfile(fp):
        return web.Response(status=404, text=filename + " not found")
    resp = web.FileResponse(fp)
    if content_type:
        resp.content_type = content_type
    if no_store:
        resp.headers["Cache-Control"] = "no-store"
    if sw:
        resp.headers["Service-Worker-Allowed"] = P + "/"
    return resp


async def pwa_manifest(request):
    return _pwa_file("manifest.json", "application/manifest+json", no_store=True)


async def pwa_sw(request):
    return _pwa_file("sw.js", "application/javascript", no_store=True, sw=True)


async def pwa_icon_svg(request):
    return _pwa_file("icon.svg", "image/svg+xml")


async def pwa_favicon(request):
    return _pwa_file("favicon.ico", "image/x-icon")


async def pwa_icon_32(request):
    return _pwa_file("icon-32.png", "image/png")


async def pwa_icon_192(request):
    return _pwa_file("icon-192.png", "image/png")


async def pwa_icon_512(request):
    return _pwa_file("icon-512.png", "image/png")


async def pwa_apple_icon(request):
    return _pwa_file("apple-touch-icon.png", "image/png")


async def pwa_font(request):
    name = os.path.basename(request.match_info.get("name", ""))
    if name not in ("Iosevka-Regular.woff2", "Iosevka-Bold.woff2"):
        return web.Response(status=404, text="font not found")
    fp = os.path.join(WEB_DIR, "fonts", name)
    if not os.path.isfile(fp):
        return web.Response(status=404, text="font not found")
    return web.FileResponse(fp, headers={"Content-Type": "font/woff2", "Cache-Control": "public, max-age=31536000, immutable"})


async def api_settings(request):
    s = G.load_settings()
    return _json({"webui_root": s.get("webui_root", ""),
                  "configured": bool(s.get("webui_root")),
                  "artist_format": s.get("artist_format") or G.DEFAULT_ARTIST_FORMAT,
                  "scan_mode": s.get("scan_mode") or "incremental"})


async def api_settings_save(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    r = G.save_settings(data)
    return _json(r, 200 if r.get("ok") else 400)


async def api_scan(request):
    source = request.query.get("source", "comfyui")
    force = request.query.get("force", "0") == "1"
    refresh = request.query.get("refresh", "0") == "1"
    r = G.start_scan(source, force=force, refresh=refresh)
    return _json(r)


async def api_status(request):
    return _json({
        "comfyui": G.scan_status("comfyui"),
        "webui": G.scan_status("webui"),
        "video": G.scan_status("video"),
        "settings": G.load_settings(),
    })


async def api_gallery(request):
    q = request.query
    source = q.get("source", "comfyui")
    r = G.query(
        source,
        q=q.get("q", ""),
        model=q.get("model", ""),
        sampler=q.get("sampler", ""),
        lora=q.get("lora", ""),
        steps_min=_int(q.get("steps_min")),
        steps_max=_int(q.get("steps_max")),
        cfg_min=_float(q.get("cfg_min")),
        cfg_max=_float(q.get("cfg_max")),
        min_w=_int(q.get("min_w")),
        min_h=_int(q.get("min_h")),
        fav_only=q.get("fav", "0") == "1",
        sort=q.get("sort", "newest"),
        seed=q.get("seed", ""),
        page=_int(q.get("page"), 1),
        page_size=min(_int(q.get("page_size"), 60) or 60, 1000),
        tags=q.get("tags", ""),
        artist=q.get("artist", ""),
        artists_all=[x for x in q.get("chain_all", "").split(",") if x.strip()],
        artists_any=[x for x in q.get("chain_any", "").split(",") if x.strip()],
        artist_min=_int(q.get("artist_min")),
        rating=q.get("rating", ""),
    )
    return _json(r)


async def api_gallery_random(request):
    q = request.query
    source = q.get("source", "comfyui")
    r = G.query_random(
        source,
        q=q.get("q", ""),
        model=q.get("model", ""),
        sampler=q.get("sampler", ""),
        lora=q.get("lora", ""),
        steps_min=_int(q.get("steps_min")),
        steps_max=_int(q.get("steps_max")),
        cfg_min=_float(q.get("cfg_min")),
        cfg_max=_float(q.get("cfg_max")),
        min_w=_int(q.get("min_w")),
        min_h=_int(q.get("min_h")),
        fav_only=q.get("fav", "0") == "1",
        tags=q.get("tags", ""),
        artist=q.get("artist", ""),
        artists_all=[x for x in q.get("chain_all", "").split(",") if x.strip()],
        artists_any=[x for x in q.get("chain_any", "").split(",") if x.strip()],
        artist_min=_int(q.get("artist_min")),
        rating=q.get("rating", "rated"),
        count=min(_int(q.get("count"), 20) or 20, 100),
        exclude=_safe_name(q.get("exclude", "")),
    )
    return _json(r)


async def api_artlib_status(request):
    return _json(G.artlib_status())


async def api_artlib_install(request):
    url = ""
    try:
        data = await request.json()
        url = (data or {}).get("url", "")
    except Exception:
        pass
    return _json(G.artlib_install(url))


async def api_artlib_facets(request):
    try:
        G._artlib_gc(_float(request.query.get("gc"), 0) or 0)
    except Exception:
        pass
    return _json(G.artlib_facets())


async def api_artlib_search(request):
    q = request.query
    try:
        G._artlib_gc(_float(q.get("gc"), 0) or 0)
    except Exception:
        pass
    r = G.artlib_search(
        q=q.get("q", ""), archetype=q.get("archetype", ""), origin=q.get("origin", ""),
        subject=q.get("subject", ""), theme=q.get("theme", ""),
        technique=q.get("technique", ""), composition=q.get("composition", ""),
        rating=q.get("rating", ""), min_posts=_int(q.get("min_posts"), 0) or 0,
        sort=q.get("sort", "posts"), page=_int(q.get("page"), 1) or 1,
        page_size=_int(q.get("page_size"), 60) or 60)
    return _json({"items": r["items"], "total": r["total"]})


async def api_artlib_preview(request):
    q = request.query
    names = [n for n in q.get("names", "").split(",") if n.strip()]
    return _json(G.artlib_preview(source=q.get("source", "comfyui"), names=names))


async def api_artlib_release(request):
    return _json(G.artlib_release())


async def api_models(request):
    source = request.query.get("source", "comfyui")
    return _json({"items": G.models(source)})


async def api_samplers(request):
    source = request.query.get("source", "comfyui")
    return _json({"items": G.samplers(source)})


async def api_loras(request):
    source = request.query.get("source", "comfyui")
    return _json({"items": G.loras(source)})


async def api_stats(request):
    return _json(G.stats())


async def api_fav_list(request):
    return _json({"items": [{"source": a, "file": b} for a, b in G._fav_set()]})


async def api_fav_toggle(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    r = G.save_fav(data.get("source", "comfyui"), _safe_name(data.get("file", "")), bool(data.get("on", True)))
    return _json(r)


async def api_delete(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    r = G.delete_image(data.get("source", "comfyui"), _safe_name(data.get("file", "")))
    return _json(r, 200 if r.get("ok") else 404)


async def api_restore(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    r = G.restore_image(
        data.get("source", "comfyui"),
        _safe_name(data.get("file", "")),
        trash_rel=data.get("trash_rel", ""),
        prev_rating=data.get("prev_rating"),
        prev_folders=data.get("prev_folders"),
    )
    return _json(r, 200 if r.get("ok") else 400)


async def api_ratings(request):
    source = request.query.get("source", "comfyui")
    return _json({"ratings": G.load_ratings(), "counts": G.rating_counts(source)})


async def api_rate(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    r = G.rate_image(
        data.get("source", "comfyui"),
        _safe_name(data.get("file", "")),
        data.get("rating", 0),
    )
    return _json(r, 200 if r.get("ok") else 400)


async def api_trash_info(request):
    return _json(G.trash_info())


async def api_trash_purge(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    items = [
        {"source": it.get("source", "comfyui"), "file": _safe_name(it.get("file", ""))}
        for it in (data.get("items") or []) if isinstance(it, dict)
    ]
    return _json(G.purge_trashed(items))


async def api_trash_empty(request):
    return _json(G.empty_trash())


async def api_events(request):
    since = _int(request.query.get("since"), 0) or 0
    timeout = min(max(_float(request.query.get("timeout"), 20.0) or 20.0, 0.0), 25.0)
    G.touch_poll()
    cur, evs = G.events_since(since)
    if evs or timeout <= 0:
        return _json({"ok": True, "seq": cur, "events": evs, "counts": G.rating_counts("comfyui")})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        G.touch_poll()
        await asyncio.sleep(0.35)
        cur, evs = G.events_since(since)
        if evs:
            return _json({"ok": True, "seq": cur, "events": evs, "counts": G.rating_counts("comfyui")})
    return _json({"ok": True, "seq": cur, "events": []})


async def api_check_new(request):
    items = await asyncio.to_thread(G.check_new_images, "comfyui")
    return _json({"ok": True, "added": len(items), "counts": G.rating_counts("comfyui")})


async def ws_gallery(request):
    ws = web.WebSocketResponse(heartbeat=25.0)
    await ws.prepare(request)
    loop = asyncio.get_running_loop()
    q = asyncio.Queue(maxsize=64)
    cid = G.ws_register(loop, q)
    cur_seq, _ = G.events_since(0)
    try:
        await ws.send_json({
            "type": "hello",
            "event_seq": cur_seq,
            "rating_counts": G.rating_counts("comfyui"),
        })

        async def _pump():
            while not ws.closed:
                msg = await q.get()
                if ws.closed:
                    break
                await ws.send_json(msg)

        pump_task = asyncio.create_task(_pump())
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    txt = (msg.data or "").strip()
                    if txt == "ping":
                        await ws.send_json({"type": "pong", "ts": time.time()})
                    elif txt == "check":
                        await asyncio.to_thread(G.check_new_images, "comfyui")
                elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
        finally:
            pump_task.cancel()
            try:
                await pump_task
            except BaseException:
                pass
    except Exception:
        pass
    finally:
        G.ws_unregister(cid)
    return ws


async def api_artists(request):
    source = request.query.get("source", "comfyui")
    sort = request.query.get("sort", "count")
    search = request.query.get("search", "")
    page = _int(request.query.get("page"), 1) or 1
    page_size = _int(request.query.get("page_size"), 0) or 0
    fav_only = request.query.get("fav", "0") == "1"
    try:
        r = G.artists(source, sort=sort, search=search, page=page, page_size=page_size,
                      fav_only=fav_only)
        return _json({"items": r["items"], "total": r["total"]})
    except Exception as e:
        return _json({"items": [], "total": 0, "error": str(e)})


async def api_artist_covers(request):
    try:
        raw = request.query.get("names", "")
        names = [x for x in raw.split(",") if x.strip()]
        return _json({"covers": G.artist_covers(names)})
    except Exception as e:
        return _json({"covers": {}, "error": str(e)})


async def api_chains(request):
    try:
        return _json({"chains": G.chain_list(), "artists": G.chain_artists_flat()})
    except Exception as e:
        return _json({"chains": [], "artists": [], "error": str(e)})


async def api_chains_save(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        return _json(G.chain_fav_save(data or {}))
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


async def api_chains_delete(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        return _json(G.chain_fav_delete((data or {}).get("id", "")))
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


async def api_artist_tags(request):
    source = request.query.get("source", "comfyui")
    tag = request.query.get("tag", "")
    try:
        mx = int(request.query.get("max", "400"))
    except Exception:
        mx = 400
    try:
        lim = int(request.query.get("limit", "40"))
    except Exception:
        lim = 40
    try:
        items = G.artist_top_tags(source, tag, max_show=mx, limit=lim)
        return _json({"items": items})
    except Exception as e:
        return _json({"items": [], "error": str(e)})


async def api_artist_note(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    return _json(G.save_artist_note(data.get("tag", ""), data.get("note", "")))


async def api_artist_fav(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    return _json(G.save_artist_fav(data.get("tag", ""), bool(data.get("on", True))))


async def api_artist_cover(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    return _json(G.save_artist_cover(data.get("tag", ""), _safe_name(data.get("file", ""))))


async def api_artist_weight(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        w = float(data.get("weight", 1.0))
    except Exception:
        w = 1.0
    return _json(G.save_artist_weight(data.get("tag", ""), w))


async def api_image(request):
    source = request.query.get("source", "comfyui")
    file = _safe_name(request.query.get("file", ""))
    t_arg = request.query.get("thumb", "").strip().lower()
    if t_arg in ("1", "true", "webp", "xs", "s", "m", "l", "xl"):
        tier = t_arg if t_arg in G.THUMB_TIERS else G.DEFAULT_THUMB_TIER
        tp = await asyncio.to_thread(G.ensure_thumb, source, file, tier)
        if tp and os.path.isfile(tp):
            cc = "private, max-age=604800, immutable" if request.query.get("v") else "private, max-age=3600"
            return web.FileResponse(tp, headers={"Content-Type": "image/webp", "Cache-Control": cc})
    fp = G.image_path(source, file)
    if not fp:
        return _json({"error": "not found"}, 404)
    return web.FileResponse(fp)


async def api_open_folder(request):
    source = request.query.get("source", "comfyui")
    file = _safe_name(request.query.get("file", ""))
    fp = G.image_path(source, file)
    if not fp:
        return _json({"error": "not found"}, 404)
    try:
        subprocess.Popen(["explorer", "/select,", fp])
        return _json({"ok": True})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


async def api_meta(request):
    source = request.query.get("source", "comfyui")
    file = _safe_name(request.query.get("file", ""))
    return _json({"metadata": G.raw_metadata(source, file)})


async def api_boot(request):
    results = {}
    full = (G.load_settings().get("scan_mode") or "incremental") == "full"
    for src in ("comfyui",):
        results[src] = G.start_scan(src, force=full, refresh=not full)
    G.ensure_danbooru_cron()
    return _json({"ok": True, "booted": results, "scan_mode": "full" if full else "incremental"})


async def api_danbooru_status(request):
    return _json(G.danbooru_cron_status())


async def api_danbooru_sync(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    force = bool((data or {}).get("force", True))
    return _json(G.trigger_danbooru_sync(force=force))


async def api_danbooru_tags(request):
    search = request.query.get("search", "")
    limit = min(_int(request.query.get("limit"), 50) or 50, 500)
    return _json({"items": G.danbooru_tags_search(search=search, limit=limit)})


async def api_tags(request):
    source = request.query.get("source", "comfyui")
    search = request.query.get("search", "")
    limit = min(_int(request.query.get("limit"), 200) or 200, 1000)
    return _json({"items": G.tag_stats(source, search=search, limit=limit)})


async def api_blacklist_get(request):
    return _json({"items": G.load_blacklist()})


async def api_blacklist_save(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    return _json(G.save_blacklist(data.get("items", [])))


async def api_folders_get(request):
    return _json({"folders": G.folder_list()})


async def api_folders_images(request):
    name = request.query.get("folder", "")
    source = request.query.get("source", "")
    return _json({"items": G.folder_images(name, source)})


async def api_folders_of(request):
    source = request.query.get("source", "comfyui")
    file = _safe_name(request.query.get("file", ""))
    return _json({"folders": G.folder_contains(source, file)})


async def api_folders_manage(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    action = data.get("action")
    if action == "create":
        return _json(G.folder_create(data.get("name", "")))
    if action == "delete":
        return _json(G.folder_delete(data.get("name", "")))
    if action == "rename":
        return _json(G.folder_rename(data.get("old", ""), data.get("new", "")))
    if action == "add":
        return _json(G.folder_add(data.get("folder", ""), data.get("items", [])))
    if action == "remove":
        return _json(G.folder_remove(data.get("folder", ""), data.get("items", [])))
    return _json({"ok": False, "error": "未知操作"}, 400)


async def api_fs_drives(request):
    return _json({"drives": G.list_drives()})


async def api_fs_list(request):
    path = request.query.get("path", "")
    r = G.list_dir(path)
    return _json(r, 200 if "error" not in r else 400)


async def api_fs_mkdir(request):
    """在当前浏览目录下新建文件夹（打包保存用）"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    parent = (data.get("path") or "").strip()
    name = (data.get("name") or "").strip()
    if not parent or not name:
        return _json({"error": "缺少路径或名称"}, 400)
    if not os.path.isdir(parent):
        return _json({"error": "父目录不存在"}, 400)
    name = name.replace("\\", "/").replace("/", "")
    if not name:
        return _json({"error": "名称无效"}, 400)
    target = os.path.join(parent, name)
    try:
        os.makedirs(target, exist_ok=True)
        return _json({"ok": True, "path": target})
    except Exception as e:
        return _json({"ok": False, "error": str(e)}, 500)


async def api_fs_open_dir(request):
    """在资源管理器中打开一个目录"""
    path = request.query.get("path", "").strip()
    if not path or not os.path.isdir(path):
        return _json({"error": "目录不存在"}, 400)
    try:
        subprocess.Popen(["explorer", path])
        return _json({"ok": True})
    except Exception as e:
        return _json({"ok": False, "error": str(e)}, 500)


async def api_pick_dir(request):
    """弹出 Windows 原生“选择文件夹”对话框（可新建文件夹），返回选中路径"""
    path = await asyncio.to_thread(_pick_native_folder)
    if path and os.path.isdir(path):
        return _json({"path": path})
    return _json({"path": ""})


async def api_zip(request):
    """打包图片到用户选择的目标文件夹（打包在后台线程执行，不阻塞服务）。
    body: {target, items} 选中打包 或 {target, query} 筛选结果整体打包；
    by_model=true 按大模型分子目录；name=指定 zip 文件名（默认时间戳）。"""
    try:
        data = await request.json()
    except Exception:
        return _json({"error": "bad json"}, 400)
    resp = await asyncio.to_thread(_pack_images, data)
    code = resp.pop("_code", 200)
    return _json(resp, code)


def _pack_images(data):
    """同步执行：收集文件 + 写 zip（在后台线程运行）。返回 resp dict"""
    target = (data.get("target") or "").strip()
    if not target or not os.path.isdir(target):
        return {"error": "目标目录不存在", "_code": 400}
    by_model = bool(data.get("by_model", False))
    entries = []  # (磁盘路径, 子目录)
    n_candidates = 0
    if data.get("items"):
        for it in data["items"]:
            fp = G.image_path(it.get("source", "comfyui"), _safe_name(it.get("file", "")))
            if fp:
                entries.append((fp, ""))
    elif data.get("query"):
        q = data["query"]
        r = G.query(
            q.get("source", "comfyui"),
            q=q.get("q", ""), model=q.get("model", ""), sampler=q.get("sampler", ""),
            lora=q.get("lora", ""),
            steps_min=_int(q.get("steps_min")), steps_max=_int(q.get("steps_max")),
            cfg_min=_float(q.get("cfg_min")), cfg_max=_float(q.get("cfg_max")),
            min_w=_int(q.get("min_w")), min_h=_int(q.get("min_h")),
            fav_only=q.get("fav", "0") == "1", tags=q.get("tags", ""),
            sort="newest", page=1, page_size=10000,
        )
        n_candidates = len(r.get("items", []))
        for it in r.get("items", []):
            fp = G.image_path(q.get("source", "comfyui"), _safe_name(it.get("file", "")))
            if fp:
                sub = str(it.get("model") or "未分类") if by_model else ""
                entries.append((fp, sub))
    else:
        return {"error": "no items", "_code": 400}
    if not entries:
        msg = "没有可打包的图片"
        if n_candidates:
            msg += "（候选 %d 个，磁盘均找不到文件）" % n_candidates
        return {"error": msg, "_code": 400}
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    zname = (data.get("name") or "").strip()
    if zname:
        safe_n = re.sub(r'[\\/:*?"<>|]', "_", zname).strip() or "gallery_export"
        zpath = os.path.join(target, safe_n + ".zip")
        if os.path.exists(zpath):
            zpath = os.path.join(target, "%s_%s.zip" % (safe_n, ts))
    else:
        zpath = os.path.join(target, "gallery_export_%s.zip" % ts)
    try:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            seen = set()
            for fp, sub in entries:
                name = os.path.basename(fp)
                if name in seen:
                    name = "%d_%s" % (len(seen), name)
                seen.add(name)
                if sub:
                    safe = re.sub(r'[\\/:*?"<>|]', "_", sub).strip() or "未分类"
                    z.write(fp, os.path.join(safe, name))
                else:
                    z.write(fp, name)
        return {"ok": True, "zip": zpath, "count": len(entries), "_code": 200}
    except Exception as e:
        return {"ok": False, "error": str(e), "_code": 500}


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
app.router.add_get(P + "/api/fs/list", api_fs_list)
app.router.add_post(P + "/api/fs/mkdir", api_fs_mkdir)
app.router.add_get(P + "/api/fs/open-dir", api_fs_open_dir)
app.router.add_post(P + "/api/fs/pick-dir", api_pick_dir)
app.router.add_post(P + "/api/zip", api_zip)
app.router.add_get(P + "/api/fs/drives", api_fs_drives)
app.router.add_post(P + "/api/folders", api_folders_manage)
app.router.add_get(P + "/api/folders/of", api_folders_of)
app.router.add_get(P + "/api/folders/images", api_folders_images)
app.router.add_get(P + "/api/folders", api_folders_get)
app.router.add_post(P + "/api/blacklist", api_blacklist_save)
app.router.add_get(P + "/api/blacklist", api_blacklist_get)
app.router.add_get(P + "/api/tags", api_tags)
app.router.add_get(P + "/api/boot", api_boot)
app.router.add_get(P + "/api/meta", api_meta)
app.router.add_get(P + "/api/image/open-folder", api_open_folder)
app.router.add_get(P + "/api/image", api_image)
app.router.add_post(P + "/api/fav", api_fav_toggle)
app.router.add_get(P + "/api/fav", api_fav_list)
app.router.add_get(P + "/api/stats", api_stats)
app.router.add_get(P + "/api/loras", api_loras)
app.router.add_get(P + "/api/samplers", api_samplers)
app.router.add_get(P + "/api/models", api_models)
app.router.add_get(P + "/api/gallery/random", api_gallery_random)
app.router.add_get(P + "/api/gallery", api_gallery)
app.router.add_get(P + "/api/status", api_status)
app.router.add_get(P + "/api/scan", api_scan)
app.router.add_post(P + "/api/settings", api_settings_save)
app.router.add_get(P + "/api/settings", api_settings)
app.router.add_get(P + "/api/artlib/status", api_artlib_status)
app.router.add_post(P + "/api/artlib/install", api_artlib_install)
app.router.add_get(P + "/api/artlib/facets", api_artlib_facets)
app.router.add_get(P + "/api/artlib/search", api_artlib_search)
app.router.add_get(P + "/api/artlib/preview", api_artlib_preview)
app.router.add_post(P + "/api/artlib/release", api_artlib_release)
app.router.add_get(P + "/api/artists/tags", api_artist_tags)
app.router.add_post(P + "/api/artists/note", api_artist_note)
app.router.add_post(P + "/api/artists/fav", api_artist_fav)
app.router.add_post(P + "/api/artists/cover", api_artist_cover)
app.router.add_post(P + "/api/artists/weight", api_artist_weight)
app.router.add_get(P + "/api/artists", api_artists)
app.router.add_get(P + "/api/artist/covers", api_artist_covers)
app.router.add_get(P + "/api/chains", api_chains)
app.router.add_post(P + "/api/chains/save", api_chains_save)
app.router.add_post(P + "/api/chains/delete", api_chains_delete)
app.router.add_post(P + "/api/delete", api_delete)
app.router.add_post(P + "/api/restore", api_restore)
app.router.add_get(P + "/api/ratings", api_ratings)
app.router.add_post(P + "/api/rate", api_rate)
app.router.add_get(P + "/api/trash", api_trash_info)
app.router.add_post(P + "/api/trash/purge", api_trash_purge)
app.router.add_post(P + "/api/trash/empty", api_trash_empty)
app.router.add_get(P + "/api/events", api_events)
app.router.add_post(P + "/api/check", api_check_new)
app.router.add_get(P + "/api/danbooru/status", api_danbooru_status)
app.router.add_post(P + "/api/danbooru/sync", api_danbooru_sync)
app.router.add_get(P + "/api/danbooru/tags", api_danbooru_tags)
app.router.add_get(P + "/ws", ws_gallery)
app.router.add_get(P + "/manifest.json", pwa_manifest)
app.router.add_get(P + "/sw.js", pwa_sw)
app.router.add_get(P + "/icon.svg", pwa_icon_svg)
app.router.add_get(P + "/favicon.ico", pwa_favicon)
app.router.add_get("/favicon.ico", pwa_favicon)
app.router.add_get(P + "/icon-32.png", pwa_icon_32)
app.router.add_get(P + "/icon-192.png", pwa_icon_192)
app.router.add_get(P + "/icon-512.png", pwa_icon_512)
app.router.add_get(P + "/apple-touch-icon.png", pwa_apple_icon)
app.router.add_get(P + "/fonts/{name}", pwa_font)
app.router.add_get(P, page_root_redirect)
app.router.add_get(P + "/", page_index)
app.router.add_get("/gallery4comfyui", page_root_redirect)
app.router.add_get("/gallery4comfyui/", page_root_redirect)
app.router.add_get("/", page_root_redirect)


if __name__ == "__main__":
    print("[*] Comfinder スタンドアロンサーバー起動中...")
    print("[*] URL: http://%s:%d%s/" % (HOST, PORT, P))
    G.ensure_danbooru_cron()
    web.run_app(app, host=HOST, port=PORT, print=None)
