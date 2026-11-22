"""Asset sources against a fake server: search filters, downloads, caching, hashes, size caps, redirects, zip-slip, license gate, previews. No network."""
import io
import os
from pathlib import Path

from fakes import FakeAssets, hdr, jpg, patch_assets
from harness import TMP, check


def try_result(f, *a, **k):
    """The result of f; on an exception returns ([], [error text]): a red check shows the error and the rest of the test still runs."""
    try:
        return f(*a, **k)
    except Exception as e:  # noqa: BLE001
        return [], [f"{type(e).__name__}: {e}"]


def run_all():
    print("--- asset sources (fake server: no network, no real downloads)")
    s = FakeAssets()
    assets, undo = patch_assets(s)
    from tether import mcp_server
    try:
        s.ph_model("ArmChair_01", "Arm Chair 01", tags=("chair", "vintage"), faces=5626, size=(848.4, 765.8, 1065.1))
        s.ph_model("Sofa_01", "Sofa 01", cat="Furniture/Seating/Sofas & Couches", tags=("sofa", "fabric"), faces=4101, size=(1571, 658, 797))
        s.ph_model("Lantern_01", "Lantern 01", cat="Decor/Lights", tags=("lantern",), faces=12000, size=(200, 200, 400))
        s.ph_texture("brick_4", "Brick 4", size=(500, 500), tags=("brick", "wall"))
        s.ph_texture("large_floor_tiles", "Large Floor Tiles", size=(2000, 2000), tags=("floor",))
        s.ph_hdri("aarfontein_dawn", "Aarfontein Dawn", time_of_day="sunrise", weather="clear")
        s.ph_hdri("belfast_sunset", "Belfast Sunset", time_of_day="sunset", weather="partly cloudy")
        s.sk_model("a" * 32, "Side Chair", faces=100142)
        s.sk_model("b" * 32, "Chair By", slug="by", label="CC Attribution", faces=3000)
        s.sk_model("c" * 32, "Chair Nc", slug="by-nc", label="CC Attribution-NonCommercial", faces=2000)
        s.pp_model("PPdog00001", "Chair Pizza")
        s.pp_model("PPdog00002", "Chair Credit", license="CC-BY 3.0")

        # --- search ---
        r, n = assets.search("polyhaven", "armchair")
        check("[assets] polyhaven search: name/tag match; fields (kind, size m, license, faces)",
              r and r[0]["id"] == "ArmChair_01" and r[0]["kind"] == "model" and r[0]["license"] == "CC0" and r[0]["size_m"] == [0.848, 0.766, 1.065]
              and r[0]["faces"] == 5626 and r[0]["source"] == "polyhaven", r[:1])
        r, _ = assets.search("polyhaven", "couch")
        check("[assets] synonym: 'couch' -> Sofa_01", [x["id"] for x in r] == ["Sofa_01"], [x["id"] for x in r])
        text = assets.format_results(*assets.search("polyhaven", "aarfontein", kind="hdri"))
        check("[assets] an hdri line shows time of day/weather (sunrise/clear) so the right light can be chosen", "| sunrise/clear |" in text, text)
        check("[assets] AND semantics: both words must match ('armchair lantern' -> no result)", assets.search("polyhaven", "armchair lantern")[0] == [])
        check("[assets] category filter: 'seating' drops the lantern", {x["id"] for x in assets.search("polyhaven", "", kind="model", category="seating")[0]} == {"ArmChair_01", "Sofa_01"})
        check("[assets] max_faces: above 6000 is dropped", {x["id"] for x in assets.search("polyhaven", "", kind="model", max_faces=6000)[0]} == {"ArmChair_01", "Sofa_01"})
        check("[assets] min_size_m: only textures whose real size is >= 1 m", [x["id"] for x in assets.search("polyhaven", "", kind="texture", min_size_m=1)[0]] == ["large_floor_tiles"])
        check("[assets] hdri attribute filter (weather=clear)", [x["id"] for x in assets.search("polyhaven", "", kind="hdri", attrs={"weather": "clear"})[0]] == ["aarfontein_dawn"])
        try:
            assets.search("polyhaven", "", kind="hdri", attrs={"climate": "clear"})
            ok = False
        except assets.AssetError as e:
            ok = "unknown attribute" in str(e) and "weather" in str(e) and "time_of_day" in str(e)
        check("[assets] unknown attribute: lists the valid keys", ok)
        s.log.clear()
        assets.search("polyhaven", "armchair")
        check("[assets] Poly Haven lists come from the cache (no network on the second search)", s.count("/ph/assets") == 0, s.log)
        r, n = assets.search("all", "chair", kind="model")
        check("[assets] all + kind=model: ambientCG is not asked; sources are interleaved (polyhaven, sketchfab, ...)",
              s.count("/ac/") == 0 and r[0]["source"] == "polyhaven" and "sketchfab" in {x["source"] for x in r}, [(x["source"], x["id"]) for x in r])
        check("[assets] all: without a Poly Pizza key a note is added, the other sources still return results", any("assets.json" in x and "polypizza" in x.lower() for x in n), n)
        try:
            assets.search("ambientcg", "x", kind="model")
            ok = False
        except assets.AssetError as e:
            ok = "no models" in str(e)
        check("[assets] a single source asked for an unsupported kind: a clear error", ok)
        s.log.clear()
        r, _ = assets.search("sketchfab", "chair")
        query = next(k["query"] for k in s.log if k["path"] == "/sf/v3/search")
        check("[assets] Sketchfab asks for CC0 only by default (license=cc0, downloadable=true); BY/NC results do not come",
              query["license"] == "cc0" and query["downloadable"] == "true" and [x["id"] for x in r] == ["a" * 32] and r[0]["license"] == "CC0 Public Domain"
              and r[0]["faces"] == 100142, (query, r))
        s.log.clear()
        r, _ = try_result(assets.search, "sketchfab", "chair", license="cc0,by")
        requests = sorted(k["query"].get("license") for k in s.log if k["path"] == "/sf/v3/search")
        check("[assets] license='cc0,by' also brings BY (NC still does not); CC0 comes first", [x["id"] for x in r] == ["a" * 32, "b" * 32], [x["id"] for x in r])
        check("[assets] Sketchfab multiple licenses: the API takes one license value (a comma list gives 400) -> one request per license, results are merged", requests == ["by", "cc0"], requests)
        check("[assets] with multiple licenses the limit applies to the total (limit=1 -> only the CC0 one)",
              [x["id"] for x in try_result(assets.search, "sketchfab", "chair", license="cc0,by", limit=1)[0]] == ["a" * 32])
        check("[assets] max_faces goes to Sketchfab as max_face_count", [x["id"] for x in try_result(assets.search, "sketchfab", "chair", license="cc0,by", max_faces=5000)[0]] == ["b" * 32])
        r, n = try_result(assets.search, "all", "chair", kind="model", license="cc0,by")
        check("[assets] all + license='cc0,by': the Sketchfab BY result comes, no error note for Sketchfab", "b" * 32 in {x["id"] for x in r} and not any("sketchfab" in x.lower() for x in n), (n, [x["id"] for x in r]))
        try:
            assets.search("sketchfab", "chair", license="free")
            ok = False
        except assets.AssetError as e:
            ok = "unknown license" in str(e)
        check("[assets] unknown license: a clear error", ok)
        try:
            assets.search("polypizza", "chair")
            ok = False
        except assets.AssetError as e:
            ok = "API key missing" in str(e) and "assets.json" in str(e) and "POLYPIZZA_KEY" in str(e) and "never paste the key into the chat" in str(e)
        check("[assets] Poly Pizza without a key: says where to get it and where to put it (the key is never pasted into the chat)", ok)
        os.environ["POLYPIZZA_KEY"] = s.pp_key
        s.log.clear()
        r, _ = assets.search("polypizza", "chair")
        check("[assets] Poly Pizza: the x-auth-token header is sent, CC0 by default (License=1), fields are mapped",
              [x["id"] for x in r] == ["PPdog00001"] and next(k for k in s.log if "/search/" in k["path"])["query"].get("License") == "1" and r[0]["faces"] == 1812
              and r[0]["author"] == "Quaternius", r)
        check("[assets] Poly Pizza license='cc0,by': the License filter is dropped", {x["id"] for x in assets.search("polypizza", "chair", license="cc0,by")[0]} == {"PPdog00001", "PPdog00002"})
        s.ac_asset("Bricks113A", "Bricks 113 A", {"Bricks113A_@_Color.jpg": jpg(), "Bricks113A_@_NormalGL.jpg": jpg(), "Bricks113A_@_NormalDX.jpg": jpg(),
                                                   "Bricks113A_@_Roughness.jpg": jpg(), "Bricks113A_@_AmbientOcclusion.jpg": jpg()}, tags=("bricks", "wall"))
        r, _ = assets.search("ambientcg", "bricks", kind="texture")
        check("[assets] ambientCG search: kind texture, CC0, preview address", len(r) == 1 and r[0]["kind"] == "texture" and r[0]["license"] == "CC0" and r[0]["preview"].endswith("/thumb/Bricks113A.png"), r)
        m = assets.format_results(*assets.search("polyhaven", "armchair"))
        check("[assets] format_results: one token-cheap line (source id | kind | name | category | faces | size | resolution | license | author)",
              "1. polyhaven ArmChair_01 | model | Arm Chair 01 | Furniture/Seating/Chairs | 5626 faces | 0.848×0.766×1.065 m | max 4k | CC0 | Kirill Sannikov" in m and "asset_import" in m, m)

        # --- download: Poly Haven ---
        r = assets.download("polyhaven", "ArmChair_01")
        main = Path(r["main_file"])
        check("[assets] polyhaven model: the main file and the extra files (in subfolders) download, 1k is chosen, license/attribution come",
              main.name == "ArmChair_01_1k.gltf" and main.is_file() and (main.parent / "model.bin").is_file() and (main.parent / "textures" / "diff_1k.jpg").is_file()
              and r["resolution"] == "1k" and r["license"] == "CC0" and "Kirill Sannikov" in r["attribution"] and not r["cached"] and r["kind"] == "model", r)
        count = s.count("/dl/")
        r2 = assets.download("polyhaven", "ArmChair_01")
        check("[assets] the second download comes from the cache (no file request to the network)", r2["cached"] and s.count("/dl/") == count and r2["main_file"] == r["main_file"], (count, s.count("/dl/")))
        check("[assets] resolution goes to the nearest: 2k asked, 1k and 4k exist -> 1k", assets.download("polyhaven", "Sofa_01", resolution="2k")["resolution"] == "1k")
        check("[assets] resolution goes to the nearest: 8k asked -> 4k", assets.download("polyhaven", "Lantern_01", resolution="8k")["resolution"] == "4k")
        check("[assets] _nearest: 1k asked from (2k,4k) -> 2k; 3k asked from (1k,2k,4k) -> 2k",
              assets._nearest({"2k": 0, "4k": 0}, "1k") == "2k" and assets._nearest({"1k": 0, "2k": 0, "4k": 0}, "3k") == "2k")
        d = assets.download("polyhaven", "brick_4", kind="texture")
        check("[assets] polyhaven texture: the color/normal/roughness/ao maps download (jpg), the unused ones (displacement) do not",
              sorted(d["maps"]) == ["ao", "color", "normal", "roughness"] and all(Path(p).is_file() and p.endswith(".jpg") for p in d["maps"].values()), d)
        d = assets.download("polyhaven", "brick_4", kind="texture", format="png")
        check("[assets] format='png': maps are png", all(p.endswith(".png") for p in d["maps"].values()), d["maps"])
        h = assets.download("polyhaven", "belfast_sunset")
        check("[assets] polyhaven hdri: the kind is found from the id (not given), the .hdr downloads", h["kind"] == "hdri" and h["main_file"].endswith("_1k.hdr") and Path(h["main_file"]).is_file(), h)
        try:
            assets.download("polyhaven", "ArmChair_01", kind="hdri")
            ok = False
        except assets.AssetError as e:
            ok = "this is a 'model'" in str(e)
        check("[assets] the wrong kind: a 'this is a model, requested hdri' error", ok)

        # --- security ---
        s.ph_model("Broken_01", "Broken", bad_md5=True)
        try:
            assets.download("polyhaven", "Broken_01")
            ok = False
        except assets.AssetError as e:
            ok = "md5 mismatch" in str(e)
        check("[security] a md5 mismatch is rejected, no partial/corrupt file or folder is left", ok and not (assets.ROOT / "downloads" / "polyhaven" / "Broken_01").exists())
        s.ph_model("Huge_01", "Huge", meta_size=900 << 20)
        s.log.clear()
        try:
            assets.download("polyhaven", "Huge_01")
            ok = False
        except assets.AssetError as e:
            ok = "cap 250 MB" in str(e) and "resolution='1k'" in str(e)
        check("[security] if the announced size exceeds the cap no file is downloaded", ok and s.count("/dl/") == 0, s.log)
        original = s.reply

        def custom(path, query, headers):
            if path == "/dl/ph/Long_01.bin":
                return 200, {"Content-Length": str(999 << 20)}, b""
            if path == "/dl/ph/stream.bin":
                return 200, {"X-Stream": "1"}, b"y" * (3 << 20)
            return original(path, query, headers)
        s.reply = custom
        try:
            assets._download_file(f"{s.url}/dl/ph/Long_01.bin", "polyhaven", TMP / "long.bin")
            ok = False
        except assets.AssetError as e:
            ok = "999 MB, cap 250 MB" in str(e)
        check("[security] if Content-Length exceeds the cap it is rejected without reading the body, no partial file is left",
              ok and not (TMP / "long.bin").exists() and not (TMP / "long.bin.part").exists())
        try:
            assets._download_file(f"{s.url}/dl/ph/stream.bin", "polyhaven", TMP / "stream.bin", cap=1 << 20)
            ok = False
        except assets.AssetError as e:
            ok = "1 MB cap was exceeded" in str(e)
        s.reply = original
        check("[security] a stream that announces no length stops when it exceeds the cap, the partial file is deleted",
              ok and not (TMP / "stream.bin").exists() and not (TMP / "stream.bin.part").exists())
        for bad in ("../x", "a/b", "", "x" * 90, "a b"):
            try:
                assets.download("polyhaven", bad)
                ok = False
            except assets.AssetError as e:
                ok = "invalid asset id" in str(e)
            if not ok:
                break
        check("[security] id validation: a path separator, a space, a long and an empty id are rejected", ok)
        try:
            assets._safe_path(TMP / "k", "../../x.txt")
            ok = False
        except assets.AssetError as e:
            ok = "escapes the folder" in str(e)
        check("[security] an extra file path that escapes the folder (../) is rejected", ok)
        url = f"{assets.URLS['polyhaven']}/assets?t=models"
        assets.LOCAL_TEST = False
        s.log.clear()
        try:
            assets._json(url, "polyhaven")
            ok = False
        except assets.AssetError as e:
            ok = "only https" in str(e)
        assets.LOCAL_TEST = True
        check("[security] plain http (outside test mode) is rejected, the request never goes out", ok and not s.log, s.log)
        assets.ALLOWED["polyhaven"] = {"other.example"}
        try:
            assets._json(url, "polyhaven")
            ok = False
        except assets.AssetError as e:
            ok = "is not allowed" in str(e)
        assets.ALLOWED["polyhaven"] = {"127.0.0.1"}
        check("[security] a host that is not on the allow-list is never contacted", ok and not s.log, s.log)

        # --- download: ambientCG (redirect, zip) ---
        d = assets.download("ambientcg", "Bricks113A")
        check("[assets] ambientCG texture: the zip downloads through a redirect and unpacks; color, NormalGL (not DX), roughness, ao maps; jpg, 1k",
              sorted(d["maps"]) == ["ao", "color", "normal", "roughness"] and d["maps"]["normal"].endswith("_NormalGL.jpg") and d["resolution"] == "1k"
              and d["maps"]["color"].endswith("_Color.jpg") and not list(Path(d["folder"]).glob("*.zip")), d)
        s.ac_asset("evil_zip", "Evil", {"a.jpg": jpg()})
        try:
            assets.download("ambientcg", "evil_zip")
            ok = False
        except assets.AssetError as e:
            ok = "is not allowed" in str(e) and "evil.example" in str(e)
        check("[security] a redirect to a host that is not allowed is rejected (the allow-list applies to redirects too)", ok)
        s.ac_asset("name_escape", "Name Escape", {"name_escape_@_Color.jpg": jpg()})
        for a in s.ac["name_escape"]["downloadFolders"]["default"]["downloadFiletypeCategories"]["zip"]["downloads"]:
            a["fileName"] = "../escaped.zip"
        s.log.clear()
        try:
            assets.download("ambientcg", "name_escape")
            ok = False
        except assets.AssetError as e:
            ok = "escapes the folder" in str(e)
        check("[security] if the API's file name tries to escape the folder ('../escaped.zip') the download is rejected before it starts, no file is written",
              ok and s.count("/ac/get") == 0 and not (assets.ROOT / "downloads" / "ambientcg" / "name_escape").exists(), s.log)
        s.ac_asset("slip_01", "Slip", {"../../evil.txt": b"bad", "slip_01_@_Color.jpg": jpg()})
        try:
            assets.download("ambientcg", "slip_01")
            ok = False
        except assets.AssetError as e:
            ok = "escapes the folder" in str(e)
        check("[security] zip-slip: a zip containing '../../evil.txt' is rejected, no file appears outside",
              ok and not (TMP / "evil.txt").exists() and not (assets.ROOT / "evil.txt").exists() and not (assets.ROOT / "downloads" / "ambientcg" / "slip_01").exists())
        s.ac_asset("Sky001", "Sky 001", {"Sky001_@.hdr": hdr(), "Sky001_@.exr": b"EXR"}, kind="HDRI", tags=("sky",))
        d = assets.download("ambientcg", "Sky001")
        check("[assets] ambientCG hdri: the .hdr is found in the package", d["kind"] == "hdri" and d["main_file"].endswith(".hdr") and Path(d["main_file"]).is_file(), d)
        s.ac_asset("TextureEmpty", "Empty", {"unrelated.txt": b"x"})
        try:
            assets.download("ambientcg", "TextureEmpty")
            ok = False
        except assets.AssetError as e:
            ok = "no color map" in str(e)
        check("[assets] ambientCG: a package without a color map gives a clear error", ok)

        # --- download: Sketchfab (token, license gate) ---
        try:
            assets.download("sketchfab", "a" * 32)
            ok = False
        except assets.AssetError as e:
            ok = "API token missing" in str(e) and "SKETCHFAB_TOKEN" in str(e)
        check("[assets] sketchfab download without a token: says where to get it and where to put it", ok)
        os.environ["SKETCHFAB_TOKEN"] = s.sk_token
        s.log.clear()
        d = assets.download("sketchfab", "a" * 32)
        info = next(k for k in s.log if k["path"].endswith("/download"))
        zip_request = next(k for k in s.log if k["path"].startswith("/sfdl/"))
        check("[assets] sketchfab: the download address is fetched with a 'Token' header; no Authorization goes to the signed address; the zip unpacks, the .gltf is found",
              info["headers"]["authorization"] == "Token tst" and "authorization" not in zip_request["headers"] and d["main_file"].endswith("scene.gltf") and Path(d["main_file"]).is_file(), (info, d))
        s.sk_bearer_only = True
        s.sk_model("d" * 32, "Chair Oauth")
        d = assets.download("sketchfab", "d" * 32)
        check("[assets] sketchfab: if 'Token' gives 401, 'Bearer' is tried (OAuth token)", Path(d["main_file"]).is_file())
        s.sk_bearer_only = False
        try:
            assets.download("sketchfab", "b" * 32)
            ok = False
        except assets.AssetError as e:
            ok = "not CC0" in str(e) and "accept_license" in str(e)
        check("[license] a CC-BY asset is not downloaded by default; the error says why and the way out",
              ok and s.count("/sf/v3/models/" + "b" * 32 + "/download") == 0)
        d = assets.download("sketchfab", "b" * 32, accept_license=True)
        check("[license] with accept_license=True it downloads; the warning says 'attribution required', the attribution comes with the author",
              "attribution required" in d["warning"] and "Sketchfab Author" in d["attribution"] and "NOT allowed" not in d["warning"], d)
        try:
            assets.download("sketchfab", "b" * 32)
            ok = False
        except assets.AssetError as e:
            ok = "not CC0" in str(e)
        check("[license] a cached CC-BY asset is not handed out without acceptance either (the gate also applies to the cache)", ok)
        d = assets.download("sketchfab", "c" * 32, accept_license=True)
        check("[license] NonCommercial: the warning says commercial use is not allowed", "commercial use is NOT allowed" in d["warning"], d["warning"])
        check("[license] a CC0 asset has no warning", assets.download("polyhaven", "ArmChair_01")["warning"] == "")

        # --- download: Poly Pizza ---
        os.environ.pop("POLYPIZZA_KEY")
        try:
            assets.download("polypizza", "PPdog00001")
            ok = False
        except assets.AssetError as e:
            ok = "API key missing" in str(e)
        check("[assets] poly pizza download without a key: a clear error", ok)
        os.environ["POLYPIZZA_KEY"] = s.pp_key
        s.log.clear()
        d = assets.download("polypizza", "PPdog00001")
        check("[assets] poly pizza: the .glb downloads; the key header also goes to the static server; the attribution comes from the Attribution field",
              Path(d["main_file"]).read_bytes() == b"glTF-fake" and all(k["headers"].get("x-auth-token") == s.pp_key for k in s.log if k["path"].startswith(("/ppdl/", "/pp/")))
              and "Quaternius" in d["attribution"], d)

        # --- preview sheet ---
        results, _ = assets.search("all", "chair", kind="model", limit=4)
        png_bytes = assets.preview_image(results, 4)
        from PIL import Image
        im = Image.open(io.BytesIO(png_bytes))
        check(f"[assets] preview sheet: small images of {len(results)} results in one PNG (2x2 grid, 256 px tiles)", len(results) == 4 and im.size == (512, 512), (len(results), im.size))
        s.files.pop("/thumb/a" + "a" * 31 + ".png", None)
        im = Image.open(io.BytesIO(assets.preview_image(results, 6)))
        check("[assets] a result whose preview does not load becomes an empty tile, the sheet is still produced", im.size[0] in (512, 768) and im.size[1] >= 256, im.size)
        out = mcp_server.asset_search(source="polyhaven", query="armchair", previews=1)
        check("[assets] MCP asset_search: a text line + a preview image (PNG) come back",
              len(out) == 2 and isinstance(out[0], str) and "ArmChair_01" in out[0] and out[1].data[:8] == b"\x89PNG\r\n\x1a\n", [type(x).__name__ for x in out])
        check("[assets] MCP asset_search: without previews only text", len(mcp_server.asset_search(source="polyhaven", query="armchair")) == 1)
        try:
            mcp_server.asset_search(source="no_such_source", query="x")
            ok = False
        except RuntimeError as e:
            ok = "unknown source" in str(e)
        check("[assets] MCP asset_search: an error comes back as RuntimeError with its explanation", ok)
    finally:
        undo()
