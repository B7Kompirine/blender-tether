"""Asset sources (Poly Haven, ambientCG, Sketchfab, Poly Pizza): search and safe download of models, PBR textures and HDRIs; the license is in every result.
HTTPS and allow-listed hosts only (redirects included), size/time caps, md5 verification, zip-slip-safe extraction, a local cache, CC0 only by default.
The Sketchfab download token and the Poly Pizza key come from ~/.tether/assets.json or an environment variable.

Usage: python -m tether.assets search [source] "query" [--kind model|texture|hdri] | download source id [--resolution 2k]; tether.mcp_server uses asset_search / asset_import
"""
import argparse
import concurrent.futures
import hashlib
import io
import itertools
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from .agent import HOME

ROOT = Path(os.environ.get("TETHER_ASSETS_DIR") or HOME / "assets")
CONFIG = HOME / "assets.json"
USER_AGENT = "blender-tether/0.1"
URLS = {"polyhaven": "https://api.polyhaven.com", "ambientcg": "https://ambientcg.com", "sketchfab": "https://api.sketchfab.com",
        "polypizza": "https://api.poly.pizza"}
ALLOWED = {"polyhaven": {"api.polyhaven.com", "dl.polyhaven.org", "cdn.polyhaven.com"},
           "ambientcg": {"ambientcg.com", "acg-download.struffelproductions.com", "acg-media.struffelproductions.com"},
           "sketchfab": {"api.sketchfab.com", "media.sketchfab.com", "sketchfab-prod-media.s3.amazonaws.com"},
           "polypizza": {"api.poly.pizza", "static.poly.pizza"}}
HOST_CHECKS = {}                      # source -> function(hostname): sources whose hosts are decided by a function (generate.py)
LOCAL_TEST = False                    # True: http://127.0.0.1 is accepted (only the tests' fake server)
JSON_LIMIT = 8 << 20
PREVIEW_LIMIT = 3 << 20
MAX_MB = 250                          # total download cap of one asset
REQUEST_S = 30
DOWNLOAD_S = 900
ZIP_MAX_BYTES = 2 << 30
ZIP_MAX_FILES = 2000
CACHE_HOURS = 12                      # Poly Haven list cache
SOURCES = ("polyhaven", "ambientcg", "sketchfab", "polypizza")
KINDS = ("model", "texture", "hdri")
SK_LICENSES = {"cc0", "by", "by-sa", "by-nd", "by-nc", "by-nc-sa", "by-nc-nd", "free-st"}
PP_CATEGORIES = {"food": 0, "clutter": 1, "weapons": 2, "transport": 3, "furniture": 4, "objects": 5, "nature": 6, "animals": 7,
                 "buildings": 8, "people": 9, "scenes": 10, "other": 11}
SYNONYMS = {"couch": ["sofa"], "sofa": ["couch"], "tv": ["television"], "television": ["tv"], "bin": ["trash", "garbage"], "trash": ["bin", "garbage"],
            "garbage": ["trash", "bin"], "bike": ["bicycle"], "bicycle": ["bike"], "car": ["vehicle", "automobile"], "cupboard": ["cabinet", "wardrobe"],
            "cabinet": ["cupboard"], "stone": ["rock"], "rock": ["stone", "boulder"], "fridge": ["refrigerator"], "tap": ["faucet"]}


class AssetError(Exception):
    pass


# --- safe HTTP ---

def _short(url):
    return url if len(url) <= 90 else url[:87] + "..."


def _check_url(url, source):
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" and not (LOCAL_TEST and p.scheme == "http" and p.hostname == "127.0.0.1"):
        raise AssetError(f"{source}: only https addresses are opened: {_short(url)}")
    if source in HOST_CHECKS:
        if not HOST_CHECKS[source](p.hostname):
            raise AssetError(f"{source}: host '{p.hostname}' is not allowed")
    elif p.hostname not in ALLOWED[source]:
        raise AssetError(f"{source}: host '{p.hostname}' is not allowed (allowed: {', '.join(sorted(ALLOWED[source]))})")


