"""Unit tests that need neither Hermes nor Rex: run with `python3 -m unittest discover tests`."""
import base64
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("rex_plugin", os.path.join(ROOT, "__init__.py"))
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class FakeCtx:
    def __init__(self):
        self.hooks, self.sections, self.skills, self.commands = [], [], [], []

    def get_config(self, key, default=None):
        return {"app_label": "test-app"}.get(key, default)

    def register_hook(self, name, cb):
        self.hooks.append((name, cb))

    def register_system_prompt_section(self, id, content, **kw):
        self.sections.append((id, content, kw))

    def register_skill(self, name, path, description=""):
        assert os.path.exists(path)
        self.skills.append((name, path))

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.commands.append(name)


class Osc7501(unittest.TestCase):
    def test_minimal(self):
        self.assertEqual(plugin.osc7501("working", app="hermes"), b"\x1b]7501;state=working:app=hermes\x1b\\")

    def test_msg_is_base64_single_line(self):
        data = plugin.osc7501("done", app="hermes", msg="line one\nline two", title="Hermes")
        body = data[len(b"\x1b]7501;"):-2].decode()
        pairs = dict(p.split("=", 1) for p in body.split(":"))
        self.assertEqual(base64.b64decode(pairs["msg"]).decode(), "line one")
        self.assertEqual(base64.b64decode(pairs["title"]).decode(), "Hermes")
        self.assertNotIn("\n", body)

    def test_kind_and_progress(self):
        data = plugin.osc7501("blocked", app="h", kind="permission", progress=250).decode()
        self.assertIn(":kind=permission", data)
        self.assertIn(":progress=100", data)

    def test_long_msg_is_clipped_and_stays_under_cap(self):
        data = plugin.osc7501("working", app="h", msg="x" * 5000)
        self.assertLess(len(data), 4000)
        body = data[len(b"\x1b]7501;"):-2].decode()
        msg = base64.b64decode(dict(p.split("=", 1) for p in body.split(":"))["msg"]).decode()
        self.assertEqual(len(msg), plugin._MSG_LIMIT)
        self.assertTrue(msg.endswith("…"))


class Helpers(unittest.TestCase):
    def test_one_line_strips_controls_and_truncates(self):
        self.assertEqual(plugin.one_line("  a\tb\x07c\nnext  ", limit=10), "abc")
        self.assertEqual(len(plugin.one_line("y" * 50, limit=10)), 10)

    def test_breadcrumb_tty(self):
        self.assertEqual(plugin.breadcrumb_tty("tty-dev-ttys004"), "/dev/ttys004")

    def test_parse_rex_env_handles_spaces_in_bin_dir(self):
        line = ('/usr/bin/python3 hermes PATH=/usr/bin REX_SESSION=session:abc REX_BIN_DIR=/Users/me/Library/'
                'Application Support/Rex Server.app/Contents/MacOS REX_BLOCK=block:def TERM_PROGRAM=rex')
        env = plugin.parse_rex_env(line)
        self.assertEqual(env["REX_SESSION"], "session:abc")
        self.assertEqual(env["REX_BLOCK"], "block:def")
        self.assertEqual(env["REX_BIN_DIR"], "/Users/me/Library/Application Support/Rex Server.app/Contents/MacOS")
        self.assertEqual(plugin.parse_rex_env("no rex here"), {})

    def test_active_session_file(self):
        self.assertEqual(plugin.parse_active_session_file('{"session_id":"local-abc"}'), "local-abc")
        self.assertIsNone(plugin.parse_active_session_file('{"session_id":""}'))
        self.assertIsNone(plugin.parse_active_session_file("not json"))


class Registration(unittest.TestCase):
    def test_register_wires_everything(self):
        ctx = FakeCtx()
        plugin.register(ctx)
        names = [n for n, _ in ctx.hooks]
        for required in ("pre_llm_call", "pre_tool_call", "pre_approval_request", "on_human_input_request",
                         "post_llm_call", "on_session_end"):
            self.assertIn(required, names)
        self.assertEqual([s[0] for s in ctx.sections], ["rex"])
        self.assertEqual([s[0] for s in ctx.skills], ["rex"])
        self.assertEqual(ctx.commands, ["rex"])
        self.assertEqual(plugin._settings["app_label"], "test-app")

    def test_context_is_empty_outside_rex(self):
        saved = dict(os.environ)
        for name in ("REX_BLOCK", "REX_SESSION", "REX_SERVER", "REX_BIN_DIR"):
            os.environ.pop(name, None)
        try:
            self.assertEqual(plugin.rex_context({}), "")
        finally:
            os.environ.clear()
            os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
