"""Generative 3D (Rodin, Hunyuan) against a fake server that mimics the documented response shapes: nothing is sent without accept_cost, request fields,
polling, the download and its cache, error bodies, the Hunyuan signature headers, redirects to private hosts, input validation. No network."""
import http.server
import json
import os
import re
import threading
import time

from fakes import make_zip, png
from harness import TMP, check

KEYS = ("RODIN_API_KEY", "TENCENT_SECRET_ID", "TENCENT_SECRET_KEY")
GLB = b"glTF-generated-model"


class FakeGen:
    def __init__(self):
        self.log, self.rodin_states, self.hy_states, self.hy_files = [], ["Waiting"], ["WAIT"], []
        self.rodin_error = self.hy_error = None
        self.rn = self.hn = 0
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, data, headers=None):
                self.send_response(code)
                for k, v in (headers or {"Content-Type": "application/json"}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.log.append({"method": "GET", "path": self.path})
                if self.path == "/files/model.glb":
                    return self.send(200, GLB, {})
                if self.path == "/files/package.zip":
                    return self.send(200, make_zip({"obj/model.obj": b"o cube\n"}), {})
                if self.path == "/files/redirect":
                    return self.send(302, b"", {"Location": "https://192.168.0.5/x"})
                self.send(404, b"")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                e = {"method": "POST", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
                fake.log.append(e)
                J = lambda o, c=201: self.send(c, json.dumps(o).encode())      # noqa: E731
                if self.path == "/api/v2/rodin":
                    if fake.rodin_error:
                        return J({"error": fake.rodin_error, "message": "no plan"})
                    fake.rn += 1                                              # every job gets its own id, as the real service does
                    return J({"message": "Submitted.", "uuid": f"11111111-2222-3333-4444-{555555555554 + fake.rn:012d}",
                              "jobs": {"uuids": ["j1"], "subscription_key": "sub-key"}, "consumed": 0.5})
                if self.path == "/api/v2/status":
                    return J({"jobs": [{"uuid": "j1", "status": fake.rodin_states[0]}]})
                if self.path == "/api/v2/download":
                    return J({"list": [{"name": "preview.png", "url": f"{fake.url}/files/x.png"}, {"name": "model.glb", "url": f"{fake.url}/files/model.glb"}]})
                if self.path == "/":
                    action = e["headers"].get("x-tc-action")
                    if fake.hy_error:
                        return J({"Response": {"Error": {"Code": "AuthFailure.SignatureFailure", "Message": "bad signature"}, "RequestId": "r"}}, 200)
                    if action == "SubmitHunyuanTo3DProJob":
                        fake.hn += 1
                        return J({"Response": {"JobId": str(1357237233311637503 + fake.hn), "RequestId": "r"}}, 200)
                    if action == "QueryHunyuanTo3DProJob":
                        s = fake.hy_states[0]
                        return J({"Response": {"Status": s, "ErrorCode": "E1" if s == "FAIL" else "", "ErrorMessage": "content rejected" if s == "FAIL" else "",
                                               "ResultFile3Ds": fake.hy_files if s == "DONE" else [], "RequestId": "r"}}, 200)
                self.send(404, b"")
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def posts(self):
        return [e for e in self.log if e["method"] == "POST"]


def raises(f, *a, **k):
    from tether.assets import AssetError
    try:
        f(*a, **k)
    except AssetError as e:
        return str(e)
    return None


def run_all():
    print("--- generative 3D (fake server: no network, nothing is spent)")
    from tether import assets, generate, mcp_server
    s = FakeGen()
    saved = (assets.ROOT, assets.CONFIG, assets.LOCAL_TEST, dict(generate.URLS), generate.JOBS, {k: os.environ.pop(k, None) for k in KEYS})
    assets.ROOT, assets.CONFIG, assets.LOCAL_TEST = TMP / "gen_cache", TMP / "gen_config" / "assets.json", True
    generate.URLS.update({"rodin": s.url, "hunyuan": s.url})
    generate.JOBS = TMP / "gen_jobs"
    try:
        # --- keys and the cost gate ---
        e = raises(generate.start, "rodin", "a chair")
        check("[generate] a missing key names where to get it and where to put it, and asks not to paste it into the chat",
              e and "RODIN_API_KEY" in e and "never paste" in e and "hyper3d.ai" in e, e)
        os.environ["RODIN_API_KEY"] = "rk-test"
        r = generate.start("rodin", "a wooden chair")
        check("[generate] without accept_cost nothing is sent: the answer shows the cost and the next call", not r["sent"] and not s.posts()
              and "NOT SENT" in r["text"] and "0.5 Rodin credits" in r["text"] and "accept_cost=True" in r["text"], (r, len(s.posts())))
        check("[generate] the Extreme-High tier is priced higher; Hunyuan prices add up (PBR + face count)",
              "1 Rodin credits" in generate.cost("rodin", "Gen-2.5-Extreme-High") and generate.cost("hunyuan", pbr=True, face_count=5000).startswith("40 Hunyuan credits"))

        # --- Rodin ---
        r = generate.start("rodin", "a wooden chair", accept_cost=True)
        post = s.posts()[0]
        form = post["body"].decode("latin-1")
        fields = dict(re.findall(r'name="(\w+)"\r\n\r\n([^\r]*)\r\n', form))
        job = r["job"]
        check("[generate] rodin start: multipart with Bearer key, the default tier, Raw mesh, glb output and the prompt",
              r["sent"] and job == "rodin-11111111-2222-3333-4444-555555555555" and post["headers"]["authorization"] == "Bearer rk-test"
              and post["headers"]["content-type"].startswith("multipart/form-data; boundary=")
              and fields == {"tier": "Gen-2.5-Low", "mesh_mode": "Raw", "geometry_file_format": "glb", "prompt": "a wooden chair"}, (fields, r))
        check("[generate] the job record keeps the subscription key (needed for polling) under the user's folder",
              json.loads((generate.JOBS / f"{job}.json").read_text(encoding="utf-8"))["subscription_key"] == "sub-key")
        st = generate.status(job)
        check("[generate] status waiting: polls with the subscription key (not the task id)", st["state"] == "waiting"
              and json.loads(s.posts()[-1]["body"]) == {"subscription_key": "sub-key"}, st)
        s.rodin_states[0] = "Generating"
        check("[generate] status running", generate.status(job)["state"] == "running")
        s.rodin_states[0] = "Done"
        n = len(s.log)
        st = generate.status(job)
        files = [e for e in s.log[n:] if e["method"] == "GET"]
        dl = [e for e in s.log[n:] if e["path"] == "/api/v2/download"]
        main = assets.ROOT / "generated" / job / "model.glb"
        check("[generate] done: the task id goes to download, the glb (not the preview) is fetched once, the marker is written",
              st["state"] == "done" and len(dl) == 1 and json.loads(dl[0]["body"]) == {"task_uuid": "11111111-2222-3333-4444-555555555555"}
              and [f["path"] for f in files] == ["/files/model.glb"] and main.read_bytes() == GLB and (main.parent / ".done.json").is_file(), (st, files))
        n = len(s.log)
        check("[generate] polling a finished job again goes nowhere near the network", generate.status(job)["state"] == "done" and len(s.log) == n)
        m = generate.fetch(job)
        check("[generate] fetch: the same dict assets.download returns (model, file, provider license, cached)",
              m["kind"] == "model" and m["source"] == "rodin" and m["id"] == job and m["main_file"] == str(main) and m["license"].startswith("AI-generated")
              and m["cached"] and m["warning"] == "", m)

        # --- failures and validation ---
        s.rodin_states[0] = "Failed"
        job2 = generate.start("rodin", "other", accept_cost=True)["job"]
        check("[generate] a Failed job reports failed", generate.status(job2)["state"] == "failed")
        s.rodin_error = "API_INSUFFICIENT_FUNDS"
        e = raises(generate.start, "rodin", "x", accept_cost=True)
        check("[generate] a rejection that arrives with a 2xx status and an error field is raised with its code", e and "API_INSUFFICIENT_FUNDS" in e, e)
        s.rodin_error = None
        check("[generate] unknown tier / nothing to generate from / a hunyuan-only option on rodin are refused before sending",
              "unknown tier" in (raises(generate.start, "rodin", "x", tier="Gen-9", accept_cost=True) or "")
              and "prompt" in (raises(generate.start, "rodin", "", accept_cost=True) or "")
              and "Hunyuan only" in (raises(generate.start, "rodin", "x", face_count=5000, accept_cost=True) or ""))
        a, b = TMP / "a.png", TMP / "b.txt"
        a.write_bytes(png())
        b.write_text("x")
        check("[generate] an image with a wrong extension, a missing file and six images are refused",
              "use .jpg" in (raises(generate.start, "rodin", "", [str(b)], accept_cost=True) or "")
              and "could not be read" in (raises(generate.start, "rodin", "", [str(TMP / "none.png")], accept_cost=True) or "")
              and "at most 5" in (raises(generate.start, "rodin", "", [str(a)] * 6, accept_cost=True) or ""))
        n = len(s.posts())
        r = generate.start("rodin", "", [str(a)], accept_cost=True)
        body = s.posts()[n]["body"]
        check("[generate] image to 3D: the picture is uploaded in the images field with its file name and no prompt field",
              r["sent"] and b'name="images"; filename="a.png"' in body and b"Content-Type: image/png" in body and b'name="prompt"' not in body)

        # --- Hunyuan ---
        os.environ["TENCENT_SECRET_ID"] = "AKIDtest"
        e = raises(generate.start, "hunyuan", "a lamp", accept_cost=True)
        check("[generate] hunyuan needs both the secret id and the secret key", e and "SecretKey" in e and "TENCENT_SECRET_KEY" in e, e)
        os.environ["TENCENT_SECRET_KEY"] = "sk-test"
        check("[generate] hunyuan: prompt + image, two images, tier and a face count out of range are refused",
              "not be combined" in (raises(generate.start, "hunyuan", "x", [str(a)], accept_cost=True) or "")
              and "at most 1 image" in (raises(generate.start, "hunyuan", "", [str(a)] * 2, accept_cost=True) or "")
              and "does not apply" in (raises(generate.start, "hunyuan", "x", tier="Gen-2", accept_cost=True) or "")
              and "3000 to 1500000" in (raises(generate.start, "hunyuan", "x", face_count=10, accept_cost=True) or ""))
        n = len(s.posts())
        r = generate.start("hunyuan", "a brass lamp", pbr=True, face_count=20000, accept_cost=True)
        post = s.posts()[n]
        h = post["headers"]
        check("[generate] hunyuan start: JSON body with the prompt, PBR and face count; common headers; TC3 Authorization for the ai3d service",
              r["job"] == "hunyuan-1357237233311637504" and json.loads(post["body"]) == {"Prompt": "a brass lamp", "EnablePBR": True, "FaceCount": 20000}
              and h["x-tc-action"] == "SubmitHunyuanTo3DProJob" and h["x-tc-version"] == "2025-05-13" and h["x-tc-region"] == "ap-guangzhou"
              and re.fullmatch(r"TC3-HMAC-SHA256 Credential=AKIDtest/\d{4}-\d\d-\d\d/ai3d/tc3_request, SignedHeaders=content-type;host, Signature=[0-9a-f]{64}",
                               h["authorization"]) and abs(int(h["x-tc-timestamp"]) - time.time()) < 30, (h, post["body"]))
        hy = r["job"]
        check("[generate] hunyuan waits then runs", generate.status(hy)["state"] == "waiting" and (s.hy_states.__setitem__(0, "RUN") or generate.status(hy)["state"] == "running"))
        s.hy_states[0], s.hy_files = "DONE", [{"Type": "OBJ", "Url": f"{s.url}/files/package.zip", "PreviewImageUrl": ""}, {"Type": "GLB", "Url": f"{s.url}/files/model.glb"}]
        st = generate.status(hy)
        main = assets.ROOT / "generated" / hy / "model.glb"
        check("[generate] hunyuan done: the GLB is preferred over the OBJ package", st["state"] == "done" and main.read_bytes() == GLB, st)
        hy2 = generate.start("hunyuan", "", [str(a)], accept_cost=True)["job"]
        sent = json.loads(s.posts()[-1]["body"])
        s.hy_files = [{"Type": "OBJ", "Url": f"{s.url}/files/package.zip"}]
        st = generate.status(hy2)
        m = generate.fetch(hy2)
        check("[generate] hunyuan from a picture sends it as base64; an OBJ-only result is unpacked and its .obj is the main file",
              sent.get("ImageBase64") and "Prompt" not in sent and st["state"] == "done" and m["main_file"].endswith("model.obj"), (sent.keys(), m))
        s.hy_states[0] = "FAIL"
        hy3 = generate.start("hunyuan", "x", accept_cost=True)["job"]
        check("[generate] hunyuan FAIL carries the provider's code and message", "content rejected" in generate.status(hy3)["text"] and generate.status(hy3)["state"] == "failed")
        s.hy_error = True
        e = raises(generate.start, "hunyuan", "x", accept_cost=True)
        check("[generate] a Response.Error is raised with its code and message", e and "SignatureFailure" in e and "bad signature" in e, e)
        s.hy_error = None

        # --- downloads, ids, routing ---
        e = raises(generate._download, "rodin", "j", [("model.glb", f"{s.url}/files/redirect")], assets.ROOT / "generated" / "red", 1 << 20)
        check("[generate] a result address that redirects to a private address is refused and leaves no folder",
              e and "not allowed" in e and not (assets.ROOT / "generated" / "red").exists(), e)
        check("[generate] a result list without glb/fbx/obj is an error that names what came back",
              "preview.png" in (raises(generate._download, "rodin", "j", [("preview.png", f"{s.url}/files/x.png")], assets.ROOT / "generated" / "none", 1 << 20) or ""))
        check("[generate] an unknown or malformed job id is refused", "unknown job" in (raises(generate.status, "rodin-nope") or "")
              and "invalid job id" in (raises(generate.status, "../../etc") or "") and "unknown provider" in (raises(generate.start, "meshy", "x") or ""))
        s.rodin_states[0] = "Waiting"
        job3 = generate.start("rodin", "pending", accept_cost=True)["job"]
        check("[generate] fetch of a job that is not done says so instead of importing", "waiting" in (raises(generate.fetch, job3) or ""))

        seen = {}
        real = mcp_server.client.call
        mcp_server.client.call = lambda app, name, args=None, timeout=60: (seen.update(args or {}) or {"result": {
            "kind": "model", "root": "r", "objects": 1, "tris": 12, "materials": [], "scale": 1.0, "size": [1, 1, 1], "min": [0, 0, 0], "max": [1, 1, 1],
            "location": [0, 0, 0], "animations": []}})
        try:
            text = mcp_server.asset_import("blender", "rodin", job, target_size=1.0)
        finally:
            mcp_server.client.call = real
        check("[generate] asset_import(source='rodin', id=<job>) hands the downloaded glb to the Blender importer with the provider license",
              seen.get("file") == str(assets.ROOT / "generated" / job / "model.glb") and seen["source"] == "rodin" and seen["license"].startswith("AI-generated")
              and seen["target_size"] == 1.0 and "imported: rodin" in text, (seen, text))
        text = mcp_server.generate_start("rodin", "a chair")
        check("[generate] the MCP tool generate_start without accept_cost returns the NOT SENT text", text.startswith("NOT SENT"), text)
    finally:
        s.srv.shutdown()
        assets.ROOT, assets.CONFIG, assets.LOCAL_TEST, urls, generate.JOBS, keys = saved
        generate.URLS.update(urls)
        for k in KEYS:
            os.environ.pop(k, None)
        for k, v in keys.items():
            if v is not None:
                os.environ[k] = v
