"""Tests for the navigation filter (Hermes addition, not part of upstream).

Upstream scans a donor page and reports EVERY link to the requested domain,
including the site's own menu, logo, footer and pagination. These tests pin the
behaviour of `nav_reason()` and of the CLI switch `--nav-filter on|off`.
"""
import csv
import http.server
import os
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backlink_finder as bf  # noqa: E402

DONOR = "https://donor.ru/stati/okna-pvh"


class TestNavReason(unittest.TestCase):
    """Unit level: what counts as site navigation."""

    def check(self, link, anchor, expected, source=DONOR):
        got = bf.nav_reason(link, anchor, source)
        self.assertEqual(got, expected, f"{link!r} / {anchor!r}: ждали {expected!r}, получили {got!r}")

    # --- внутренняя навигация площадки: отсеиваем -------------------------
    def test_root_of_donor(self):
        self.check("https://donor.ru/", "Главная", "url-root")

    def test_root_with_logo_alt(self):
        self.check("https://donor.ru", "Donor logo", "url-root")

    def test_service_paths(self):
        for path, anchor in [("/about", "О нас"), ("/contacts", "Контакты"),
                             ("/faq", "FAQ"), ("/login", "Войти"),
                             ("/cart", "Корзина"), ("/blog", "Блог"),
                             ("/sitemap", "Карта сайта"), ("/privacy", "Политика")]:
            self.check("https://donor.ru" + path, anchor, "url-service")

    def test_service_path_ignores_case_and_slash(self):
        self.check("https://donor.ru/About/", "О нас", "url-service")

    def test_pagination_path(self):
        self.check("https://donor.ru/page/3", "Статьи", "url-service")

    def test_pagination_query(self):
        for q in ("?page=2", "?p=3", "?paged=4&x=1", "?offset=20"):
            self.check("https://donor.ru/stati/okna-pvh" + q, "Дальше", "url-pagination",
                       source="https://donor.ru/stati/drugaya-statya")

    def test_self_link(self):
        self.check(DONOR, "Читать целиком", "self-link")
        self.check(DONOR + "/", "Наверх", "self-link")

    def test_empty_and_decorative_anchors(self):
        for anchor in ("", "   ", "→", "»", "...", "|"):
            self.check("https://donor.ru/stati/okna-pvh", anchor, "anchor-empty")

    def test_nav_anchor_words(self):
        other = "https://donor.ru/stati/drugaya-statya"
        for anchor in ("Главная", "ГЛАВНАЯ", "На главную", "О компании", "Меню",
                       "Ещё", "Далее", "Назад", "Партнёры", "Поиск", "Login",
                       "Sign in", "About us", "More"):
            self.check("https://donor.ru/stati/okna-pvh", anchor, "anchor-nav", source=other)

    def test_nav_anchor_beats_clean_path(self):
        self.check("https://donor.ru/stati/okna-pvh", "  Главная  ", "anchor-nav",
                   source="https://donor.ru/stati/drugaya-statya")

    # --- реальные размещения: НЕ отсеиваем --------------------------------
    def test_external_placement_is_kept(self):
        self.check("https://mysite.com/katalog/okna-pvh", "пластиковые окна под ключ", "")

    def test_external_root_is_kept(self):
        """Ссылка на наш корень — это размещение, а не навигация донора."""
        self.check("https://mysite.com/", "Сайт компании Ромашка", "")

    def test_external_service_path_is_kept(self):
        self.check("https://mysite.com/about", "о компании Ромашка", "")

    def test_external_pagination_is_kept(self):
        self.check("https://mysite.com/page/2", "каталог окон, страница 2", "")

    def test_soft_anchors_are_kept(self):
        for anchor in ("Подробнее", "Читать далее", "Узнать больше", "Подробнее о монтаже окон"):
            self.check("https://mysite.com/article", anchor, "")

    def test_subdomain_of_our_site_is_kept(self):
        self.check("https://blog.mysite.com/post", "разбор монтажа", "")

    def test_anchor_with_comma_and_length(self):
        self.check("https://mysite.com/x", "Окна ПВХ, монтаж под ключ — Москва", "")

    def test_www_vs_bare_host_not_confused(self):
        """www.donor.ru и donor.ru — не одна и та же внутренняя ссылка."""
        self.check("https://www.donor.ru/", "наши статьи", "")

    def test_external_link_same_path_as_source_is_kept(self):
        self.check("https://mysite.com/stati/okna-pvh", "окна под ключ", "")


