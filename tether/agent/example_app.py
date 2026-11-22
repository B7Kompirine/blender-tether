"""The smallest example of embedding the Tether agent in a Python program; also the fake app of the tests (a closable daemon).

Usage: python example_app.py [app_name] [mode]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tether.agent import Agent  # noqa: E402


def setup(ns):
    ns["hello"] = lambda who="world": f"hello {who}"
    ns["emit_warning"] = lambda text: agent.warn(text)


def search(query):
    return "hello(who): greets" if "hello" in query else "no match"


def error_hint(e):
    return "the divisor must not be zero" if isinstance(e, ZeroDivisionError) else None


def write(text):
    print("written:", text)
    return {"length": len(text)}


FUNCTIONS = {"add": lambda a, b: a + b, "write": write, "divide": lambda a, b: a / b}

agent = Agent(sys.argv[1] if len(sys.argv) > 1 else "example", "1.0", mode=sys.argv[2] if len(sys.argv) > 2 else "background",
              setup=setup, search=search, error_hint=error_hint, closable=True, hint="python; hello() is in the namespace",
              idle_s=float(os.environ.get("EXAMPLE_IDLE_S", 0)), owner_pid=int(os.environ.get("EXAMPLE_OWNER_PID", 0)), functions=FUNCTIONS)
agent.start()
agent.loop()
