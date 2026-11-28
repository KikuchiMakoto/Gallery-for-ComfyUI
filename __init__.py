# -*- coding: utf-8 -*-
"""
Comfinder —— ComfyUI カスタムノード・高速スワイプ＆ギャラリー WebApp
====================================================================
- ComfyUI キャンバスに「Comfinder」ノードを追加 → 「Comfinder を開く」ボタン → 新規タブで開く
- スワイプ仕分け（未評価 / ★1〜3 / ゴミ箱）とギャラリー検索・閲覧を ComfyUI プロセス内で提供
"""
import os, json, threading, zipfile, datetime, asyncio, time, tempfile, re, heapq, random

from server import PromptServer
from aiohttp import web

from . import gallery_core as G

WEB_DIRECTORY = "./js"

# ---------------------------------------------------------------------------
# ノード
# ---------------------------------------------------------------------------
class ComfinderOpener:
    """Comfinder オープナー：フロントエンド JS から「Comfinder を開く」ボタンを提供"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "webui_url": ("STRING", {"default": ""}),
            }
        }

    RETURN_TYPES = ()
    FUNCTION = "noop"
    OUTPUT_NODE = True
    CATEGORY = "Comfinder"

    def noop(self, webui_url):
        return ()


NODE_CLASS_MAPPINGS = {
    "ComfinderOpener": ComfinderOpener,
    "Gallery4ComfyUIOpener": ComfinderOpener,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "ComfinderOpener": "Comfinder",
    "Gallery4ComfyUIOpener": "Comfinder",
}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]


# ---------------------------------------------------------------------------
# ルーティング
# ---------------------------------------------------------------------------
P = "/comfinder"
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


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
    """安全化相对路径：去掉绝对路径/盘符/.. 前缀，保留子目录（如 txt2img-images/xx.png）"""
    n = (name or "").replace("\\", "/").lstrip("/")
    parts = [p for p in n.split("/") if p not in ("", ".", "..") and not p.endswith(":")]
    return "/".join(parts)


@PromptServer.instance.routes.get(P)
async def page_index_redirect(request):
    raise web.HTTPFound(P + "/")


@PromptServer.instance.routes.get("/gallery4comfyui")
@PromptServer.instance.routes.get("/gallery4comfyui/")
async def legacy_index_redirect(request):
    raise web.HTTPFound(P + "/")


@PromptServer.instance.routes.get(P + "/")
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


@PromptServer.instance.routes.get(P + "/manifest.json")
async def pwa_manifest(request):
    return _pwa_file("manifest.json", "application/manifest+json", no_store=True)


@PromptServer.instance.routes.get(P + "/sw.js")
async def pwa_sw(request):
    return _pwa_file("sw.js", "application/javascript", no_store=True, sw=True)


@PromptServer.instance.routes.get(P + "/icon.svg")
async def pwa_icon_svg(request):
    return _pwa_file("icon.svg", "image/svg+xml")


@PromptServer.instance.routes.get(P + "/favicon.ico")
async def pwa_favicon(request):
    return _pwa_file("favicon.ico", "image/x-icon")


@PromptServer.instance.routes.get(P + "/icon-32.png")
async def pwa_icon_32(request):
    return _pwa_file("icon-32.png", "image/png")


@PromptServer.instance.routes.get(P + "/icon-192.png")
async def pwa_icon_192(request):
    return _pwa_file("icon-192.png", "image/png")


@PromptServer.instance.routes.get(P + "/icon-512.png")
async def pwa_icon_512(request):
    return _pwa_file("icon-512.png", "image/png")


@PromptServer.instance.routes.get(P + "/apple-touch-icon.png")
async def pwa_apple_icon(request):
    return _pwa_file("apple-touch-icon.png", "image/png")


@PromptServer.instance.routes.get(P + "/fonts/{name}")
async def pwa_font(request):
    name = os.path.basename(request.match_info.get("name", ""))
    if name not in ("Iosevka-Regular.woff2", "Iosevka-Bold.woff2"):
        return web.Response(status=404, text="font not found")
    fp = os.path.join(WEB_DIR, "fonts", name)
    if not os.path.isfile(fp):
        return web.Response(status=404, text="font not found")
    return web.FileResponse(fp, headers={"Content-Type": "font/woff2", "Cache-Control": "public, max-age=31536000, immutable"})


@PromptServer.instance.routes.get(P + "/api/artist/covers")
async def api_artist_covers(request):
    """画师封面图：?names=a,b,c → {归一化名: {source,file}}"""
    try:
        raw = request.query.get("names", "")
        names = [x for x in raw.split(",") if x.strip()]
        return _json({"covers": G.artist_covers(names)})
    except Exception as e:
        return _json({"covers": {}, "error": str(e)})


@PromptServer.instance.routes.get(P + "/api/chains")
async def api_chains(request):
    """收藏的画师串列表"""
    try:
        return _json({"chains": G.chain_list(), "artists": G.chain_artists_flat()})
    except Exception as e:
        return _json({"chains": [], "artists": [], "error": str(e)})


@PromptServer.instance.routes.post(P + "/api/chains/save")
async def api_chains_save(request):
    """收藏/更新画师串"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        return _json(G.chain_fav_save(data or {}))
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@PromptServer.instance.routes.post(P + "/api/chains/delete")
async def api_chains_delete(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        return _json(G.chain_fav_delete((data or {}).get("id", "")))
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@PromptServer.instance.routes.get(P + "/api/settings")
async def api_settings(request):
    s = G.load_settings()
    return _json({"webui_root": s.get("webui_root", ""),
                  "configured": bool(s.get("webui_root")),
                  "artist_format": s.get("artist_format") or G.DEFAULT_ARTIST_FORMAT,
                  "scan_mode": s.get("scan_mode") or "incremental"})


@PromptServer.instance.routes.post(P + "/api/settings")
async def api_settings_save(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    r = G.save_settings(data)
    return _json(r, 200 if r.get("ok") else 400)


# ---------------------------------------------------------------------------
# Queue view / control (ComfyUI's own queue is the source of truth)
# ---------------------------------------------------------------------------
def _queue_positive_preview(prompt):
    if not isinstance(prompt, dict):
        return ""
    try:
        for node in prompt.values():
            if not isinstance(node, dict):
                continue
            ct = node.get("class_type", "")
            if ct.startswith("KSampler") or ct in ("SamplerCustom", "SamplerCustomAdvanced"):
                ref = (node.get("inputs") or {}).get("positive")
                if isinstance(ref, (list, tuple)) and ref:
                    tgt = prompt.get(str(ref[0]))
                    if tgt is None:
                        tgt = prompt.get(ref[0])
                    if isinstance(tgt, dict):
                        txt = (tgt.get("inputs") or {}).get("text")
                        if isinstance(txt, str) and txt.strip():
                            return txt.strip()[:120]
        texts = []
        for node in prompt.values():
            if isinstance(node, dict) and "CLIPTextEncode" in node.get("class_type", ""):
                txt = (node.get("inputs") or {}).get("text")
                if isinstance(txt, str) and txt.strip():
                    texts.append(txt.strip())
        if texts:
            return max(texts, key=len)[:120]
    except Exception:
        pass
    return ""


def _queue_item_view(item):
    extra = item[3] if len(item) > 3 and isinstance(item[3], dict) else {}
    return {
        "number": item[0],
        "prompt_id": item[1],
        "positive": _queue_positive_preview(item[2]),
        "create_time": extra.get("create_time"),
    }


@PromptServer.instance.routes.get(P + "/api/queue")
async def api_queue(request):
    try:
        q = PromptServer.instance.prompt_queue
        with q.mutex:
            pending = [_queue_item_view(it) for it in sorted(q.queue, key=lambda x: x[0])]
            running = [_queue_item_view(it) for it in q.currently_running.values()]
        return _json({"ok": True, "running": running, "pending": pending, "shuffle_undo_available": _QUEUE_UNDO["map"] is not None})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@PromptServer.instance.routes.post(P + "/api/queue/front")
async def api_queue_front(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    pid = (data or {}).get("prompt_id", "")
    if not pid:
        return _json({"ok": False, "error": "prompt_id is required"}, 400)
    q = PromptServer.instance.prompt_queue
    with q.mutex:
        idx = None
        min_num = None
        for i, it in enumerate(q.queue):
            if not (isinstance(it, (list, tuple)) and len(it) >= 2
                    and isinstance(it[0], (int, float)) and isinstance(it[1], str)):
                return _json({"ok": False, "error": "unexpected queue item shape"}, 400)
            if min_num is None or it[0] < min_num:
                min_num = it[0]
            if it[1] == pid:
                idx = i
        if idx is None:
            return _json({"ok": False, "error": "prompt_id not in pending queue"}, 404)
        if q.queue[idx][0] == min_num:
            return _json({"ok": True, "number": min_num, "note": "already at front"})
        new_num = min_num - 1
        q.queue[idx] = (new_num,) + tuple(q.queue[idx][1:])
        heapq.heapify(q.queue)
    PromptServer.instance.queue_updated()
    return _json({"ok": True, "number": new_num})

_QUEUE_UNDO = {"map": None}


def _queue_validate(items):
    for it in items:
        if not (isinstance(it, (list, tuple)) and len(it) >= 2
                and isinstance(it[0], (int, float)) and isinstance(it[1], str)):
            return False
    return True


@PromptServer.instance.routes.post(P + "/api/queue/shuffle")
async def api_queue_shuffle(request):
    q = PromptServer.instance.prompt_queue
    with q.mutex:
        if not _queue_validate(q.queue):
            return _json({"ok": False, "error": "unexpected queue item shape"}, 400)
        if len(q.queue) < 2:
            return _json({"ok": True, "count": len(q.queue), "note": "nothing to shuffle"})
        ordered = sorted(q.queue, key=lambda x: x[0])
        _QUEUE_UNDO["map"] = {it[1]: it[0] for it in ordered}
        numbers = [it[0] for it in ordered]
        items = list(ordered)
        random.shuffle(items)
        newq = [(num,) + tuple(it[1:]) for num, it in zip(numbers, items)]
        q.queue[:] = newq
        heapq.heapify(q.queue)
    PromptServer.instance.queue_updated()
    return _json({"ok": True, "count": len(newq)})


@PromptServer.instance.routes.post(P + "/api/queue/shuffle/undo")
async def api_queue_shuffle_undo(request):
    q = PromptServer.instance.prompt_queue
    with q.mutex:
        snap = _QUEUE_UNDO.get("map")
        if not snap:
            return _json({"ok": False, "error": "no shuffle to undo"}, 400)
        if not _queue_validate(q.queue):
            return _json({"ok": False, "error": "unexpected queue item shape"}, 400)
        restored = 0
        newq = []
        for it in q.queue:
            if it[1] in snap:
                newq.append((snap[it[1]],) + tuple(it[1:]))
                restored += 1
            else:
                newq.append(it)
        q.queue[:] = newq
        heapq.heapify(q.queue)
        _QUEUE_UNDO["map"] = None
    PromptServer.instance.queue_updated()
    return _json({"ok": True, "restored": restored})

async def _submit_prompt(graph, front=False):
    import execution as _exec
    import uuid as _uuid
    srv = PromptServer.instance
    prompt_id = str(_uuid.uuid4())
    valid = await _exec.validate_prompt(prompt_id, graph, None)
    if not valid[0]:
        return {"ok": False, "error": str(valid[1]), "node_errors": valid[3]}
    number = srv.number
    if front:
        number = -number
    srv.number += 1
    extra_data = {"create_time": int(time.time() * 1000)}
    srv.prompt_queue.put((number, prompt_id, graph, extra_data, valid[2], {}))
    return {"ok": True, "prompt_id": prompt_id, "number": number}


@PromptServer.instance.routes.get(P + "/api/settings/export")
async def api_settings_export(request):
    try:
        return _json(G.export_bundle())
    except Exception as e:
        return _json({"error": str(e)}, 500)


@PromptServer.instance.routes.post(P + "/api/settings/import")
async def api_settings_import(request):
    try:
        data = await request.json()
    except Exception:
        data = None
    try:
        return _json(G.import_bundle(data))
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@PromptServer.instance.routes.get(P + "/api/tag-templates")
async def api_tag_templates(request):
    try:
        return _json(G.tag_template_list())
    except Exception as e:
        return _json({"templates": [], "error": str(e)})


@PromptServer.instance.routes.post(P + "/api/tag-templates/save")
async def api_tag_templates_save(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        return _json(G.tag_template_save(data or {}))
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@PromptServer.instance.routes.post(P + "/api/tag-templates/delete")
async def api_tag_templates_delete(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        return _json(G.tag_template_delete((data or {}).get("id", "")))
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@PromptServer.instance.routes.get(P + "/api/recipes")
async def api_recipes(request):
    try:
        return _json(G.recipe_list())
    except Exception as e:
        return _json({"recipes": [], "error": str(e)})


@PromptServer.instance.routes.post(P + "/api/generate")
async def api_generate(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = data or {}
    recipe = G.get_recipe(data.get("recipe_id", ""))
    if recipe is None:
        return _json({"ok": False, "error": "recipe not found"}, 400)
    try:
        count = max(1, min(500, int(data.get("count") or 1)))
        steps = int(data["steps"]) if data.get("steps") else None
        width = int(data["width"]) if data.get("width") else None
        height = int(data["height"]) if data.get("height") else None
        fixed_seed = data.get("seed")
        fixed_seed = int(fixed_seed) if (fixed_seed is not None and fixed_seed != "") else None
    except Exception:
        return _json({"ok": False, "error": "invalid numeric field"}, 400)
    front = bool(data.get("front"))
    order = list(range(count))
    if front:
        order.reverse()
    ids = []
    for i in order:
        seed_i = (fixed_seed + i) if fixed_seed is not None else random.randint(0, 2**63 - 1)
        graph = G.build_prompt(recipe, positive=data.get("positive") or "",
                                negative=data.get("negative"), seed=seed_i,
                                steps=steps, width=width, height=height)
        r = await _submit_prompt(graph, front=front)
        if not r.get("ok"):
            return _json({"ok": False, "error": r.get("error"), "node_errors": r.get("node_errors"), "submitted": len(ids)}, 400)
        ids.append(r["prompt_id"])
    return _json({"ok": True, "prompt_ids": ids, "count": len(ids)})


@PromptServer.instance.routes.get(P + "/api/prompts")
async def api_prompts(request):
    try:
        return _json(G.prompt_list())
    except Exception as e:
        return _json({"prompts": [], "error": str(e)})


@PromptServer.instance.routes.post(P + "/api/prompts/save")
async def api_prompts_save(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        return _json(G.prompt_save(data or {}))
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@PromptServer.instance.routes.post(P + "/api/prompts/delete")
async def api_prompts_delete(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        return _json(G.prompt_delete((data or {}).get("id", "")))
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@PromptServer.instance.routes.post(P + "/api/prompts/use")
async def api_prompts_use(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        return _json(G.prompt_use((data or {}).get("id", "")))
    except Exception as e:
        return _json({"ok": False, "error": str(e)})




@PromptServer.instance.routes.get(P + "/api/scan")
async def api_scan(request):
    source = request.query.get("source", "comfyui")
    force = request.query.get("force", "0") == "1"
    refresh = request.query.get("refresh", "0") == "1"
    r = G.start_scan(source, force=force, refresh=refresh)
    return _json(r)


@PromptServer.instance.routes.get(P + "/api/status")
async def api_status(request):
    return _json({
        "comfyui": G.scan_status("comfyui"),
        "webui": G.scan_status("webui"),
        "video": G.scan_status("video"),
        "settings": G.load_settings(),
    })


@PromptServer.instance.routes.get(P + "/api/gallery")
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


@PromptServer.instance.routes.get(P + "/api/gallery/random")
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


@PromptServer.instance.routes.get(P + "/api/models")
async def api_models(request):
    source = request.query.get("source", "comfyui")
    return _json({"items": G.models(source)})


@PromptServer.instance.routes.get(P + "/api/samplers")
async def api_samplers(request):
    source = request.query.get("source", "comfyui")
    return _json({"items": G.samplers(source)})


@PromptServer.instance.routes.get(P + "/api/loras")
async def api_loras(request):
    source = request.query.get("source", "comfyui")
    return _json({"items": G.loras(source)})


@PromptServer.instance.routes.get(P + "/api/artists")
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


@PromptServer.instance.routes.get(P + "/api/artlib/facets")
async def api_artlib_facets(request):
    try:
        G._artlib_gc(_float(request.query.get("gc"), 0) or 0)
        return _json(G.artlib_facets())
    except Exception as e:
        return _json({"error": str(e)})


@PromptServer.instance.routes.post(P + "/api/artlib/release")
async def api_artlib_release(request):
    """释放画师库内存缓存（离开画师库标签页时前端调用）。"""
    try:
        return _json(G.artlib_release())
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@PromptServer.instance.routes.get(P + "/api/artlib/status")
async def api_artlib_status(request):
    """画师库数据是否已安装（含下载进度）。"""
    try:
        return _json(G.artlib_status())
    except Exception as e:
        return _json({"error": str(e)})


@PromptServer.instance.routes.post(P + "/api/artlib/install")
async def api_artlib_install(request):
    """启动后台下载画师库数据（Release 附件）。"""
    url = ""
    try:
        if request.can_read_body:
            url = (await request.json() or {}).get("url", "")
    except Exception:
        pass
    try:
        return _json(G.artlib_install(url))
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@PromptServer.instance.routes.get(P + "/api/artlib/search")
async def api_artlib_search(request):
    q = request.query
    try:
        G._artlib_gc(_float(q.get("gc"), 0) or 0)
    except Exception:
        pass
    try:
        r = G.artlib_search(
            q=q.get("q", ""),
            archetype=q.get("archetype", ""),
            origin=q.get("origin", ""),
            subject=q.get("subject", ""),
            theme=q.get("theme", ""),
            technique=q.get("technique", ""),
            composition=q.get("composition", ""),
            rating=q.get("rating", ""),
            min_posts=_int(q.get("min_posts"), 0) or 0,
            sort=q.get("sort", "posts"),
            page=_int(q.get("page"), 1) or 1,
            page_size=_int(q.get("page_size"), 60) or 60,
        )
        return _json({"items": r["items"], "total": r["total"]})
    except Exception as e:
        return _json({"items": [], "total": 0, "error": str(e)})


@PromptServer.instance.routes.get(P + "/api/artlib/preview")
async def api_artlib_preview(request):
    """画师库本地预览：读「本地索引用过该 tag 的图」作封面。names 逗号分隔。"""
    q = request.query
    try:
        names = [n for n in q.get("names", "").split(",") if n.strip()]
        r = G.artlib_preview(source=q.get("source", "comfyui"), names=names)
        return _json(r)
    except Exception as e:
        return _json({})


@PromptServer.instance.routes.get(P + "/api/artists/tags")
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


@PromptServer.instance.routes.post(P + "/api/artists/note")
async def api_artist_note(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    r = G.save_artist_note(data.get("tag", ""), data.get("note", ""))
    return _json(r)


@PromptServer.instance.routes.post(P + "/api/artists/fav")
async def api_artist_fav(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    r = G.save_artist_fav(data.get("tag", ""), bool(data.get("on", True)))
    return _json(r)


@PromptServer.instance.routes.post(P + "/api/artists/cover")
async def api_artist_cover(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    r = G.save_artist_cover(data.get("tag", ""), _safe_name(data.get("file", "")))
    return _json(r)


@PromptServer.instance.routes.post(P + "/api/artists/weight")
async def api_artist_weight(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        w = float(data.get("weight", 1.0))
    except Exception:
        w = 1.0
    r = G.save_artist_weight(data.get("tag", ""), w)
    return _json(r)


@PromptServer.instance.routes.get(P + "/api/stats")
async def api_stats(request):
    return _json(G.stats())


@PromptServer.instance.routes.get(P + "/api/fav")
async def api_fav_list(request):
    return _json({"items": [{"source": a, "file": b} for a, b in G._fav_set()]})


@PromptServer.instance.routes.post(P + "/api/fav")
async def api_fav_toggle(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    r = G.save_fav(data.get("source", "comfyui"), _safe_name(data.get("file", "")), bool(data.get("on", True)))
    return _json(r)


@PromptServer.instance.routes.post(P + "/api/delete")
async def api_delete(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    r = G.delete_image(data.get("source", "comfyui"), _safe_name(data.get("file", "")))
    return _json(r, 200 if r.get("ok") else 404)


@PromptServer.instance.routes.post(P + "/api/restore")
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


@PromptServer.instance.routes.get(P + "/api/ratings")
async def api_ratings(request):
    source = request.query.get("source", "comfyui")
    return _json({"ratings": G.load_ratings(), "counts": G.rating_counts(source)})


@PromptServer.instance.routes.post(P + "/api/rate")
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


@PromptServer.instance.routes.get(P + "/api/trash")
async def api_trash_info(request):
    return _json(G.trash_info())


@PromptServer.instance.routes.post(P + "/api/trash/purge")
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


@PromptServer.instance.routes.post(P + "/api/trash/empty")
async def api_trash_empty(request):
    return _json(G.empty_trash())


@PromptServer.instance.routes.get(P + "/api/events")
async def api_events(request):
    """Long-polling フォールバック：since より新しいリアルタイムイベントを待機して返す。"""
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


@PromptServer.instance.routes.post(P + "/api/check")
async def api_check_new(request):
    items = await asyncio.to_thread(G.check_new_images, "comfyui")
    return _json({"ok": True, "added": len(items), "counts": G.rating_counts("comfyui")})


@PromptServer.instance.routes.get(P + "/ws")
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


@PromptServer.instance.routes.get(P + "/api/image")
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


@PromptServer.instance.routes.get(P + "/api/image/open-folder")
async def api_open_folder(request):
    """在文件管理器中打开图片所在文件夹并选中该文件（Windows）"""
    source = request.query.get("source", "comfyui")
    file = _safe_name(request.query.get("file", ""))
    fp = G.image_path(source, file)
    if not fp:
        return _json({"error": "not found"}, 404)
    try:
        # 用 os.startfile 打开所在文件夹（不选中文件）——避免外部进程调用触发 Registry 安全扫描
        os.startfile(os.path.dirname(fp))
        return _json({"ok": True})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


@PromptServer.instance.routes.get(P + "/api/meta")
async def api_meta(request):
    source = request.query.get("source", "comfyui")
    file = _safe_name(request.query.get("file", ""))
    return _json({"metadata": G.raw_metadata(source, file)})


@PromptServer.instance.routes.get(P + "/api/boot")
async def api_boot(request):
    """进入图库：按设置 scan_mode 分流——incremental（默认）秒开+后台补新增；full（选项）才全量重建。"""
    results = {}
    full = (G.load_settings().get("scan_mode") or "incremental") == "full"
    for src in ("comfyui",):
        results[src] = G.start_scan(src, force=full, refresh=not full)
    G.ensure_danbooru_cron()
    return _json({"ok": True, "booted": results, "scan_mode": "full" if full else "incremental"})


@PromptServer.instance.routes.get(P + "/api/danbooru/status")
async def api_danbooru_status(request):
    return _json(G.danbooru_cron_status())


@PromptServer.instance.routes.post(P + "/api/danbooru/sync")
async def api_danbooru_sync(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    force = bool((data or {}).get("force", True))
    return _json(G.trigger_danbooru_sync(force=force))


@PromptServer.instance.routes.get(P + "/api/danbooru/tags")
async def api_danbooru_tags(request):
    search = request.query.get("search", "")
    limit = min(_int(request.query.get("limit"), 50) or 50, 500)
    return _json({"items": G.danbooru_tags_search(search=search, limit=limit)})


@PromptServer.instance.routes.get(P + "/api/tags")
async def api_tags(request):
    source = request.query.get("source", "comfyui")
    search = request.query.get("search", "")
    limit = min(_int(request.query.get("limit"), 200) or 200, 1000)
    return _json({"items": G.tag_stats(source, search=search, limit=limit)})


@PromptServer.instance.routes.get(P + "/api/blacklist")
async def api_blacklist_get(request):
    return _json({"items": G.load_blacklist()})


@PromptServer.instance.routes.post(P + "/api/blacklist")
async def api_blacklist_save(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    return _json(G.save_blacklist(data.get("items", [])))


@PromptServer.instance.routes.get(P + "/api/folders")
async def api_folders_get(request):
    return _json({"folders": G.folder_list()})


@PromptServer.instance.routes.get(P + "/api/folders/images")
async def api_folders_images(request):
    name = request.query.get("folder", "")
    source = request.query.get("source", "")
    return _json({"items": G.folder_images(name, source)})


@PromptServer.instance.routes.get(P + "/api/folders/of")
async def api_folders_of(request):
    source = request.query.get("source", "comfyui")
    file = _safe_name(request.query.get("file", ""))
    return _json({"folders": G.folder_contains(source, file)})


@PromptServer.instance.routes.post(P + "/api/folders")
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
    return _json({"ok": False, "error": "不明な操作です"}, 400)


@PromptServer.instance.routes.get(P + "/api/fs/drives")
async def api_fs_drives(request):
    return _json({"drives": G.list_drives()})


@PromptServer.instance.routes.get(P + "/api/fs/list")
async def api_fs_list(request):
    path = request.query.get("path", "")
    r = G.list_dir(path)
    return _json(r, 200 if "error" not in r else 400)


@PromptServer.instance.routes.post(P + "/api/fs/mkdir")
async def api_fs_mkdir(request):
    """在当前浏览目录下新建文件夹（打包保存用）"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    parent = (data.get("path") or "").strip()
    name = (data.get("name") or "").strip()
    if not parent or not name:
        return _json({"error": "パスまたは名前が不足しています"}, 400)
    if not os.path.isdir(parent):
        return _json({"error": "親フォルダが存在しません"}, 400)
    name = name.replace("\\", "/").replace("/", "")
    if not name:
        return _json({"error": "名称无效"}, 400)
    target = os.path.join(parent, name)
    try:
        os.makedirs(target, exist_ok=True)
        return _json({"ok": True, "path": target})
    except Exception as e:
        return _json({"ok": False, "error": str(e)}, 500)


@PromptServer.instance.routes.get(P + "/api/fs/open-dir")
async def api_fs_open_dir(request):
    """在文件管理器中打开一个目录（os.startfile，避免外部进程调用触发 Registry 扫描）"""
    path = request.query.get("path", "").strip()
    if not path or not os.path.isdir(path):
        return _json({"error": "目录不存在"}, 400)
    try:
        os.startfile(path)
        return _json({"ok": True})
    except Exception as e:
        return _json({"ok": False, "error": str(e)}, 500)


@PromptServer.instance.routes.post(P + "/api/zip")
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
    """同步执行：收集文件 + 写 zip（在后台线程运行）。返回 (resp_dict, code)"""
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


# 注意：不在 ComfyUI 启动时做任何扫描 —— 图库为懒启动，
# 仅在用户点节点按钮（/api/boot）或打开图库页面时才触发索引。
