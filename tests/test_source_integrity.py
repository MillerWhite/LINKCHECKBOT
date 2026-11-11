import re
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from linkcheck_config import derive_bothost_domain, normalize_https_url, resolve_miniapp_url


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

    def test_fetch_retry_runtime_scenarios(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")
        result = subprocess.run(
            [node, str(ROOT / "tests" / "fetch_retry.test.cjs")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("6 fetch resilience scenarios passed", result.stdout)

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

    def test_telegram_sdk_retries_after_load_errors(self):
        self.assertIn("function retryTelegramWebAppSdk(attempt=1)", self.html)
        self.assertIn("telegram-webapp-sdk-error", self.html)
        self.assertIn("telegram-webapp-sdk-failed", self.html)
        self.assertIn("attempt>2", self.html)

    def test_telethon_startup_never_prompts_for_interactive_input(self):
        self.assertIn("await tg_client.connect()", self.backend)
        self.assertIn("if not await tg_client.is_user_authorized():", self.backend)
        self.assertIn("SESSION_STRING = os.getenv", self.backend)
        self.assertIn("StringSession(SESSION_STRING) if SESSION_STRING else TG_SESSION", self.backend)
        self.assertNotIn("await tg_client.start()", self.backend)

    def test_platform_domain_replaces_stale_miniapp_url(self):
        resolved = resolve_miniapp_url(
            "https://old-host.example/",
            "bot-1793993417-4443-nifortt.bothost.tech",
        )
        self.assertEqual(
            resolved,
            "https://bot-1793993417-4443-nifortt.bothost.tech",
        )

    def test_bothost_domain_is_derived_from_bot_id(self):
        self.assertEqual(
            derive_bothost_domain("bot_1793993417_4443_nifortt"),
            "https://bot-1793993417-4443-nifortt.bothost.tech",
        )
        self.assertEqual(derive_bothost_domain(""), "")
        self.assertEqual(derive_bothost_domain("some_other_id"), "")

    def test_configured_path_is_preserved_when_domain_matches(self):
        resolved = resolve_miniapp_url(
            "http://my-app.bothost.tech/miniapp/",
            "my-app.bothost.tech",
        )
        self.assertEqual(resolved, "https://my-app.bothost.tech/miniapp")

    def test_configured_url_is_fallback_without_platform_domain(self):
        self.assertEqual(
            resolve_miniapp_url("http://example.com/app", ""),
            "https://example.com/app",
        )
        self.assertEqual(normalize_https_url(""), "")

    def test_public_health_endpoint_is_registered(self):
        self.assertIn('app.router.add_get("/health", handle_health)', self.backend)
        self.assertIn('"service": "LINKCHECK"', self.backend)
        self.assertIn('"bind_host": "0.0.0.0"', self.backend)
        self.assertIn('"platform_domain_host": _platform_domain_host or None', self.backend)
        self.assertIn('"miniapp_url_source": MINIAPP_URL_SOURCE', self.backend)
        self.assertIn("LINKCHECK hosting config: DOMAIN_present=%s", self.backend)
        self.assertIn("async def log_platform_domain_dns_status()", self.backend)
        self.assertIn("await log_platform_domain_dns_status()", self.backend)

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
        for key in ("BOT_TOKEN=", "TG_API_ID=", "TG_API_HASH="):
            self.assertIn(key, example)
        self.assertNotRegex(example, r"\d{8,10}:[A-Za-z0-9_-]{25,}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
