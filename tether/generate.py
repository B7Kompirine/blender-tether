"""Generative 3D from a prompt or pictures: Hyper3D Rodin and Tencent Hunyuan3D (Pro). A job costs the provider's credits, so `start` only describes what it
would send until accept_cost=True; `status` polls the job and downloads the finished model; asset_import(source='rodin'|'hunyuan', id=<job>) brings it in.
Keys, from the environment or ~/.tether/assets.json: RODIN_API_KEY (rodin_key); TENCENT_SECRET_ID + TENCENT_SECRET_KEY (tencent_secret_id, tencent_secret_key).
The API hosts are fixed; result files come from whatever public https host the authenticated API names, so redirects, private addresses and size are checked.

Usage: python -m tether.generate start rodin "a wooden chair" [--accept-cost] | status <job>
"""
import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import re
import shutil
import socket
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

from . import assets
from .agent import HOME
from .assets import AssetError

PROVIDERS = ("rodin", "hunyuan")
URLS = {"rodin": "https://api.hyper3d.com", "hunyuan": "https://ai3d.tencentcloudapi.com"}
JOBS = HOME / "jobs"
RODIN_TIERS = ("Gen-2.5-Extreme-Low", "Gen-2.5-Low", "Gen-2.5-Medium", "Gen-2.5-High", "Gen-2.5-Extreme-High", "Gen-2", "Sketch", "Regular", "Detail", "Smooth")
RODIN_DEFAULT_TIER = "Gen-2.5-Low"             # the highest tier a basic API plan may use
HUNYUAN_VERSION, HUNYUAN_REGION, HUNYUAN_SERVICE = "2025-05-13", "ap-guangzhou", "ai3d"
IMAGE_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
IMAGE_MAX = 6 << 20
MAX_IMAGES = {"rodin": 5, "hunyuan": 1}
PROMPT_MAX = {"rodin": 2000, "hunyuan": 1024}
PICK = {"glb": 0, "fbx": 1, "obj": 2}      # preference among the result files


# --- hosts and keys ---

def _api_host(provider):
    return lambda host: host == urllib.parse.urlsplit(URLS[provider]).hostname or (assets.LOCAL_TEST and host == "127.0.0.1")


def _public_host(host):
    """Result files: any host that resolves only to public addresses (not loopback, private or link-local)."""
    if assets.LOCAL_TEST and host == "127.0.0.1":
        return True
    try:
        found = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return bool(found) and all(ipaddress.ip_address(f[4][0]).is_global for f in found)
    except (OSError, ValueError):
        return False


for _p in PROVIDERS:
    assets.HOST_CHECKS[_p] = _api_host(_p)
    assets.HOST_CHECKS[_p + "_files"] = _public_host


def _provider(name):
    k = str(name or "").lower().strip()
    if k not in PROVIDERS:
        raise AssetError(f"unknown provider '{name}'; valid: {', '.join(PROVIDERS)}")
    return k


def _credentials(provider):
    if provider == "rodin":
        return (assets._key("rodin_key", "RODIN_API_KEY", "https://hyper3d.ai/workspace/api-dashboard (API key)", "Hyper3D Rodin API key"),)
    return (assets._key("tencent_secret_id", "TENCENT_SECRET_ID", "https://console.cloud.tencent.com/cam/capi (API key)", "Tencent Cloud SecretId"),
            assets._key("tencent_secret_key", "TENCENT_SECRET_KEY", "https://console.cloud.tencent.com/cam/capi (API key)", "Tencent Cloud SecretKey"))


# --- inputs ---

def _read_images(provider, images):
    """[(name, mime, bytes)]: local picture files, checked before anything is sent."""
    out = []
    for i in images or []:
        p = Path(str(i)).expanduser()
        if p.suffix.lower() not in IMAGE_EXT:
            raise AssetError(f"image '{p.name}': use {', '.join(IMAGE_EXT)}")
        try:
            if p.stat().st_size > IMAGE_MAX:
                raise AssetError(f"image '{p.name}' is larger than {IMAGE_MAX >> 20} MB")
            out.append((p.name, IMAGE_EXT[p.suffix.lower()], p.read_bytes()))
        except OSError as e:
            raise AssetError(f"image '{p}' could not be read ({e.strerror or e})") from None
    return out


