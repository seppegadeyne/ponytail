"""Fork contracts (seppegadeyne/ponytail): auto activation, per-session modes, settings, addendum.

Run: python3 tests/test_hermes_fork.py
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git_repo():
    path = tempfile.mkdtemp()
    os.mkdir(os.path.join(path, ".git"))
    return path


def load(settings=None, env=None):
    for key in ("PONYTAIL_DEFAULT_MODE", "PONYTAIL_ACTIVATION", "PONYTAIL_SKIP_SOURCES", "HERMES_SESSION_SOURCE"):
        os.environ.pop(key, None)
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
    os.environ["TERMINAL_CWD"] = tempfile.mkdtemp()  # not a git repo
    os.environ.update(env or {})
    spec = importlib.util.spec_from_file_location("ponytail_fork_under_test", ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class Ctx:
        def __init__(self):
            self.hooks, self.commands = {}, {}

        def register_skill(self, name, path):
            pass

        def register_hook(self, name, fn):
            self.hooks[name] = fn

        def register_command(self, name, fn, description="", args_hint=""):
            self.commands[name] = fn

        def get_config(self, key, default=None):
            return (settings or {}).get(key, default)

    ctx = Ctx()
    mod.register(ctx)
    return mod, ctx


def persisted(ctx_result, text="task"):
    return {"role": "user", "content": text, "api_content": f"{text}\n\n{ctx_result['context']}"}


class AutoActivation(unittest.TestCase):
    def test_household_chat_gets_nothing(self):
        mod, ctx = load()
        out = ctx.hooks["pre_llm_call"](session_id="s", user_message="Wat eten we vanavond? Plan het weekend.",
                                        conversation_history=[], platform="telegram")
        self.assertIsNone(out)

    def test_dev_message_activates_full_with_addendum(self):
        mod, ctx = load()
        out = ctx.hooks["pre_llm_call"](session_id="s", user_message="Fix de bug in ~/Projects/foo/app.py en draai pytest",
                                        conversation_history=[], platform="cli")
        self.assertIn("PONYTAIL MODE ACTIVE — level: full", out["context"])
        self.assertIn("Project rules win", out["context"])

    def test_git_repo_cwd_activates(self):
        repo = tempfile.mkdtemp()
        (Path(repo) / ".git").mkdir()
        mod, ctx = load(env={"TERMINAL_CWD": os.path.join(repo)})
        out = ctx.hooks["pre_llm_call"](session_id="s", user_message="hoi", conversation_history=[], platform="cli")
        self.assertIsNotNone(out)

    def test_cron_is_skipped_by_default(self):
        mod, ctx = load()
        out = ctx.hooks["pre_llm_call"](session_id="s", user_message="review the PR #12 diff",
                                        conversation_history=[], platform="cron")
        self.assertIsNone(out)

    def test_subagent_goal_with_code_intent_activates(self):
        mod, ctx = load()
        out = ctx.hooks["pre_llm_call"](session_id="child", user_message="Scan the codebase in ~/Projects/x for dead code",
                                        conversation_history=[], platform="subagent")
        self.assertIsNotNone(out)

    def test_active_session_stays_active_and_injects_once(self):
        mod, ctx = load()
        first = ctx.hooks["pre_llm_call"](session_id="s", user_message="refactor utils.ts", conversation_history=[])
        history = [persisted(first)]
        again = ctx.hooks["pre_llm_call"](session_id="s", user_message="en nu iets over het weer",
                                          conversation_history=history)
        self.assertIsNone(again)

    def test_tool_call_on_code_activates_mid_session_once(self):
        mod, ctx = load()
        ctx.hooks["pre_llm_call"](session_id="s", user_message="kijk eens", conversation_history=[], platform="cli")
        hook = ctx.hooks["transform_tool_result"]
        repo = git_repo()
        self.assertIsNone(hook(tool_name="read_file", args={"path": f"{repo}/app.py"}, result="{}", session_id="s", turn_id="t"))
        self.assertIsNone(hook(tool_name="patch", args={"path": f"{repo}/notes.md"}, result="{}", session_id="s", turn_id="t"))
        out = hook(tool_name="patch", args={"path": f"{repo}/src/app.py"}, result='{"ok":1}', session_id="s", turn_id="t")
        self.assertTrue(out.startswith('{"ok":1}'))
        self.assertIn("PONYTAIL MODE ACTIVE — level: full", out)
        self.assertIsNone(hook(tool_name="patch", args={"path": f"{repo}/b.py"}, result="{}", session_id="s", turn_id="t"))
        # Next turn sees the tool-result marker and does not re-inject.
        nxt = ctx.hooks["pre_llm_call"](session_id="s", user_message="verder",
                                        conversation_history=[{"role": "tool", "content": out}])
        self.assertIsNone(nxt)

    def test_tool_hook_ignores_nested_calls_and_terminal_noise(self):
        mod, ctx = load()
        ctx.hooks["pre_llm_call"](session_id="s", user_message="hoi", conversation_history=[])
        hook = ctx.hooks["transform_tool_result"]
        self.assertIsNone(hook(tool_name="patch", args={"path": "a.py"}, result="{}", session_id="s", turn_id=""))
        self.assertIsNone(hook(tool_name="terminal", args={"command": "ls -la"}, result="{}", session_id="s", turn_id="t"))
        self.assertIsNone(hook(tool_name="terminal", args={"command": "git diff HEAD && git status"}, result="{}", session_id="s", turn_id="t"))
        self.assertIsNotNone(hook(tool_name="terminal", args={"command": "npm run test"}, result="{}", session_id="s", turn_id="t"))

    def test_tool_hook_ignores_scripts_outside_a_git_repo(self):
        # Replay 2026-10-29: scratch helper scripts in ops/CV chats were the main false hits.
        mod, ctx = load()
        ctx.hooks["pre_llm_call"](session_id="s", user_message="hoi", conversation_history=[])
        hook = ctx.hooks["transform_tool_result"]
        scratch = tempfile.mkdtemp()
        self.assertIsNone(hook(tool_name="write_file", args={"path": f"{scratch}/cdp.mjs"}, result="{}", session_id="s", turn_id="t"))
        self.assertIsNone(hook(tool_name="patch", args={"path": "a.py"}, result="{}", session_id="s", turn_id="t"))  # cwd not a repo
        self.assertIsNotNone(hook(tool_name="write_file", args={"path": f"{git_repo()}/x.ts"}, result="{}", session_id="s", turn_id="t"))

    def test_tool_hook_respects_skipped_source(self):
        mod, ctx = load()
        ctx.hooks["pre_llm_call"](session_id="c", user_message="hoi", conversation_history=[], platform="cron")
        out = ctx.hooks["transform_tool_result"](tool_name="patch", args={"path": "a.py"}, result="{}", session_id="c", turn_id="t")
        self.assertIsNone(out)


class SessionModes(unittest.TestCase):
    def test_mode_command_is_per_session(self):
        mod, ctx = load()
        self.assertIn("ultra", ctx.commands["ponytail"]("ultra", session_id="a"))
        a = ctx.hooks["pre_llm_call"](session_id="a", user_message="hoi", conversation_history=[])
        b = ctx.hooks["pre_llm_call"](session_id="b", user_message="hoi", conversation_history=[])
        self.assertIn("level: ultra", a["context"])
        self.assertIsNone(b)

    def test_off_reaches_model_once(self):
        mod, ctx = load()
        first = ctx.hooks["pre_llm_call"](session_id="a", user_message="fix app.py", conversation_history=[])
        ctx.commands["ponytail"]("off", session_id="a")
        off = ctx.hooks["pre_llm_call"](session_id="a", user_message="x", conversation_history=[persisted(first)])
        self.assertEqual(off, {"context": "PONYTAIL MODE OFF"})
        history = [persisted(first), persisted(off, "y")]
        self.assertIsNone(ctx.hooks["pre_llm_call"](session_id="a", user_message="fix b.py", conversation_history=history))

    def test_bare_command_reports_or_switches_on(self):
        mod, ctx = load()
        self.assertIn("set to full", ctx.commands["ponytail"]("", session_id="a"))
        self.assertIn("Ponytail mode: full", ctx.commands["ponytail"]("", session_id="a"))


class Settings(unittest.TestCase):
    def test_config_settings_beat_env(self):
        mod, ctx = load(settings={"default_mode": "lite", "activation": "always", "skip_sources": []},
                        env={"PONYTAIL_DEFAULT_MODE": "ultra"})
        out = ctx.hooks["pre_llm_call"](session_id="s", user_message="hoi", conversation_history=[], platform="cron")
        self.assertIn("level: lite", out["context"])

    def test_manual_activation_never_auto_injects(self):
        mod, ctx = load(settings={"activation": "manual"})
        self.assertIsNone(ctx.hooks["pre_llm_call"](session_id="s", user_message="fix app.py", conversation_history=[]))
        ctx.commands["ponytail"]("full", session_id="s")
        self.assertIsNotNone(ctx.hooks["pre_llm_call"](session_id="s", user_message="x", conversation_history=[]))

    def test_addendum_can_be_disabled(self):
        mod, ctx = load(settings={"addendum": False})
        out = ctx.hooks["pre_llm_call"](session_id="s", user_message="fix app.py", conversation_history=[])
        self.assertNotIn("Project rules win", out["context"])


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False, verbosity=1).result.wasSuccessful() else 1)