class _Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, source):
        self.source = source

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl, self.source)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url, source, headers=None, timeout=REQUEST_S, data=None):
    _check_url(url, source)
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        return urllib.request.build_opener(_Redirect(source)).open(request, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise AssetError(f"{source}: HTTP {e.code} {e.reason} ({_short(url)})") from None
    except (urllib.error.URLError, OSError) as e:
        raise AssetError(f"{source}: connection error ({getattr(e, 'reason', e)}) ({_short(url)})") from None


def _fetch(url, source, headers=None, cap=JSON_LIMIT, data=None):
    with _open(url, source, headers, data=data) as r:
        data = r.read(cap + 1)
    if len(data) > cap:
        raise AssetError(f"{source}: the response is larger than {cap >> 20} MB ({_short(url)})")
    return data


def _json(url, source, headers=None, data=None):
    try:
        return json.loads(_fetch(url, source, headers, data=data))
    except ValueError:
        raise AssetError(f"{source}: the response is not JSON ({_short(url)})") from None


def _download_file(url, source, target, headers=None, md5=None, cap=MAX_MB << 20):
    """Streams the download; if the cap, the time limit or (when given) the md5 is exceeded/violated the partial file is deleted."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    t0, h, n = time.time(), hashlib.md5(), 0
    try:
        with _open(url, source, headers) as r, open(partial, "wb") as f:
            if int(r.headers.get("Content-Length") or 0) > cap:
                raise AssetError(f"{target.name}: {int(r.headers['Content-Length']) >> 20} MB, cap {cap >> 20} MB")
            while block := r.read(1 << 16):
                n += len(block)
                if n > cap:
                    raise AssetError(f"{target.name}: the {cap >> 20} MB cap was exceeded")
                if time.time() - t0 > DOWNLOAD_S:
                    raise AssetError(f"{target.name}: the download took longer than {DOWNLOAD_S} s")
                h.update(block)
                f.write(block)
        if md5 and h.hexdigest() != md5:
            raise AssetError(f"{target.name}: md5 mismatch (corrupt or changed file)")
        os.replace(partial, target)
    except OSError as e:
        raise AssetError(f"{target.name}: could not be written ({e})") from None
    finally:
        partial.unlink(missing_ok=True)
    return n


def _safe_path(folder, relative):
    path = (folder / relative).resolve()
    if folder.resolve() not in path.parents:
        raise AssetError(f"path escapes the folder: {relative}")
    return path


def _unzip(zip_path, folder):
    """Extracts with zip-slip, file count and unpacked size checks."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            total, count = 0, 0
            for b in z.infolist():
                if b.is_dir():
                    continue
                _safe_path(folder, b.filename)
                total, count = total + b.file_size, count + 1
                if total > ZIP_MAX_BYTES or count > ZIP_MAX_FILES:
                    raise AssetError("the zip is too large or has too many files (could be a zip bomb)")
            z.extractall(folder)
    except zipfile.BadZipFile:
        raise AssetError(f"{zip_path.name}: not a valid zip") from None


# --- settings, shared helpers ---

def _setting(name, env):
    v = os.environ.get(env, "").strip()
    if v:
        return v
    try:
        return str(json.loads(CONFIG.read_text(encoding="utf-8")).get(name, "")).strip()
    except (OSError, ValueError):
        return ""


def _key(name, env, where, what):
    v = _setting(name, env)
    if not v:
        raise AssetError(f"{what} missing: get it at {where}, write {{\"{name}\": \"...\"}} into {CONFIG} or set the {env} environment variable "
                         "(never paste the key into the chat)")
    return v


def _source(name):
    k = re.sub(r"[\s.\-_]", "", str(name).lower())
    k = {"ph": "polyhaven", "acg": "ambientcg", "sf": "sketchfab", "pp": "polypizza"}.get(k, k)
    if k not in SOURCES and k != "all":
        raise AssetError(f"unknown source '{name}'; valid: all, {', '.join(SOURCES)}")
    return k


def _kind(name):
    t = {"models": "model", "textures": "texture", "material": "texture", "materials": "texture", "hdris": "hdri"}.get(str(name or "").lower(), str(name or "").lower())
    if t and t not in KINDS:
        raise AssetError(f"unknown kind '{name}'; valid: {', '.join(KINDS)}")
    return t


def _id(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,80}", str(name or "")):
        raise AssetError(f"invalid asset id: {str(name)[:40]!r}")
    return str(name)


def placement(target_size=0, location=None):
    """Validates the numeric inputs before downloading: (target_size in metres, location [x, y, z] or None). target_size 0 = no scaling."""
    try:
        size = float(target_size or 0)
        place = None if location is None else [float(x) for x in location]
    except (TypeError, ValueError):
        raise AssetError(f"target_size must be a number and location three numbers [x, y, z]: target_size={target_size!r}, location={location!r}") from None
    if not math.isfinite(size) or size < 0:
        raise AssetError(f"target_size must be 0 (no scaling) or positive metres, got: {target_size!r}")
    if place is not None and (len(place) != 3 or not all(math.isfinite(x) for x in place)):
        raise AssetError(f"location must be three numbers [x, y, z] (metres), got: {location!r}")
    return size, place


def _is_cc0(license):
    return str(license).strip().upper().startswith("CC0")


def _license_set(license):
    k = {x.strip().lower() for x in str(license or "cc0").split(",") if x.strip()}
    bad = k - SK_LICENSES
    if bad:
        raise AssetError(f"unknown license {sorted(bad)}; valid: {', '.join(sorted(SK_LICENSES))}")
    return k or {"cc0"}


def _nearest(available, wanted):
    """From '1k', '2k'... the one nearest to the wanted one (the largest that is not above it, otherwise the smallest above)."""
    numbers = {m: int(re.sub(r"\D", "", m) or 0) for m in available}
    want = int(re.sub(r"\D", "", wanted) or 1)
    below = [m for m, s in numbers.items() if s <= want]
    return max(below, key=numbers.get) if below else min(numbers, key=numbers.get)


def _result(source, id_, name, kind, **extra):
    return {"source": source, "id": id_, "name": name, "kind": kind, "category": "", "tags": [], "license": "", "author": "", "faces": 0,
            "size_m": None, "resolution": "", "preview": "", "link": "", "animated": False, **extra}


def _words(query):
    return [[w, *SYNONYMS.get(w, [])] for w in re.findall(r"[a-z0-9]+", str(query).lower())]


def _score(words, *fields):
    """fields: (text, weight) pairs. Every query word (with its synonyms) must occur in some field (AND); score = sum of the best weights."""
    if not words:
        return 1
    total = 0
    for options in words:
        best = max((w for text, w in fields for k in options if k in text), default=0)
        if not best:
            return 0
        total += best
    return total


# --- Poly Haven ---

PH_KIND = {"model": "models", "texture": "textures", "hdri": "hdris"}
PH_NAMES = {0: "hdri", 1: "texture", 2: "model"}
PH_MAPS = {"color": "Diffuse", "normal": "nor_gl", "roughness": "Rough", "metal": "Metal", "ao": "AO"}


def _ph_list(kind):
    path = ROOT / "cache" / f"polyhaven_{kind}.json"
    try:
        if time.time() - path.stat().st_mtime < CACHE_HOURS * 3600:
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    d = _json(f"{URLS['polyhaven']}/assets?t={kind}", "polyhaven")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass
    return d


def _ph_search(words, kind, category, attrs, min_size_m, max_faces, limit):
    kinds = [PH_KIND[kind]] if kind else list(PH_KIND.values())
    listing = {t: _ph_list(t) for t in kinds}
    if attrs:
        valid = sorted({k for g in listing.get("hdris", {}).values() for k in g.get("attributes", {})})
        bad = [k for k in attrs if k not in valid]
        if bad:
            raise AssetError(f"polyhaven: unknown attribute {bad}; valid (hdri): {', '.join(valid) or 'no hdri list: give kind=hdri'}")
    found = []
    for kind_name, entries in listing.items():
        for id_, g in entries.items():
            cat = " ".join(g.get("categories", [])) + " " + g.get("category", "")
            if category and category.lower() not in cat.lower():
                continue
            if attrs and any(str(g.get("attributes", {}).get(k, "")).lower() != str(v).lower() for k, v in attrs.items()):
                continue
            size = [x / 1000 for x in g.get("dimensions", [])]
            if min_size_m and (len(size) < 2 or min(size[:2]) < min_size_m):
                continue
            if max_faces and g.get("polycount", 0) > max_faces:
                continue
            p = _score(words, (id_.lower() + " " + g.get("name", "").lower(), 5), (" ".join(g.get("tags", [])).lower(), 3), (cat.lower(), 2),
                       (g.get("description", "").lower(), 1))
            if p:
                found.append((p, g.get("name", id_), id_, kind_name, g, size))
    found.sort(key=lambda x: (-x[0], x[1]))
    return [_result("polyhaven", id_, g.get("name", id_), PH_NAMES[g.get("type", 2)], category=g.get("category") or "/".join(g.get("categories", [])),
                    tags=g.get("tags", []), license="CC0", author=", ".join(g.get("authors", {})), faces=g.get("polycount", 0),
                    size_m=[round(x, 3) for x in size] if size else None, resolution=f"{(g.get('max_resolution') or [0])[0] // 1024}k",
                    preview=f"https://cdn.polyhaven.com/asset_img/thumbs/{id_}.png?width=256&height=256", link=f"https://polyhaven.com/a/{id_}",
                    attrs=g.get("attributes") or None) for _, _, id_, kind_name, g, size in found[:limit]]


def _ph_info(id_):
    i = _json(f"{URLS['polyhaven']}/info/{id_}", "polyhaven")
    return {"name": i.get("name", id_), "kind": PH_NAMES[i.get("type", 2)], "license": "CC0", "author": ", ".join(i.get("authors", {})),
            "attribution": f"\"{i.get('name', id_)}\" by {', '.join(i.get('authors', {}))}, Poly Haven (CC0), https://polyhaven.com/a/{id_}"}


def _cap_check(name, size, cap):
    if size > cap:
        raise AssetError(f"{name}: {size >> 20} MB would be downloaded, cap {cap >> 20} MB: ask for a lower resolution (resolution='1k')")


def _ph_download(id_, kind, resolution, fmt, folder, cap):
    f = _json(f"{URLS['polyhaven']}/files/{id_}", "polyhaven")
    if kind == "model":
        fmt = fmt or "gltf"
        if fmt not in ("gltf", "fbx") or fmt not in f:
            raise AssetError(f"polyhaven {id_}: no '{fmt}' format (available: {', '.join(k for k in ('gltf', 'fbx') if k in f) or '-'})")
        c = _nearest(f[fmt], resolution)
        g = f[fmt][c][fmt]
        parts = [(g["url"], Path(urllib.parse.urlsplit(g["url"]).path).name, g.get("md5"), g.get("size", 0))]
        parts += [(x["url"], rel, x.get("md5"), x.get("size", 0)) for rel, x in (g.get("include") or {}).items()]
        _cap_check(id_, sum(p[3] for p in parts), cap)
        for url, rel, md5, _ in parts:
            _download_file(url, "polyhaven", _safe_path(folder, rel), md5=md5, cap=cap)
        return {"main_file": str(folder / parts[0][1]), "resolution": c}
    if kind == "hdri":
        fmt = fmt or "hdr"
        h = {k: v for k, v in (f.get("hdri") or {}).items() if fmt in v}
        if not h:
            raise AssetError(f"polyhaven {id_}: no hdri in '{fmt}' format")
        c = _nearest(h, resolution)
        g = h[c][fmt]
        _cap_check(id_, g.get("size", 0), cap)
        path = folder / Path(urllib.parse.urlsplit(g["url"]).path).name
        _download_file(g["url"], "polyhaven", path, md5=g.get("md5"), cap=cap)
        return {"main_file": str(path), "resolution": c}
    fmt = fmt or "jpg"
    c = _nearest(f.get("Diffuse") or {}, resolution) if f.get("Diffuse") else None
    if c is None:
        raise AssetError(f"polyhaven {id_}: no texture maps")
    chosen = {}
    for name, key in PH_MAPS.items():
        if key in f and c in f[key]:
            ext = fmt if fmt in f[key][c] else next((b for b in ("png", "jpg", "exr") if b in f[key][c]), None)
            if ext:
                chosen[name] = f[key][c][ext]
    _cap_check(id_, sum(g.get("size", 0) for g in chosen.values()), cap)
    maps = {}
    for name, g in chosen.items():
        path = folder / Path(urllib.parse.urlsplit(g["url"]).path).name
        _download_file(g["url"], "polyhaven", path, md5=g.get("md5"), cap=cap)
        maps[name] = str(path)
    return {"maps": maps, "resolution": c}


# --- ambientCG ---

AC_KIND = {"texture": "Material", "hdri": "HDRI"}
AC_MAPS = {"color": r"_Color\.", "normal": r"_NormalGL\.", "roughness": r"_Roughness\.", "metal": r"_Metalness\.", "ao": r"_AmbientOcclusion\."}


def _ac_search(query, kind, category, limit):
    if kind == "model":
        raise AssetError("ambientcg: no models (textures and hdris only)")
    results = []
    for t in ([AC_KIND[kind]] if kind else AC_KIND.values()):
        p = {"type": t, "limit": min(max(limit, 1), 50), "sort": "Popular", "include": "imageData,tagData", **({"q": query} if query else {})}
        for a in _json(f"{URLS['ambientcg']}/api/v2/full_json?{urllib.parse.urlencode(p)}", "ambientcg").get("foundAssets", []):
            tags = a.get("tags") or []
            if category and category.lower() not in (" ".join(tags) + " " + str(a.get("displayCategory") or "")).lower():
                continue
            results.append(_result("ambientcg", a["assetId"], a.get("displayName") or a["assetId"], "hdri" if a.get("dataType") == "HDRI" else "texture",
                                   category=str(a.get("displayCategory") or ""), tags=tags, license="CC0", author="ambientCG",
                                   preview=(a.get("previewImage") or {}).get("256-PNG", ""), link=f"https://ambientcg.com/view?id={a['assetId']}"))
    return results[:limit]


def _ac_asset(id_):
    d = _json(f"{URLS['ambientcg']}/api/v2/full_json?id={urllib.parse.quote(id_)}&include=downloadData,imageData", "ambientcg").get("foundAssets") or []
    if not d:
        raise AssetError(f"ambientcg: '{id_}' not found")
    return d[0]


def _ac_info(id_):
    a = _ac_asset(id_)
    name = a.get("displayName") or id_
    return {"name": name, "kind": "hdri" if a.get("dataType") == "HDRI" else "texture", "license": "CC0", "author": "ambientCG", "_asset": a,
            "attribution": f"\"{name}\", ambientCG (CC0), https://ambientcg.com/view?id={id_}"}


def _ac_download(id_, kind, resolution, fmt, folder, cap, asset):
    downloads = (asset.get("downloadFolders", {}).get("default", {}).get("downloadFiletypeCategories", {}).get("zip", {}).get("downloads")) or []
    ours = {}
    for d in downloads:
        m = re.fullmatch(r"(\d+K)(?:-(\w+))?", d.get("attribute", ""))
        if m and (kind == "hdri" or (m.group(2) or "").upper() == (fmt or "jpg").upper()):
            ours[m.group(1).lower()] = d
    if not ours:
        raise AssetError(f"ambientcg {id_}: no downloadable package (format '{fmt or 'jpg'}')")
    c = _nearest(ours, resolution)
    d = ours[c]
    _cap_check(f"{id_} {c}", int(d.get("size") or 0), cap)
    zip_path = _safe_path(folder, d["fileName"])
    _download_file(d["downloadLink"], "ambientcg", zip_path, cap=cap)
    _unzip(zip_path, folder)
    zip_path.unlink()
    files = [p for p in folder.rglob("*") if p.is_file()]
    if kind == "hdri":
        main = next((p for e in (".hdr", ".exr") for p in files if p.suffix.lower() == e), None)
        if main is None:
            raise AssetError(f"ambientcg {id_}: no .hdr/.exr in the package")
        return {"main_file": str(main), "resolution": c}
    maps = {}
    for name, pattern in AC_MAPS.items():
        p = next((p for p in files if re.search(pattern, p.name, re.I) and p.suffix.lower() in (".jpg", ".png", ".exr")), None)
        if p:
            maps[name] = str(p)
    if "color" not in maps:
        raise AssetError(f"ambientcg {id_}: no color map in the package")
    return {"maps": maps, "resolution": c}


# --- Sketchfab ---

def _sk_result(m):
    thumbs = sorted((m.get("thumbnails") or {}).get("images", []), key=lambda x: x.get("width", 0))
    preview = next((x["url"] for x in thumbs if x.get("width", 0) >= 256), thumbs[-1]["url"] if thumbs else "")
    return _result("sketchfab", m["uid"], m.get("name", ""), "model", category=", ".join(c.get("name", "") for c in m.get("categories") or []),
                   tags=[t.get("name", "") for t in m.get("tags") or []], license=(m.get("license") or {}).get("label", ""),
                   author=(m.get("user") or {}).get("displayName", ""), faces=m.get("faceCount") or 0, preview=preview,
                   link=m.get("viewerUrl", ""), animated=(m.get("animationCount") or 0) > 0)


def _sk_search(query, license, category, limit, max_faces, animated):
    if not query:
        raise AssetError("sketchfab: a query is required")
    p = {"type": "models", "q": query, "downloadable": "true", "count": min(max(limit, 1), 24),
         **({"categories": category} if category else {}), **({"max_face_count": max_faces} if max_faces else {}), **({"animated": "true"} if animated else {})}
    licenses = sorted(_license_set(license), key=lambda x: (x != "cc0", x))          # the API takes a single license value (a comma list gives 400): one request per license, CC0 first

    def fetch(lic):
        return [_sk_result(m) for m in _json(f"{URLS['sketchfab']}/v3/search?{urllib.parse.urlencode({**p, 'license': lic})}", "sketchfab").get("results", [])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        groups = list(ex.map(fetch, licenses))
    seen, results = set(), []
    for r in (r for group in itertools.zip_longest(*groups) for r in group if r is not None):
        if r["id"] not in seen:
            seen.add(r["id"])
            results.append(r)
    return results[:limit]


def _sk_info(uid):
    m = _json(f"{URLS['sketchfab']}/v3/models/{uid}", "sketchfab")
    s = _sk_result(m)
    return {"name": s["name"], "kind": "model", "license": s["license"], "author": s["author"],
            "attribution": f"\"{s['name']}\" by {s['author']}, Sketchfab ({s['license']}), {s['link']}"}


def _sk_download(uid, folder, cap):
    token = _key("sketchfab_token", "SKETCHFAB_TOKEN", "https://sketchfab.com/settings/password (API token)", "Sketchfab API token")
    try:
        d = _json(f"{URLS['sketchfab']}/v3/models/{uid}/download", "sketchfab", {"Authorization": f"Token {token}"})
    except AssetError as e:
        if "HTTP 401" not in str(e):
            raise
        d = _json(f"{URLS['sketchfab']}/v3/models/{uid}/download", "sketchfab", {"Authorization": f"Bearer {token}"})
    g = d.get("gltf")
    if not g or not g.get("url"):
        raise AssetError(f"sketchfab {uid}: no gltf in the download response (available: {', '.join(d) or '-'})")
    _cap_check(uid, int(g.get("size") or 0), cap)
    zip_path = folder / "model.zip"
    _download_file(g["url"], "sketchfab", zip_path, cap=cap)         # signed address: no Authorization is sent
    _unzip(zip_path, folder)
    zip_path.unlink()
    main = next((p for e in (".gltf", ".glb") for p in sorted(folder.rglob("*" + e))), None)
    if main is None:
        raise AssetError(f"sketchfab {uid}: no .gltf/.glb in the package")
    return {"main_file": str(main), "resolution": ""}


# --- Poly Pizza ---

def _pp_headers():
    return {"x-auth-token": _key("polypizza_key", "POLYPIZZA_KEY", "https://poly.pizza/settings/api", "Poly Pizza API key")}


def _pp_result(m):
    return _result("polypizza", m["ID"], m.get("Title", ""), "model", category=m.get("Category", ""), tags=m.get("Tags") or [], license=m.get("Licence", ""),
                   author=(m.get("Creator") or {}).get("Username", ""), faces=m.get("Tri Count") or 0, preview=m.get("Thumbnail", ""),
                   link=f"https://poly.pizza/m/{m['ID']}", animated=bool(m.get("Animated")), attribution=m.get("Attribution", ""), download=m.get("Download", ""))


def _pp_search(query, license, category, limit, animated):
    headers = _pp_headers()
    if not query:
        raise AssetError("polypizza: a query is required")
    k = _license_set(license)
    p = {"Limit": min(max(limit, 1), 32)}
    if k == {"cc0"}:
        p["License"] = 1
    elif k == {"by"}:
        p["License"] = 0
    if category:
        name = next((a for a in PP_CATEGORIES if a.startswith(category.lower())), None)
        if name is None:
            raise AssetError(f"polypizza: unknown category '{category}'; valid: {', '.join(PP_CATEGORIES)}")
        p["Category"] = PP_CATEGORIES[name]
    if animated:
        p["Animated"] = 1
    d = _json(f"{URLS['polypizza']}/v1.1/search/{urllib.parse.quote(query)}?{urllib.parse.urlencode(p)}", "polypizza", headers)
    return [_pp_result(m) for m in d.get("results", [])]


def _pp_info(id_):
    s = _pp_result(_json(f"{URLS['polypizza']}/v1.1/model/{id_}", "polypizza", _pp_headers()))
    return {"name": s["name"], "kind": "model", "license": s["license"], "author": s["author"], "attribution": s["attribution"], "_download": s["download"]}


def _pp_download(id_, info, folder, cap):
    path = folder / f"{id_}.glb"
    _download_file(info["_download"], "polypizza", path, headers=_pp_headers(), cap=cap)
    return {"main_file": str(path), "resolution": ""}


# --- public interface ---

def search(source="all", query="", kind="", category="", license="cc0", limit=10, attrs=None, min_size_m=0, max_faces=0, animated=False):
    """(results, notes). With source 'all' it searches every reachable source in parallel and interleaves the results."""
    source, kind, limit = _source(source), _kind(kind), max(1, min(int(limit), 40))
    _license_set(license)
    words = _words(query)
    jobs = {"polyhaven": lambda: _ph_search(words, kind, category, attrs, min_size_m, max_faces, limit) if _cc0_requested(license) else [],
            "ambientcg": lambda: _ac_search(query, kind, category, limit) if _cc0_requested(license) else [],
            "sketchfab": lambda: _sk_search(query, license, category, limit, max_faces, animated),
            "polypizza": lambda: _pp_search(query, license, category, limit, animated)}
    chosen = [k for k in SOURCES if source == "all" and (kind != "model" or k != "ambientcg") and (kind not in ("texture", "hdri") or k in ("polyhaven", "ambientcg"))
              or k == source]
    results, notes = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = {k: ex.submit(jobs[k]) for k in chosen}
        for k, g in futures.items():
            try:
                results[k] = g.result()
            except AssetError as e:
                if source != "all":
                    raise
                notes.append(str(e))
                results[k] = []
    merged = [r for group in itertools.zip_longest(*[results[k] for k in chosen]) for r in group if r is not None]
    return merged[:limit], notes


def _cc0_requested(license):
    return "cc0" in _license_set(license)


def format_results(results, notes=()):
    """Turns the results into token-cheap lines."""
    if not results:
        lines = ["no results"]
    else:
        counts = {}
        for r in results:
            counts[r["source"]] = counts.get(r["source"], 0) + 1
        lines = [f"{len(results)} results ({', '.join(f'{k} {v}' for k, v in counts.items())}); pass the id to asset_import"]
    for i, r in enumerate(results, 1):
        parts = [f"{i}. {r['source']} {r['id']}", r["kind"], r["name"] if r["name"] != r["id"] else "", r["category"],
                 f"{r['faces']} faces" if r["faces"] else "", "×".join(str(x) for x in r["size_m"]) + " m" if r["size_m"] else "",
                 f"max {r['resolution']}" if r["resolution"] else "", "/".join(str(r["attrs"][k]) for k in ("time_of_day", "weather") if k in (r.get("attrs") or {})),
                 "animated" if r["animated"] else "", r["license"], r["author"]]
        lines.append(" | ".join(p for p in parts if p))
    lines += [f"note: {n}" for n in notes]
    return "\n".join(lines)


def preview_image(results, n=6, size=256):
    """Combines the previews of the first n results into one numbered PNG (Pillow); a preview that does not load becomes an empty tile."""
    from PIL import Image, ImageDraw
    results = results[:max(1, min(n, 6))]

    def fetch(r):
        try:
            headers = _pp_headers() if r["source"] == "polypizza" else None
            return Image.open(io.BytesIO(_fetch(r["preview"], r["source"], headers, PREVIEW_LIMIT))).convert("RGB").resize((size, size))
        except (AssetError, OSError, ValueError):
            return Image.new("RGB", (size, size), (60, 60, 60))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        tiles = list(ex.map(fetch, results))
    cols = 3 if len(tiles) > 4 else min(len(tiles), 2) if len(tiles) > 1 else 1
    rows = -(-len(tiles) // cols)
    canvas = Image.new("RGB", (cols * size, rows * size), (200, 200, 200))
    pen = ImageDraw.Draw(canvas)
    for i, t in enumerate(tiles):
        x, y = (i % cols) * size, (i // cols) * size
        canvas.paste(t, (x, y))
        pen.rectangle((x, y, x + 22, y + 14), fill=(0, 0, 0))
        pen.text((x + 6, y + 2), str(i + 1), fill=(255, 255, 255))
    buf = io.BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue()


def download(source, id, kind="", resolution="1k", format="", accept_license=False, max_mb=MAX_MB):
    """Downloads the asset under ROOT (nothing goes to the network if it is cached). Returns main_file (model, hdri) or maps (texture), license, attribution, warning..."""
    source, kind, id_ = _source(source), _kind(kind), _id(id)
    if source == "all":
        raise AssetError("give a single source to download: " + ", ".join(SOURCES))
    resolution = str(resolution or "1k").lower()
    cap = int(max_mb) << 20
    folder = ROOT / "downloads" / source / id_ / (resolution + (f"_{format}" if format else ""))
    marker = folder / ".done.json"
    meta = None
    if marker.is_file():
        try:
            meta = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = None
    if meta:
        if kind and kind != meta["kind"]:
            raise AssetError(f"{source} {id_}: this is a '{meta['kind']}', requested kind '{kind}'")
        _license_check(meta["license"], accept_license)
        meta["cached"] = True
    else:
        info = {"polyhaven": lambda: _ph_info(id_), "ambientcg": lambda: _ac_info(id_), "sketchfab": lambda: _sk_info(id_), "polypizza": lambda: _pp_info(id_)}[source]()
        if kind and kind != info["kind"]:
            raise AssetError(f"{source} {id_}: this is a '{info['kind']}', requested kind '{kind}'")
        _license_check(info["license"], accept_license)
        if folder.exists():
            shutil.rmtree(folder)                                      # an earlier attempt that was left half-done
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if source == "polyhaven":
                files = _ph_download(id_, info["kind"], resolution, format, folder, cap)
            elif source == "ambientcg":
                files = _ac_download(id_, info["kind"], resolution, format, folder, cap, info["_asset"])
            elif source == "sketchfab":
                files = _sk_download(id_, folder, cap)
            else:
                files = _pp_download(id_, info, folder, cap)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            try:
                folder.parent.rmdir()                                  # if there is no other resolution the id folder goes too
            except OSError:
                pass
            raise
        meta = {"source": source, "id": id_, "name": info["name"], "kind": info["kind"], "license": info["license"], "author": info["author"],
                "attribution": info["attribution"], "folder": str(folder), **files, "cached": False}
        marker.write_text(json.dumps({k: v for k, v in meta.items() if k != "cached"}, ensure_ascii=False), encoding="utf-8")
    meta["warning"] = _license_warning(meta["license"])
    return meta


def _license_check(license, accepted):
    if _is_cc0(license) or accepted:
        return
    raise AssetError(f"license '{license}': not CC0. Attribution and commercial/derivative restrictions are your decision; pass accept_license=True "
                     "if you accept them")


def _license_warning(license):
    if _is_cc0(license):
        return ""
    b = license.upper()
    nc = "NONCOMMERCIAL" in b or re.search(r"(^|[-\s])NC([-\s]|$)", b)
    return "attribution required (see the attribution field)" + ("; NonCommercial: commercial use is NOT allowed" if nc else "")


def _cli(argv=None):
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="tether.assets", description="Search and download from the asset sources.")
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("search")
    s.add_argument("source", nargs="?", default="all")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("--kind", default="")
    s.add_argument("--category", default="")
    s.add_argument("--license", default="cc0")
    s.add_argument("--limit", type=int, default=10)
    d = sub.add_parser("download")
    d.add_argument("source")
    d.add_argument("id")
    d.add_argument("--kind", default="")
    d.add_argument("--resolution", default="1k")
    d.add_argument("--format", default="")
    d.add_argument("--accept-license", action="store_true")
    o = ap.parse_args(argv)
    try:
        if o.command == "search":
            print(format_results(*search(o.source, o.query, o.kind, o.category, o.license, o.limit)))
        else:
            print(json.dumps(download(o.source, o.id, o.kind, o.resolution, o.format, o.accept_license), ensure_ascii=False, indent=1))
    except AssetError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
