"""A local HTTP server that mimics the real response shapes of the asset providers (Poly Haven, ambientCG, Sketchfab, Poly Pizza) and serves tiny files,
plus a few file builders. Nothing here touches the network."""
import hashlib
import http.server
import io
import json
import os
import threading
import urllib.parse
import zipfile

from harness import TMP


def png(color=(200, 50, 50), size=(16, 16)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


def jpg(color=(120, 120, 120)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(b, "JPEG")
    return b.getvalue()


def make_zip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return b.getvalue()


def hdr():
    """Radiance .hdr: 4x4 flat (no RLE) pixels; Blender can load it."""
    return b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y 4 +X 4\n" + bytes((128, 128, 128, 129)) * 16


class FakeAssets:
    def __init__(self):
        self.files, self.log = {}, []                 # files: server path -> bytes; log: every request
        self.ph = {"models": {}, "textures": {}, "hdris": {}}
        self.ph_files = {}
        self.ac = {}
        self.sk = {}
        self.pp = {}
        self.sk_token, self.sk_bearer_only, self.pp_key = "tst", False, "pp-tst"
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                p = urllib.parse.urlsplit(self.path)
                entry = {"path": p.path, "query": dict(urllib.parse.parse_qsl(p.query)), "headers": {k.lower(): v for k, v in self.headers.items()}}
                fake.log.append(entry)
                code, headers, data = fake.reply(entry["path"], entry["query"], entry["headers"])
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                if "Content-Length" not in headers and "X-Stream" not in headers:      # X-Stream: stream without announcing a length (ends when the connection closes)
                    self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()

    def count(self, prefix):
        return sum(1 for k in self.log if k["path"].startswith(prefix))

    # --- routing ---
    def reply(self, path, query, headers):
        def J(o, code=200):
            return code, {"Content-Type": "application/json"}, json.dumps(o).encode()
        if path.startswith("/ppdl/") and headers.get("x-auth-token") != self.pp_key:
            return 401, {}, b"no key"
        if path.startswith("/sfdl/") and "authorization" in headers:
            return 400, {}, b"an Authorization header is not sent to a signed S3 address"
        if path in self.files:
            return 200, {}, self.files[path]
        if path == "/ph/assets":
            return J(self.ph[query["t"]])
        if path.startswith("/ph/info/") and path.rsplit("/", 1)[1] in self.ph_files:
            i = path.rsplit("/", 1)[1]
            g = next(t[i] for t in self.ph.values() if i in t)
            return J({"name": g["name"], "type": g["type"], "authors": g["authors"], "categories": g["categories"]})
        if path.startswith("/ph/files/") and path.rsplit("/", 1)[1] in self.ph_files:
            return J(self.ph_files[path.rsplit("/", 1)[1]])
        if path == "/ac/api/v2/full_json":
            found = [a for a in self.ac.values() if (not query.get("type") or a["dataType"] == query["type"]) and (not query.get("id") or a["assetId"] == query["id"])
                     and (not query.get("q") or query["q"].lower() in (a["assetId"] + " " + " ".join(a["tags"])).lower())]
            return J({"numberOfResults": len(found), "foundAssets": found})
        if path == "/ac/get":
            name = query["file"]
            return 302, {"Location": "https://evil.example/x.zip" if name.startswith("evil") else f"{self.url}/acdl/{name}"}, b""
        if path == "/sf/v3/search":
            if "," in query.get("license", ""):         # the real API wants a single license value; a comma list gives 400 (measured live)
                return J({"detail": {"license": [f"Select a valid choice. {query['license']} is not one of the available choices."]}}, 400)
            lic = {query["license"]} if query.get("license") else set()
            found = [m for m in self.sk.values() if (not lic or m["license"]["slug"] in lic) and query.get("q", "").lower() in m["name"].lower()
                     and (not query.get("max_face_count") or m["faceCount"] <= int(query["max_face_count"]))]
            return J({"results": found[:int(query.get("count", 24))], "next": None})
        if path.startswith("/sf/v3/models/"):
            parts = path.split("/")
            m = self.sk.get(parts[4])
            if m is None:
                return J({"detail": "not found"}, 404)
            if len(parts) == 5:
                return J(m)
            expected = ("Bearer " if self.sk_bearer_only else "Token ") + self.sk_token
            if headers.get("authorization") != expected:
                return J({"detail": "unauthorized"}, 401)
            return J({"gltf": {"url": f"{self.url}/sfdl/{m['uid']}.zip", "size": len(self.files[f"/sfdl/{m['uid']}.zip"]), "expires": 300}})
        if path.startswith("/pp/v1.1/"):
            if headers.get("x-auth-token") != self.pp_key:
                return J({"error": "You need an API key to do that dingus"}, 401)
            parts = path.split("/")
            if parts[3] == "search":
                cc0 = query.get("License")
                found = [m for m in self.pp.values() if parts[4].lower() in m["Title"].lower() and (cc0 is None or (cc0 == "1") == m["Licence"].startswith("CC0"))]
                return J({"total": len(found), "results": found})
            m = self.pp.get(parts[4])
            return J(m) if m else J({"error": "not found"}, 404)
        return 404, {}, b"not found"

    # --- fixture builders ---
    def _add_file(self, path, data):
        self.files[path] = data
        return {"url": f"{self.url}{path}", "md5": hashlib.md5(data).hexdigest(), "size": len(data)}

    def ph_model(self, id_, name, cat="Furniture/Seating/Chairs", tags=(), faces=1000, size=(1000, 1000, 1000), main=b'{"asset":{"version":"2.0"}}', extras=None,
                 bad_md5=False, meta_size=None):
        extras = {"model.bin": b"BIN" * 20, "textures/diff_1k.jpg": jpg()} if extras is None else extras
        g = {"name": name, "categories": [c.lower() for c in cat.split("/")[:2]], "category": cat, "type": 2, "tags": list(tags), "authors": {"Kirill Sannikov": "All"},
             "max_resolution": [4096, 4096], "polycount": faces, "dimensions": list(size), "description": f"Free CC0 {name}"}
        self.ph["models"][id_] = g
        res = {}
        for c in ("1k", "4k"):
            d = self._add_file(f"/dl/ph/{id_}/{id_}_{c}.gltf", main)
            d["include"] = {rel: self._add_file(f"/dl/ph/{id_}/{rel}", b) for rel, b in extras.items()}
            if bad_md5:
                d["md5"] = "0" * 32
            if meta_size:
                d["size"] = meta_size
            res[c] = {"gltf": d}
        self.ph_files[id_] = {"gltf": res}

    def ph_texture(self, id_, name, size=(500, 500), tags=()):
        self.ph["textures"][id_] = {"name": name, "categories": ["wall", "brick"], "category": "", "type": 1, "tags": list(tags), "authors": {"Rob Tuytel": "All"},
                                    "max_resolution": [8192, 8192], "dimensions": list(size), "description": ""}
        files = {}
        for key, short in (("Diffuse", "diff"), ("nor_gl", "nor_gl"), ("Rough", "rough"), ("AO", "ao")):
            files[key] = {c: {"jpg": self._add_file(f"/dl/ph/{id_}/{id_}_{short}_{c}.jpg", jpg((90, 90, 90))),
                              "png": self._add_file(f"/dl/ph/{id_}/{id_}_{short}_{c}.png", png((90, 90, 90)))} for c in ("1k", "2k")}
        self.ph_files[id_] = files

    def ph_hdri(self, id_, name, **attrs):
        self.ph["hdris"][id_] = {"name": name, "categories": ["outdoor", "sunrise-sunset"], "category": "Skies/Sunsets", "type": 0, "tags": ["sky"], "authors": {"Greg Zaal": "All"},
                                 "max_resolution": [24576, 12288], "attributes": attrs, "description": ""}
        self.ph_files[id_] = {"hdri": {c: {"hdr": self._add_file(f"/dl/ph/{id_}/{id_}_{c}.hdr", hdr()), "exr": self._add_file(f"/dl/ph/{id_}/{id_}_{c}.exr", b"EXR")}
                                       for c in ("1k", "2k")}}

    def ac_asset(self, id_, name, zip_files, kind="Material", tags=()):
        d = []
        for c in ("1K", "2K"):
            for fmt in (("JPG", "PNG") if kind == "Material" else ("",)):
                attribute = f"{c}-{fmt}" if fmt else c
                zip_name = f"{id_}_{attribute}.zip"
                data = make_zip({k.replace("@", attribute): v for k, v in zip_files.items()})
                self.files[f"/acdl/{zip_name}"] = data
                d.append({"attribute": attribute, "downloadLink": f"{self.url}/ac/get?file={zip_name}", "fileName": zip_name, "size": len(data), "filetype": "zip", "zipContent": []})
        self.ac[id_] = {"assetId": id_, "dataType": kind, "displayName": name, "tags": list(tags), "displayCategory": "Bricks",
                        "previewImage": {"256-PNG": f"{self.url}/thumb/{id_}.png"},
                        "downloadFolders": {"default": {"downloadFiletypeCategories": {"zip": {"downloads": d}}}}}
        self.files[f"/thumb/{id_}.png"] = png((50, 120, 200), (64, 64))

    def sk_model(self, uid, name, slug="cc0", label="CC0 Public Domain", faces=1000, zip_files=None):
        self.sk[uid] = {"uid": uid, "name": name, "license": {"uid": "x" + uid, "label": label, "slug": slug}, "faceCount": faces, "vertexCount": faces // 2, "isDownloadable": True,
                        "animationCount": 0, "user": {"username": "u", "displayName": "Sketchfab Author"}, "viewerUrl": f"https://sketchfab.com/3d-models/{uid}",
                        "categories": [{"name": "furniture-home"}], "tags": [{"name": "chair"}],
                        "thumbnails": {"images": [{"width": 64, "url": f"{self.url}/thumb/{uid}.png"}, {"width": 256, "url": f"{self.url}/thumb/{uid}.png"}]}}
        self.files[f"/sfdl/{uid}.zip"] = make_zip(zip_files or {"scene.gltf": b'{"asset":{"version":"2.0"}}', "scene.bin": b"BIN", "textures/a.png": png()})
        self.files[f"/thumb/{uid}.png"] = png((200, 120, 50), (64, 64))

    def pp_model(self, id_, name, license="CC0 1.0", glb=b"glTF-fake"):
        self.pp[id_] = {"ID": id_, "Title": name, "Attribution": f'"{name}" by Quaternius, https://poly.pizza/m/{id_}', "Thumbnail": f"{self.url}/thumb/{id_}.png",
                        "Download": f"{self.url}/ppdl/{id_}.glb", "Tri Count": 1812, "Creator": {"Username": "Quaternius"}, "Category": "Animals", "Tags": ["dog"],
                        "Licence": license, "Animated": False}
        self.files[f"/ppdl/{id_}.glb"] = glb
        self.files[f"/thumb/{id_}.png"] = png((60, 200, 90), (64, 64))


def patch_assets(s):
    """Points tether.assets at the fake server (the cache and the settings file are temporary, the keys are removed); returns (module, undo)."""
    from tether import assets
    saved = (assets.ROOT, assets.CONFIG, dict(assets.URLS), {k: set(v) for k, v in assets.ALLOWED.items()}, assets.LOCAL_TEST)
    keys = {k: os.environ.pop(k, None) for k in ("SKETCHFAB_TOKEN", "POLYPIZZA_KEY")}
    assets.ROOT, assets.CONFIG, assets.LOCAL_TEST = TMP / "assets_cache", TMP / "config" / "assets.json", True
    assets.URLS.update({"polyhaven": f"{s.url}/ph", "ambientcg": f"{s.url}/ac", "sketchfab": f"{s.url}/sf", "polypizza": f"{s.url}/pp"})
    for k in assets.ALLOWED:
        assets.ALLOWED[k] = {"127.0.0.1"}

    def undo():
        s.close()
        assets.ROOT, assets.CONFIG, assets.URLS, assets.ALLOWED, assets.LOCAL_TEST = saved
        for k, v in keys.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    return assets, undo
