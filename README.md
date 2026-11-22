![blender-tether: headless, live window, verified](docs/cover.png)

# blender-tether

Let an AI agent drive Blender, headless or live, and check its own work.

Tether is an [MCP](https://modelcontextprotocol.io) server plus a small agent that runs inside Blender. The agent runs Python in Blender, reads a text report of the
scene, looks at the result from several angles in one image, and fixes what it finds. When no Blender is running it starts its own headless one, so there is no window
to click and no add-on to install for the common case. It can also attach to a Blender window you opened.

![demo](docs/demo.gif)

## What it does

- **Starts its own Blender.** `open` launches a headless daemon (factory scene, no user add-ons) in about 2 seconds and closes it after 30 idle minutes or when the agent session ends.
- **Runs Python with a persistent namespace.** Define a function once and call it by name. `bpy`, `bmesh`, `mathutils`, `numpy` and friends are already there. Stale `bpy` references are dropped when a file loads or undo runs, instead of crashing Blender.
- **Lets the agent verify its work.** `scene` returns a text report (sizes, ground contact, topology, weights, modifiers, animation range...). `view` renders the scene offscreen, in the background too, as one PNG with several angles, shading modes (solid, wireframe, xray, texture, material) or an animation strip.
- **Finds and imports assets.** `asset_search` and `asset_import` work with Poly Haven, ambientCG, Sketchfab and Poly Pizza. CC0 only by default. Downloads are HTTPS-only, from allow-listed hosts, capped in size, hash-checked and cached.
- **Generates 3D models** from a prompt or pictures with Hyper3D Rodin or Tencent Hunyuan3D, with your own API keys. Nothing is sent, and no credits are spent, until the agent passes `accept_cost=True`.
- **Fails loudly.** Helpers such as `tether.select` raise with the reason where raw Blender calls silently do nothing (a locked object, a hidden collection, an export that says `FINISHED` and writes no file).
- **Optional GTA V / FiveM checks** through the Sollumz add-on (see below).

## Install

Requirements: Python 3.10+, Blender, and the `mcp` and `Pillow` packages. Developed and tested on Windows 11 with Blender 5.2.4 LTS and Python 3.13. macOS and Linux are untested.
Tether runs on the MCP Python SDK 1.x (1.28 and later) and on 2.x with the same code: `tether/mcp_server.py` imports `MCPServer` (2.x) or `FastMCP` (1.x), whichever is installed. The install allows `mcp>=1.28,<3`; the full test suite passes with 1.30.0 and 2.3.0.

```powershell
pip install git+https://github.com/B7Kompirine/blender-tether
```

Register it with your MCP client. For Claude Code:

```powershell
claude mcp add tether -- python -m tether.mcp_server
```

Any client that reads a JSON config:

```json
{
  "mcpServers": {
    "tether": {
      "command": "python",
      "args": ["-m", "tether.mcp_server"],
      "env": { "TETHER_BLENDER": "C:\\Program Files\\Blender Foundation\\Blender 5.2\\blender.exe" }
    }
  }
}
```

`TETHER_BLENDER` is optional: Tether looks on `PATH` and in the usual install folders first. From a source checkout you can also point the client at `python tether/mcp_server.py`.

## Tools

| Tool | What it does |
|---|---|
| `status` | Lists running apps (selector, mode, version, busy or idle) and what can be opened. Call it first. |
| `open` | Starts Blender and waits for its agent. `background=true` is the headless daemon, otherwise a window with your settings. |
| `close` | Closes a background agent. |
| `run` | Runs Python (or a script file with `args`, `cwd`, `env`, `blend`, `clean`, `grep`). Returns output, the value of the last expression and a line-numbered trace on errors. |
| `api_search` | Searches the running Blender's own API (`bpy.ops`, `bpy.types`, add-ons included). An exact name dumps its parameters. |
| `scene` | Read-only scene report. Fields: location, size, ground, rotation, scale, parent, children, hidden, details, materials, modifiers, animation, topology, weights, gta, settings. |
| `view` | One PNG of the scene: `angles`, `shading`, `target`, `distance`, `frames`, `look`, `image`. |
| `asset_search` | Searches models, PBR textures and HDRIs. |
| `asset_import` | Downloads an asset and brings it into Blender with a target size, a license note and rollback on failure. Also imports a finished generated model (`source` `rodin` or `hunyuan`, `id` = the job). |
| `generate_start` | Starts a text-to-3D or image-to-3D job at Rodin or Hunyuan. Without `accept_cost=True` it only shows what it would send and what it costs. |
| `generate_status` | Polls a job once and, when it is done, downloads the model. |

A typical loop for an agent: `status` → `open` → `api_search` → `run` → `scene` + `view` → fix → `scene` + `view`.

## Modes

- **Headless daemon:** `open app=blender background=true`. Starts clean (`--factory-startup`), so a user add-on cannot interfere. Use `blend=` and `clean=` on `run` to open a file or return to the factory scene.
- **Windowed Blender:** `open app=blender` starts Blender with your own settings and the agent attached. To attach to a Blender you started yourself, link `blender_addon/tether_blender` into Blender's `scripts/addons` folder and enable **Tether** in Preferences.
- **Other apps:** the wire protocol is JSON lines over loopback TCP and does not depend on Python. `tether/agent/example_app.py` embeds the agent in a Python program and `tether/agent/example_node.js` does the same in about 70 lines of Node. The protocol is described at the top of `tether/agent/__init__.py`.

## Configuration

| Variable | Meaning |
|---|---|
| `TETHER_BLENDER` | Full path of the Blender executable. |
| `TETHER_ADDONS` | Comma-separated add-ons the headless daemon enables, for example `Sollumz`. Default: none. |
| `TETHER_SCRIPT_DIRS` | Extra folders (separated by `;` on Windows, `:` elsewhere) in which a relative `file` of `run` is looked up, after `cwd`. |
| `TETHER_HOME` | State folder. Default `~/.tether`. |
| `TETHER_STATS=0` | Turns off the call log. It stores counts and timings only, never code or output. |
| `SKETCHFAB_TOKEN`, `POLYPIZZA_KEY` | Keys for the two providers that need them. They can also be put in `~/.tether/assets.json` as `sketchfab_token` and `polypizza_key`. Never paste a key into a chat. |
| `RODIN_API_KEY`, `TENCENT_SECRET_ID`, `TENCENT_SECRET_KEY` | Keys for generative 3D (Hyper3D Rodin; Tencent Cloud for Hunyuan3D). Also readable from `~/.tether/assets.json` as `rodin_key`, `tencent_secret_id`, `tencent_secret_key`. |

## Measured numbers

`python tools/benchmark.py` drives the MCP server the way an agent does and writes real tool output and timings. Medians of three runs on one machine (Ryzen 7 9800X3D, RTX 5070, Windows 11, Blender 5.2.4 LTS) varied like this:

| | Range of medians |
|---|---|
| Cold `blender -b --factory-startup` with an empty script | 0.59 to 0.83 s |
| One `run` round trip to the running daemon | 22 to 30 ms |
| 4-angle 1024 px image from the daemon, warm | 76 to 95 ms |
| Daemon start, once per session | 2 to 3 s |

These are tool-side times. Inside an agent every call also pays the agent runtime's own overhead, which in my sessions was tens to hundreds of milliseconds, so the saving shows up in jobs that would otherwise start Blender, not in a single call. The runs differ from each other by more than the noise inside one run, so treat single numbers with care.

## Compared with blender-mcp

![comparison](docs/compare.png)

[blender-mcp](https://pypi.org/project/blender-mcp/) (the "MCP for Blender" add-on, version 1.8 here) is the established tool for the same job. `python tools/compare.py` drives both through the same MCP stdio harness with the same code. Three runs on the machine above (after the speed fixes), the range of per-run medians, both tools attached to a windowed Blender unless stated:

| | Tether | blender-mcp |
|---|---|---|
| Needs a Blender window | No: headless daemon, ready in 2.5 to 2.8 s. A window is optional (4.2 to 4.9 s) | Yes: a Blender with the add-on running (3.7 to 4.1 s) |
| `print(1 + 1)` round trip | 12 to 23 ms | 54 ms |
| Scene read (text report / JSON) | 21 to 28 ms | 53 to 54 ms |
| Build a 40-object scene with one call | 74 to 306 ms | 74 to 141 ms |
| Viewport image, 1024 px | 66 to 83 ms | 86 to 123 ms |
| Several angles in one image, headless | 80 to 90 ms | not available |
| Error answer | one line plus a line-numbered trace | JSON wrapped in a "communication error" message |
| Poly Haven, Sketchfab, Poly Pizza | yes, CC0 by default (also ambientCG) | yes |
| Generative 3D (Hyper3D, Hunyuan3D) | yes, with your own keys (not yet run against the live services, see below) | yes |
| Sollumz / GTA V export checks | yes | no |

What this does and does not show:

- Per call, Tether is faster on code and scene reads in a windowed Blender: the other add-on hands commands to Blender's main thread on a 50 ms timer, which sets a floor of about 50 ms per call. Viewport images are about the same (Tether slightly ahead in these runs). Building the scene is a tie within the noise.
- The larger difference is structural. Tether can start and use its own headless Blender, and its `scene` report and multi-angle `view` let an agent check its own work. blender-mcp has a much larger user base, which Tether does not.
- The two tools return different content (a text report is not a JSON dump, a window screenshot is not a four-angle image), so read the times together with what comes back.
- Asset downloads are network bound: about 2.5 s for Tether and 2.1 s for blender-mcp for the same Poly Haven texture, one run each. The blender-mcp asset search failed on this machine with an argument mismatch between the server and the installed add-on, so it is not in the table.
- One machine, one Blender version, a game running in the background, so absolute numbers will differ on yours. Tether's headless `print(1 + 1)` varied from 5 to 31 ms between runs.

## Security

Tether listens on `127.0.0.1` only, on a port the OS chooses. Each agent has a random token that is stored in `~/.tether/apps/<app>-<pid>.json`. Anyone who can read that file can run code in that Blender, exactly as the user who started it can, so treat it like any local code execution. The client verifies a registry entry against the process id and creation time before connecting, and removes stale ones. Asset downloads accept HTTPS from allow-listed hosts only, including after redirects, and are capped in size and time.

## Generative 3D

```text
generate_start provider=rodin prompt="a wooden barrel"           -> NOT SENT, shows the cost
generate_start provider=rodin prompt="a wooden barrel" accept_cost=true  -> job rodin-<id>
generate_status job=rodin-<id>                                    -> waiting / running / done (downloads the glb)
asset_import app=blender source=rodin id=rodin-<id> target_size=1.0
```

- **Rodin** (`rodin`): text or up to 5 pictures, `tier` default `Gen-2.5-Low` (the higher tiers need a full API plan). About 0.5 credits per job.
- **Hunyuan3D Pro** (`hunyuan`): a prompt or one picture, `pbr` and `face_count` optional. 20 credits per job plus extras (about 2 to 2.4 CNY), a free package of 100 credits can be claimed once in the Tencent console.
- The result is a glb, downloaded once into `~/.tether/assets/generated/<job>`. The license is the provider's terms, not CC0. Result files come from whichever public HTTPS host the provider's API names, so redirects, private addresses and size are checked.
- Jobs live in `~/.tether/jobs`, so a job can be polled from a later session. Hunyuan's result links are valid for 24 hours.
- Hunyuan Rapid, multi-view input and the other Tencent 3D actions are not supported.

## GTA V / FiveM (optional)

If you work with GTA V assets through the [Sollumz](https://github.com/Sollumz/Sollumz) add-on, set `TETHER_ADDONS=Sollumz`. The headless daemon then enables it, `scene fields=["gta"]` flags the usual export problems (missing `UVMap 0` or `Color 1`, empty `sz_lods`, unapplied transforms, `.001` suffixes, missing textures) and `tether.export_sollumz(folder, [names])` runs a Sollumz export that checks the files it wrote. None of this is active unless you ask for it.

## Tests

```powershell
python tests/run_tests.py                     # engine, client, scripts, asset sources against a fake server (no Blender, no network)
python tests/run_tests.py --mcp               # plus the MCP server through a stdio client
python tests/run_tests.py --mcp --blender     # plus a real headless Blender (412 checks with Sollumz installed, about two minutes)
python tests/run_tests.py --blender --gui     # also a windowed Blender: a window opens and takes focus
```

The Blender groups analyse image pixels, so they need numpy: `pip install ".[test]"` from a checkout. The Sollumz checks run only when that add-on is available. The MCP groups use whichever SDK is installed: add `"mcp<2"` or `"mcp>=2"` to that command to pick the major.

## Limits

- Developed on Windows. The process launch uses WMI to start Blender outside a packaged app's file virtualization and with the console hidden. The non-Windows launch path is a plain detached `Popen` and has not been run.
- Tested with Blender 5.2.4 LTS only.
- A script that registers add-ons, classes or handlers needs a fresh process (`close`, then `open`).
- While you are inside a modal operator in a windowed Blender, jobs wait until it ends.
- Generative 3D has been tested against a fake server that follows the providers' documented request and response shapes, and the Hunyuan request signature matches the official Tencent SDK byte for byte. It has not been run against the live Rodin or Hunyuan services yet: expect to fix small things on the first real job.

## License

MIT, see [LICENSE](LICENSE). Blender is a registered trademark of the Blender Foundation. Tether is an independent project and is not affiliated with or endorsed by the Blender Foundation.