def _validate(provider, prompt, images, tier, face_count):
    prompt = str(prompt or "").strip()
    if not prompt and not images:
        raise AssetError("give a prompt (text to 3D) or images (picture to 3D)")
    if len(prompt) > PROMPT_MAX[provider]:
        raise AssetError(f"{provider}: the prompt is {len(prompt)} characters, the limit is {PROMPT_MAX[provider]}")
    if len(images) > MAX_IMAGES[provider]:
        raise AssetError(f"{provider}: at most {MAX_IMAGES[provider]} image{'s' if MAX_IMAGES[provider] > 1 else ''}, got {len(images)}")
    if provider == "hunyuan" and prompt and images:
        raise AssetError("hunyuan: a prompt cannot be combined with an image: give one of them")
    if provider == "rodin" and tier and tier not in RODIN_TIERS:
        raise AssetError(f"rodin: unknown tier '{tier}'; valid: {', '.join(RODIN_TIERS)}")
    if provider == "hunyuan" and tier:
        raise AssetError("hunyuan: tier does not apply (Pro is used)")
    if face_count and not (provider == "hunyuan" and 3000 <= face_count <= 1500000):
        raise AssetError("face_count: Hunyuan only, 3000 to 1500000" if provider == "hunyuan" else "face_count applies to Hunyuan only")
    return prompt


def cost(provider, tier="", pbr=False, face_count=0):
    """Credits the provider's documentation lists for one job, as text."""
    if provider == "rodin":
        return f"{0.5 + (0.5 if (tier or RODIN_DEFAULT_TIER) == 'Gen-2.5-Extreme-High' else 0):g} Rodin credits"
    c = 20 + (10 if pbr else 0) + (10 if face_count else 0)
    return f"{c} Hunyuan credits (about {c * 0.1:.1f} to {c * 0.12:.1f} CNY; a free package of 100 credits can be claimed once in the console)"


# --- Rodin ---

def _multipart(fields, files):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for name, mime, data in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="images"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _rodin(path, key, payload=None, body=None):
    headers = {"Authorization": f"Bearer {key}"}
    if body is not None:
        data, headers["Content-Type"] = body
    else:
        data, headers["Content-Type"] = json.dumps(payload).encode(), "application/json"
    d = assets._json(f"{URLS['rodin']}/api/v2/{path}", "rodin", headers, data=data)
    if isinstance(d, dict) and d.get("error"):                       # a rejection comes back with a 2xx status and an error field
        raise AssetError(f"rodin: {d['error']}: {d.get('message', '')}".rstrip(": "))
    return d


def _rodin_start(key, prompt, images, tier, mesh_mode):
    fields = {"tier": tier or RODIN_DEFAULT_TIER, "mesh_mode": mesh_mode or "Raw", "geometry_file_format": "glb"}
    if prompt:
        fields["prompt"] = prompt
    d = _rodin("rodin", key, body=_multipart(fields, images))
    if not d.get("uuid") or not (d.get("jobs") or {}).get("subscription_key"):
        raise AssetError(f"rodin: the answer has no job id: {json.dumps(d)[:200]}")
    return d["uuid"], {"subscription_key": d["jobs"]["subscription_key"]}


def _rodin_status(rec, key):
    jobs = (_rodin("status", key, {"subscription_key": rec["subscription_key"]}).get("jobs")) or []
    states = [str(j.get("status", "")).lower() for j in jobs]
    if "failed" in states:
        return "failed", "the provider reports the job as Failed"
    if states and all(s == "done" for s in states):
        return "done", ""
    return ("running" if "generating" in states else "waiting"), ""


def _rodin_files(rec, key):
    files = _rodin("download", key, {"task_uuid": rec["remote_id"]}).get("list") or []
    return [(f.get("name", ""), f.get("url", "")) for f in files]


# --- Hunyuan (Tencent Cloud API 3.0, TC3-HMAC-SHA256) ---

