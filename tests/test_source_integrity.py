import re
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InlineScriptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "script":
            return
        attrs = dict(attrs)
        self.current = [] if "src" not in attrs else None

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None


class SourceIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.html = (ROOT / "miniapp.html").read_text(encoding="utf-8")
        cls.ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        parser = InlineScriptParser()
        parser.feed(cls.html)
        cls.inline_scripts = parser.scripts

    def test_backend_compiles(self):
        compile(self.backend, "main.py", "exec")

    def test_frontend_has_inline_application_script(self):
        self.assertTrue(self.inline_scripts)
        self.assertTrue(any("initUser" in script for script in self.inline_scripts))

    def test_frontend_javascript_syntax(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")
        self.assertTrue(self.inline_scripts)
        with tempfile.TemporaryDirectory() as directory:
            for index, script in enumerate(self.inline_scripts):
                path = Path(directory) / f"inline-{index}.js"
                path.write_text(script, encoding="utf-8")
                result = subprocess.run(
                    [node, "--check", str(path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    msg=f"JavaScript syntax error in {path.name}: {result.stderr}",
                )

    def test_every_literal_frontend_api_route_exists(self):
        routes = {
            match.group(1)
            for match in re.finditer(
                r"""app\.router\.add_(?:get|post)\(\s*["']([^"']+)["']""",
                self.backend,
            )
        }
        routes = {route.split("?", 1)[0] for route in routes}
        fetches = {
            match.group(1).split("?", 1)[0]
            for match in re.finditer(
                r"""fetch\(\s*["'](/api/[^"']+)["']""",
                self.html,
            )
        }
        self.assertTrue(fetches)
        missing = sorted(fetches - routes)
        self.assertEqual(missing, [], msg=f"Frontend API routes missing in backend: {missing}")

    def test_mobile_api_reads_have_timeout_and_bounded_retries(self):
        self.assertIn("LINKCHECK_API_READ_TIMEOUT_MS=15000", self.html)
        self.assertIn("new AbortController()", self.html)
        self.assertIn("for(let attempt=0;attempt<3;attempt++)", self.html)
        self.assertIn("requestMethod==='GET'", self.html)
        self.assertIn("if(!retryable)return linkcheckNativeFetch(input,init)", self.html)

    def test_telegram_sdk_wait_is_not_too_short(self):
        self.assertIn("function waitForTelegramWebApp(timeoutMs=10000)", self.html)

    def test_public_stats_do_not_wait_for_telegram_auth(self):
        self.assertIn("loadStats();\ninitUser().then(()=>loadRecent());", self.html)

    def test_reconnect_refreshes_active_screen(self):
        self.assertIn("window.addEventListener('online'", self.html)
        self.assertIn("refreshCurrentScreen();", self.html)

    def test_sensitive_runtime_files_are_ignored(self):
        for pattern in (".env", ".env.*", "*.session", "*.db", "__pycache__/"):
            self.assertIn(pattern, self.ignore)

    def test_no_credentials_are_embedded_in_example(self):
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        for key in ("BOT_TOKEN=", "TG_API_HASH=", "TELEGRAM_API_ID="):
            self.assertIn(key, example)
        self.assertNotRegex(example, r"\d{8,10}:[A-Za-z0-9_-]{25,}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