class TestNavFilterCli(unittest.TestCase):
    """Integration: the CLI switch actually drops navigation from the CSV."""

    @classmethod
    def setUpClass(cls):
        page = (
            "<html><head><title>t</title></head><body>"
            '<a href="/">Home</a>'
            '<a href="/about">О нас</a>'
            '<a href="/faq">FAQ</a>'
            '<a href="/page/2">2</a>'
            '<a href="/">'          # логотип: пустой анкор (img без alt)
            "</a>"
            '<a href="/cart">Корзина</a>'
            '<a href="http://127.0.0.1:{port}/about">Мы в каталоге</a>'
            '<a href="https://mysite.com/katalog/okna-pvh">пластиковые окна под ключ</a>'
            "</body></html>"
        )

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = page.format(port=self.server.server_port).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # silence test server
                pass

        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.port = cls.srv.server_port
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.tmp = tempfile.mkdtemp(prefix="blf-nav-")
        cls.urls = os.path.join(cls.tmp, "urls.txt")
        with open(cls.urls, "w", encoding="utf-8") as f:
            f.write(f"http://127.0.0.1:{cls.port}/stati/okna\n")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def run_cli(self, *extra):
        out = os.path.join(self.tmp, "out.csv")
        p = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backlink_finder.py"),
             self.urls, "--domains", "127.0.0.1,mysite.com", "--out", out, *extra],
            capture_output=True, text=True, timeout=120,
        )
        with open(out, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        return rows, p.stderr

    def test_filter_on_keeps_only_real_placements(self):
        rows, err = self.run_cli()
        links = [r["link_url"] for r in rows if r["found_domain"]]
        # остаётся только внешнее размещение: внутренние ссылки донора (в т.ч. /about,
        # где анкор осмысленный) — это навигация площадки, их в отчёте быть не должно
        self.assertEqual(links, ["https://mysite.com/katalog/okna-pvh"], links)
        self.assertIn("Navigation filter (on)", err)
        self.assertIn("dropped as navigation", err)
        self.assertNotIn("Home", " ".join(r["anchor_text"] for r in rows))

    def test_filter_off_restores_upstream_behaviour(self):
        rows, err = self.run_cli("--nav-filter", "off")
        links = [r["link_url"] for r in rows if r["found_domain"]]
        self.assertGreater(len(links), 2, "без фильтра навигация должна остаться в отчёте")
        self.assertTrue(any(u.endswith("/about") for u in links), links)
        self.assertTrue(any(u.endswith("/cart") for u in links), links)
        self.assertIn("Navigation filter           : off", err)

    def test_nav_out_writes_dropped_rows(self):
        nav_out = os.path.join(self.tmp, "dropped.csv")
        rows, err = self.run_cli("--nav-out", nav_out)
        self.assertTrue(os.path.exists(nav_out))
        with open(nav_out, encoding="utf-8") as f:
            dropped = list(csv.DictReader(f))
        anchors = " ".join(r["anchor_text"] for r in dropped)
        self.assertIn("Home", anchors)
        self.assertIn("О нас", anchors)
        # отсеянное не попало в основной отчёт
        self.assertNotIn("Home", " ".join(r["anchor_text"] for r in rows if r["found_domain"]))

    def test_nav_reason_counts_are_reported(self):
        rows, err = self.run_cli()
        self.assertIn("reasons:", err)
        self.assertIn("url-root", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
