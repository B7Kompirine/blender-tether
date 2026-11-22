"""Test runner. The core tests use a fake app and fake asset servers (no network, no Blender); the other groups are opt-in.

Usage: python tests/run_tests.py [--mcp] [--blender] [--gui]
"""
import argparse
import shutil
import sys

import harness
import test_assets
import test_core


def main():
    ap = argparse.ArgumentParser(description="Tether tests (fake app).")
    ap.add_argument("--mcp", action="store_true", help="also test the MCP server through a stdio client")
    ap.add_argument("--blender", action="store_true", help="also test a headless Blender daemon (~1 minute)")
    ap.add_argument("--gui", action="store_true", help="also test a windowed Blender (needs --blender): a window opens and closes and steals focus (do not run while a game or full screen is open)")
    a = ap.parse_args()
    try:
        test_core.run_all()
        test_assets.run_all()
        if a.mcp:
            import test_mcp
            test_mcp.run_generic()
        if a.blender:
            import test_blender
            test_blender.run_all(gui=a.gui)
            import test_mcp
            test_mcp.run_blender()
    finally:
        shutil.rmtree(harness.TMP, ignore_errors=True)
    print(f"\n{sum(harness.RESULTS)}/{len(harness.RESULTS)} passed")
    return 0 if all(harness.RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