def _tc3_headers(secret_id, secret_key, action, body, host, now):
    """The common headers and the Authorization of one POST (signed headers content-type and host, as the official SDK does)."""
    date, content_type = time.strftime("%Y-%m-%d", time.gmtime(now)), "application/json"
    canonical = f"POST\n/\n\ncontent-type:{content_type}\nhost:{host}\n\ncontent-type;host\n{hashlib.sha256(body).hexdigest()}"
    scope = f"{date}/{HUNYUAN_SERVICE}/tc3_request"
    to_sign = f"TC3-HMAC-SHA256\n{int(now)}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    key = ("TC3" + secret_key).encode()
    for part in (date, HUNYUAN_SERVICE, "tc3_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {"Content-Type": content_type, "Host": host, "X-TC-Action": action, "X-TC-Version": HUNYUAN_VERSION, "X-TC-Region": HUNYUAN_REGION,
            "X-TC-Timestamp": str(int(now)),
            "Authorization": f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, SignedHeaders=content-type;host, Signature={signature}"}


def _hunyuan(action, creds, payload):
    body = json.dumps(payload).encode()
    headers = _tc3_headers(*creds, action, body, urllib.parse.urlsplit(URLS["hunyuan"]).netloc, time.time())
    r = assets._json(URLS["hunyuan"] + "/", "hunyuan", headers, data=body).get("Response") or {}
    if r.get("Error"):
        raise AssetError(f"hunyuan: {r['Error'].get('Code', '')}: {r['Error'].get('Message', '')}")
    return r


def _hunyuan_start(creds, prompt, images, pbr, face_count):
    payload = {"Prompt": prompt} if prompt else {"ImageBase64": base64.b64encode(images[0][2]).decode()}
    if pbr:
        payload["EnablePBR"] = True
    if face_count:
        payload["FaceCount"] = int(face_count)
    r = _hunyuan("SubmitHunyuanTo3DProJob", creds, payload)
    if not r.get("JobId"):
        raise AssetError(f"hunyuan: the answer has no JobId: {json.dumps(r)[:200]}")
    return str(r["JobId"]), {}


def _hunyuan_status(rec, creds):
    r = _hunyuan("QueryHunyuanTo3DProJob", creds, {"JobId": rec["remote_id"]})
    s = str(r.get("Status", "")).upper()
    rec["_files"] = [(f"{f.get('Type', '')}", f.get("Url", "")) for f in r.get("ResultFile3Ds") or []]
    if s == "FAIL":
        return "failed", f"{r.get('ErrorCode', '')} {r.get('ErrorMessage', '')}".strip()
    return {"DONE": "done", "RUN": "running"}.get(s, "waiting"), ""


# --- jobs ---

def _job_id(job):
    j = str(job or "")
    if not re.fullmatch(r"(rodin|hunyuan)-[A-Za-z0-9][A-Za-z0-9._\-]{0,80}", j):
        raise AssetError(f"invalid job id: {j[:60]!r} (the id generate_start returned, such as rodin-<uuid>)")
    return j


def _load(job):
    try:
        return json.loads((JOBS / f"{_job_id(job)}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise AssetError(f"unknown job '{job}': it was not started from this computer (jobs are kept in {JOBS})") from None


def _save(rec):
    JOBS.mkdir(parents=True, exist_ok=True)
    (JOBS / f"{rec['job']}.json").write_text(json.dumps({k: v for k, v in rec.items() if not k.startswith("_")}, ensure_ascii=False), encoding="utf-8")


def start(provider, prompt="", images=None, tier="", pbr=False, face_count=0, accept_cost=False, mesh_mode=""):
    """Submits a job. Without accept_cost nothing is sent: the result describes the job and its cost. Returns {job, sent, text}."""
    provider = _provider(provider)
    creds = _credentials(provider)                                    # a missing key shows up before anything else
    pics = _read_images(provider, images)
    prompt = _validate(provider, prompt, pics, tier, face_count)
    what = f"{provider} {'image to 3D' if pics else 'text to 3D'}: " + (f"prompt {prompt[:80]!r}" if prompt else f"{len(pics)} image{'s' if len(pics) > 1 else ''}")
    price = cost(provider, tier, pbr, face_count)
    if not accept_cost:
        return {"job": "", "sent": False, "text": f"NOT SENT. {what}. It would cost {price}. Call again with accept_cost=True to start it."}
    remote_id, extra = _rodin_start(creds[0], prompt, pics, tier, mesh_mode) if provider == "rodin" else _hunyuan_start(creds, prompt, pics, pbr, face_count)
    rec = {"job": f"{provider}-{remote_id}", "provider": provider, "remote_id": remote_id, "prompt": prompt, "created": int(time.time()), **extra}
    _save(rec)
    return {"job": rec["job"], "sent": True, "text": f"started {rec['job']} ({what}); cost {price}. Generation takes minutes: poll with generate_status."}


def status(job, max_mb=assets.MAX_MB):
    """Polls once; when the job is done the model is downloaded (once). Returns {state: waiting|running|done|failed, text}."""
    rec = _load(job)
    provider = rec["provider"]
    folder, marker = assets.ROOT / "generated" / rec["job"], assets.ROOT / "generated" / rec["job"] / ".done.json"
    if marker.is_file():
        return {"state": "done", "text": f"{rec['job']}: done, the model is at {json.loads(marker.read_text(encoding='utf-8'))['main_file']}. "
                                         f"Bring it in with asset_import(source='{provider}', id='{rec['job']}', target_size=...)."}
    creds = _credentials(provider)
    state, message = _rodin_status(rec, creds[0]) if provider == "rodin" else _hunyuan_status(rec, creds)
    if state != "done":
        return {"state": state, "text": f"{rec['job']}: {state}" + (f": {message}" if message else "") + ("" if state == "failed" else ". Poll again in a while.")}
    files = _rodin_files(rec, creds[0]) if provider == "rodin" else rec["_files"]
    main = _download(provider, rec["job"], files, folder, int(max_mb) << 20)
    marker.write_text(json.dumps({"source": provider, "id": rec["job"], "name": rec["job"], "kind": "model", "license": f"AI-generated ({provider} terms apply)",
                                  "author": "", "attribution": "", "folder": str(folder), "main_file": str(main), "resolution": ""}), encoding="utf-8")
    return {"state": "done", "text": f"{rec['job']}: done, downloaded {main.name}. Bring it in with asset_import(source='{provider}', id='{rec['job']}', target_size=...)."}


def _download(provider, job, files, folder, cap):
    """Picks the glb (or an obj package / fbx) from the result list, downloads it to folder and returns the main file."""
    def kind(name, url):                   # Rodin names the file, Hunyuan names the type (GLB, OBJ) and gives a plain or zip address
        for s in (Path(name).suffix, Path(urllib.parse.urlsplit(url).path).suffix, name):
            if s.lower().lstrip(".") in PICK:
                return s.lower().lstrip(".")
        return ""
    usable = sorted((PICK[k], url, k) for name, url in files if url and (k := kind(name, url)))
    if not usable:
        raise AssetError(f"{provider} {job}: no glb, fbx or obj among the result files: {', '.join(n or '?' for n, _ in files) or 'none'}")
    _, url, k = usable[0]
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    try:
        target = folder / ("model" + (Path(urllib.parse.urlsplit(url).path).suffix.lower() or "." + k))
        assets._download_file(url, provider + "_files", target, cap=cap)
        if target.suffix == ".zip":
            assets._unzip(target, folder)
            target.unlink()
            target = next((p for e in (".glb", ".gltf", ".fbx", ".obj") for p in sorted(folder.rglob("*" + e))), None)
            if target is None:
                raise AssetError(f"{provider} {job}: no glb, fbx or obj in the result package")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return target


def fetch(job, max_mb=assets.MAX_MB):
    """The asset_import view of a finished job: the same dict assets.download returns."""
    rec = _load(job)
    marker = assets.ROOT / "generated" / rec["job"] / ".done.json"
    if not marker.is_file():
        s = status(job, max_mb)
        if s["state"] != "done":
            raise AssetError(s["text"])
    meta = json.loads(marker.read_text(encoding="utf-8"))
    return {**meta, "cached": True, "warning": ""}


def _cli(argv=None):
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="tether.generate", description="Generative 3D: Rodin and Hunyuan3D.")
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("start")
    s.add_argument("provider")
    s.add_argument("prompt", nargs="?", default="")
    s.add_argument("--image", action="append", default=[])
    s.add_argument("--tier", default="")
    s.add_argument("--pbr", action="store_true")
    s.add_argument("--accept-cost", action="store_true")
    t = sub.add_parser("status")
    t.add_argument("job")
    o = ap.parse_args(argv)
    try:
        print((start(o.provider, o.prompt, o.image, o.tier, o.pbr, 0, o.accept_cost) if o.command == "start" else status(o.job))["text"])
    except AssetError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
