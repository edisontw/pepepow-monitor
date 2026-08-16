from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "monitor" / "pepepow_org_probe.py"
spec = importlib.util.spec_from_file_location("pepepow_org_probe", MODULE_PATH)
probe = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["pepepow_org_probe"] = probe
spec.loader.exec_module(probe)


class WebsiteProbeTests(unittest.TestCase):
    def test_cloudflare_challenge_is_recognized(self):
        self.assertTrue(probe.is_cloudflare(403, "<title>Just a moment...</title> Cloudflare"))
        self.assertFalse(probe.is_cloudflare(404, "not found"))

    def test_collects_img_lazy_and_social_images(self):
        html = """
        <html><head><meta property="og:image" content="/og.jpg"></head>
        <body>
          <img src="/a.jpg">
          <img data-src="https://pepepow.org/b.webp">
          <img srcset="/c-320.jpg 320w, /c-640.jpg 640w">
        </body></html>
        """
        urls = probe.image_urls(html, "https://pepepow.org/")
        self.assertIn("https://pepepow.org/og.jpg", urls)
        self.assertIn("https://pepepow.org/a.jpg", urls)
        self.assertIn("https://pepepow.org/b.webp", urls)
        self.assertIn("https://pepepow.org/c-320.jpg", urls)

    def test_data_images_are_ignored(self):
        html = '<img src="data:image/png;base64,AAAA"><img src="/real.png">'
        urls = probe.image_urls(html, "https://pepepow.org/")
        self.assertEqual(urls, ["https://pepepow.org/real.png"])


if __name__ == "__main__":
    unittest.main()
