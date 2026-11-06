"""Read-only guards for approved Korean labels and removal of empty ad slots."""
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


class UxAdsLabelsTests(unittest.TestCase):
    def test_spa_has_no_empty_ads_and_keeps_editorial_and_newsletter(self):
        app = read("web/app.js")
        for removed in ("sponsor", "광고 자리", "유료 소개"):
            self.assertNotIn(removed, app.lower())
        self.assertNotIn(".sponsor", read("web/style.css"))
        for retained in (
            "const SHOW_NEWSLETTER=false;",
            "const news=",
            "const SHOW_VERDICTS=false;",
            "[빈 자리] 준태님 한 줄 평 (작성 전)",
            "준태님 한 줄 평 자리(작성 예정)",
            '<a class="btn p" href="#lineage">셰프 DNA 계보도 보기</a>',
        ):
            self.assertIn(retained, app)

    def test_spa_headings(self):
        app = read("web/app.js")
        self.assertIn("<h1>식당 목록</h1>", app)
        self.assertIn('<h2>셰프 DNA 계보도 <span class="mono">LINEAGE MAP</span></h2>', app)
        self.assertIn('<h2 style="margin-top:0">셰프 DNA 계보</h2>', app)
        self.assertIn("'계보 출처'", app)
        self.assertNotIn("디렉토리", app)

    def test_korean_navigation_in_all_pages(self):
        pages = sorted((ROOT / "web").rglob("*.html"))
        checked = []
        for path in pages:
            html = path.read_text(encoding="utf-8")
            if '<html lang="ko">' not in html:
                continue
            with self.subTest(page=path.relative_to(ROOT)):
                nav = re.search(r"<nav\b[^>]*>(.*?)</nav>", html, re.S)
                self.assertIsNotNone(nav)
                home, directory = ("#/", "#/dir") if path == ROOT / "web/index.html" else ("/", "/directory/")
                self.assertIn(f'<a href="{home}">셰프 DNA 계보도</a>', nav[1])
                self.assertIn(f'<a href="{directory}">식당 목록</a>', nav[1])
                self.assertNotIn(">계보</a>", nav[1])
                self.assertNotIn("디렉토리", nav[1])
                checked.append(path)
        self.assertIn(ROOT / "web/index.html", checked)
        self.assertIn(ROOT / "web/login/index.html", checked)

    def test_english_navigation_keeps_existing_labels_and_routes(self):
        pages = sorted((ROOT / "web/en").rglob("*.html"))
        self.assertTrue(pages)
        for path in pages:
            with self.subTest(page=path.relative_to(ROOT)):
                html = path.read_text(encoding="utf-8")
                nav = re.search(r"<nav\b[^>]*>(.*?)</nav>", html, re.S)
                self.assertIsNotNone(nav)
                self.assertIn('<a href="/en/directory/">Directory</a>', nav[1])
                self.assertIn('<a href="/#/">Lineage map</a>', nav[1])
                self.assertNotIn("DNA", nav[1])

    def test_public_pages_have_no_empty_ad_slots(self):
        for path in sorted((ROOT / "web").rglob("*.html")):
            with self.subTest(page=path.relative_to(ROOT)):
                html = path.read_text(encoding="utf-8").lower()
                for removed in ("sponsor", "광고 자리", "유료 소개"):
                    self.assertNotIn(removed, html)

    def test_generator_source_uses_approved_labels(self):
        source = read("scripts/build_pages.py")
        for required in (
            '<a href="/">셰프 DNA 계보도</a>',
            '<a href="/directory/">식당 목록</a>',
            "<h1>식당 목록</h1>",
            "전체 식당 목록",
            "식당 목록 (필터)",
            "셰프 DNA 계보도 보기",
            "계보 출처",
            "키친 게놈 식당 목록",
            "셰프 계보 식당 목록 | 키친 게놈",
        ):
            self.assertIn(required, source)
        self.assertNotIn("디렉토리", source)
        self.assertNotIn("인터랙티브 계보도 보기", source)

    def test_generated_pages_have_approved_ctas_and_metadata(self):
        seed = json.loads(read("data/seed.json"))
        kitchens = sorted((ROOT / "web/k").glob("*/index.html"))
        self.assertEqual({path.parent.name for path in kitchens}, {k["id"] for k in seed["kitchens"]})
        restaurants = sorted((ROOT / "web/r").glob("*/index.html"))
        self.assertEqual({path.parent.name for path in restaurants}, {r["id"] for r in seed["restaurants"]})
        for path in kitchens:
            with self.subTest(page=path.relative_to(ROOT)):
                html = path.read_text(encoding="utf-8")
                kitchen_id = path.parent.name
                self.assertIn(f'href="/#/k/{kitchen_id}">셰프 DNA 계보도 보기</a>', html)
                self.assertIn('href="/directory/">전체 식당 목록</a>', html)
                self.assertNotIn("인터랙티브 계보도 보기", html)
                self.assertNotIn("디렉토리", html)
        html = read("web/directory/index.html")
        self.assertIn("<h1>식당 목록</h1>", html)
        self.assertIn('href="/#/dir">식당 목록 (필터)</a>', html)
        title = "셰프 계보 식당 목록 | 키친 게놈"
        self.assertIn(f"<title>{title}</title>", html)
        self.assertIn(f'<meta property="og:title" content="{title}">', html)
        schema = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)[1])
        self.assertEqual(schema["@type"], "CollectionPage")
        self.assertEqual(schema["name"], "키친 게놈 식당 목록")
        self.assertTrue(schema["url"].endswith("/directory/"))


if __name__ == "__main__":
    unittest.main()
