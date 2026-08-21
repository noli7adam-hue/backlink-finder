#!/usr/bin/env python3
"""Stdlib-only tests: python3 -m unittest -v (or ./test_backlink_finder.py)."""

import codecs
import csv
import io
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import backlink_finder as bf

HERE = Path(__file__).parent
SCRIPT = HERE / "backlink_finder.py"

PAGES = {
    "/plain": (200, "text/html; charset=utf-8", b"""
        <a href="https://mysite.com/a">alpha</a>
        <a href="https://www.mysite.com/b" rel="nofollow sponsored">beta</a>
        <a href="https://other.net/">nope</a>
        <a href="mailto:x@mysite.com">mail</a>
        <a href="javascript:go('mysite.com')">js</a>
    """),
    "/relative": (200, "text/html", b'<a href="/from-root">root</a>'),
    "/based": (200, "text/html", b'<base href="https://mysite.com/dir/"><a href="page">based</a>'),
    "/img": (200, "text/html", b'<a href="https://mysite.com/i"><img alt="  banner  alt "></a>'),
    "/noise": (200, "text/html", b'<a href="https://mysite.com/n">te<script>var x="X"</script>xt</a>'),
    "/dupes": (200, "text/html", b'<a href="https://mysite.com/d">one</a><a href="https://mysite.com/d">one</a>'
                                 b'<a href="https://mysite.com/d">two</a>'),
    "/deep": (200, "text/html", b'<a href="https://shop.mysite.com/x">shop</a>'),
    "/cp1251": (200, "text/html; charset=windows-1251",
                '<a href="https://mysite.com/ru">Казино</a>'.encode("cp1251")),
    "/metacharset": (200, "text/html",
                     '<meta charset="utf-8"><a href="https://mysite.com/u">über</a>'.encode("utf-8")),
    "/empty": (200, "text/html", b"<p>nothing here</p>"),
    "/gone": (404, "text/html", b"nope"),
    "/pdfish": (200, "application/pdf", b"%PDF-1.4"),
    "/notype": (200, None, b'<a href="https://mysite.com/nt">no ctype</a>'),
    "/selfclosing": (200, "text/html", b'<a href="https://mysite.com/sc"/>after'),
    "/nested": (200, "text/html", b'<a href="https://mysite.com/outer">before<a>x</a>after</a>'),
    "/trailing": (200, "text/html", b'<a href="https://mysite.com/t">tail &amp'),
    "/utf16": (200, "text/html",
               codecs.BOM_UTF16_LE + '<a href="https://mysite.com/16">ü16</a>'.encode("utf-16-le")),
    "/utf32": (200, "text/html",
               codecs.BOM_UTF32_LE + '<a href="https://mysite.com/32">ü32</a>'.encode("utf-32-le")),
    "/xmldecl": (200, "text/html",
                 '<?xml version="1.0" encoding="cp1251"?><a href="https://mysite.com/x">Казино</a>'.encode("cp1251")),
    "/fakeattr": (200, "text/html",
                  '<div encoding="latin-1"></div><a href="https://mysite.com/f">über</a>'.encode("utf-8")),
    "/badcharset": (200, "text/html; charset=nonsense-9",
                    '<a href="https://mysite.com/b">plain</a>'.encode("utf-8")),
    "/cp1252": (200, "text/html", b'<a href="https://mysite.com/c">caf\xe9</a>'),
    "/relmix": (200, "text/html", b'<a href="https://mysite.com/r">same</a>'
                                  b'<a href="https://mysite.com/r" rel="nofollow">same</a>'),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/relative")
            self.end_headers()
            return
        if self.path == "/ftpredirect":
            self.send_response(302)
            self.send_header("Location", "ftp://files.example.com/x")
            self.end_headers()
            return
        if self.path == "/biglie":
            body = b'<a href="https://mysite.com/big">tiny</a>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "99999999")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path not in PAGES:
            self.send_error(404)
            return
        status, ctype, body = PAGES[self.path]
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def rows(self, path, domains=("mysite.com",)):
        rows, _ = bf.scan(self.base + path, sorted(domains), False)
        return rows

    def links(self, path, domains=("mysite.com",)):
        return [r for r in self.rows(path, domains) if r["found_domain"]]


class TestMatching(ServerCase):
    def test_finds_links_and_skips_others(self):
        got = self.links("/plain")
        self.assertEqual([r["link_url"] for r in got],
                         ["https://mysite.com/a", "https://www.mysite.com/b"])

    def test_anchor_rel_and_nofollow(self):
        a, b = self.links("/plain")
        self.assertEqual((a["anchor_text"], a["nofollow"]), ("alpha", ""))
        self.assertEqual((b["rel"], b["nofollow"]), ("nofollow sponsored", "yes"))

    def test_mailto_and_javascript_are_skipped(self):
        self.assertEqual(len(self.links("/plain")), 2)

    def test_relative_link_resolves_against_source(self):
        got = self.links("/relative", domains=("127.0.0.1",))
        self.assertEqual(got[0]["link_url"], self.base + "/from-root")

    def test_redirect_is_followed_and_used_as_base(self):
        rows = self.rows("/redirect", domains=("127.0.0.1",))
        self.assertEqual(rows[0]["link_url"], self.base + "/from-root")
        # source_url stays the requested URL so rows join back to the input list
        self.assertEqual(rows[0]["source_url"], self.base + "/redirect")

    def test_base_href_wins_over_source_url(self):
        self.assertEqual(self.links("/based")[0]["link_url"], "https://mysite.com/dir/page")

    def test_img_alt_becomes_anchor_text(self):
        self.assertEqual(self.links("/img")[0]["anchor_text"], "banner alt")

    def test_script_text_is_not_anchor_text(self):
        self.assertEqual(self.links("/noise")[0]["anchor_text"], "text")

    def test_identical_anchors_dedupe_but_different_text_does_not(self):
        self.assertEqual([r["anchor_text"] for r in self.links("/dupes")], ["one", "two"])

    def test_longest_domain_wins(self):
        got = self.links("/deep", domains=("mysite.com", "shop.mysite.com"))
        self.assertEqual(got[0]["found_domain"], "shop.mysite.com")


class TestParsing(ServerCase):
    def test_self_closing_anchor_is_found(self):
        got = self.links("/selfclosing")
        self.assertEqual(got[0]["link_url"], "https://mysite.com/sc")
        self.assertEqual(got[0]["anchor_text"], "")   # text after <a/> belongs to no link

    def test_nested_anchor_does_not_swallow_text(self):
        got = self.links("/nested")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["anchor_text"], "before")

    def test_trailing_buffered_text_is_flushed(self):
        self.assertEqual(self.links("/trailing")[0]["anchor_text"], "tail &")

    def test_same_url_and_text_but_different_rel_are_both_kept(self):
        got = self.links("/relmix")
        self.assertEqual([r["nofollow"] for r in got], ["", "yes"])


class TestSchemes(ServerCase):
    def test_non_http_input_url_is_rejected(self):
        rows, _ = bf.scan("ftp://files.example.com/x", ["mysite.com"], False)
        self.assertIn("unsupported scheme", rows[0]["error"])

    def test_redirect_into_non_http_is_blocked(self):
        row, = self.rows("/ftpredirect")
        self.assertIn("non-http(s)", row["error"])


class TestEncoding(ServerCase):
    def test_http_charset_header_is_honoured(self):
        self.assertEqual(self.links("/cp1251")[0]["anchor_text"], "Казино")

    def test_meta_charset_is_honoured(self):
        self.assertEqual(self.links("/metacharset")[0]["anchor_text"], "über")

    def test_xml_declaration_is_honoured(self):
        self.assertEqual(self.links("/xmldecl")[0]["anchor_text"], "Казино")

    def test_encoding_attribute_outside_xml_declaration_is_ignored(self):
        self.assertEqual(self.links("/fakeattr")[0]["anchor_text"], "über")

    def test_utf16_bom(self):
        self.assertEqual(self.links("/utf16")[0]["anchor_text"], "ü16")

    def test_utf32_bom_is_not_mistaken_for_utf16(self):
        self.assertEqual(self.links("/utf32")[0]["anchor_text"], "ü32")

    def test_unknown_declared_charset_falls_back(self):
        self.assertEqual(self.links("/badcharset")[0]["anchor_text"], "plain")

    def test_cp1252_fallback_for_undeclared_legacy_bytes(self):
        self.assertEqual(self.links("/cp1252")[0]["anchor_text"], "café")


class TestRowSemantics(ServerCase):
    def test_scanned_but_empty_page_has_no_error(self):
        row, = self.rows("/empty")
        self.assertEqual((row["found_domain"], row["error"]), ("", ""))

    def test_http_error_is_reported(self):
        row, = self.rows("/gone")
        self.assertEqual(row["error"], "HTTP 404")

    def test_non_html_is_skipped_with_reason(self):
        row, = self.rows("/pdfish")
        self.assertIn("non-html", row["error"])

    def test_missing_content_type_is_still_scanned(self):
        self.assertEqual(len(self.links("/notype")), 1)

    def test_declared_oversize_is_skipped_without_downloading(self):
        row, = self.rows("/biglie")
        self.assertIn("4MB", row["error"])


class TestCanonHost(unittest.TestCase):
    def test_accepts_what_users_actually_paste(self):
        for raw in ("mysite.com", "www.mysite.com", "https://mysite.com/path?q=1",
                    "MySite.com.", "mysite.com:8443", "user@mysite.com"):
            self.assertEqual(bf.canon_host(raw), "mysite.com", raw)

    def test_idn_and_punycode_are_the_same_host(self):
        self.assertEqual(bf.canon_host("bücher.de"), bf.canon_host("xn--bcher-kva.de"))

    def test_ipv6_literals_canonicalize_consistently(self):
        want = "2001:db8::1"
        for raw in ("[2001:db8::1]", "2001:DB8::1", "http://[2001:db8::1]:8080/x"):
            self.assertEqual(bf.canon_host(raw), want, raw)

    def test_invalid_domains_are_rejected(self):
        for raw in ("a..com", "-bad.com", "bad-.com", "", "   ", "x" * 70 + ".com"):
            self.assertEqual(bf.canon_host(raw), "", repr(raw))

    def test_query_string_is_stripped_from_a_bare_domain(self):
        self.assertEqual(bf.canon_host("mysite.com?x=1"), "mysite.com")

    def test_subdomain_matches_apex_but_not_a_lookalike(self):
        self.assertEqual(bf.domain_matches("blog.mysite.com", ["mysite.com"]), "mysite.com")
        self.assertIsNone(bf.domain_matches("notmysite.com", ["mysite.com"]))


class TestCli(ServerCase):
    def run_cli(self, *args, stdin_urls=None, tmp=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              capture_output=True, text=True, cwd=tmp or HERE)

    def test_missing_domains_is_a_usage_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            urls = Path(tmp) / "u.txt"
            urls.write_text(self.base + "/empty\n")
            r = self.run_cli(str(urls), tmp=tmp)
            self.assertEqual(r.returncode, 2)
            self.assertIn("no domains given", r.stderr)

    def test_unreadable_url_file_is_a_clean_error(self):
        r = self.run_cli("does-not-exist.txt", "--domains", "mysite.com")
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot read URL file", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_unwritable_out_is_a_clean_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            urls = Path(tmp) / "u.txt"
            urls.write_text(self.base + "/empty\n")
            r = self.run_cli(str(urls), "--domains", "mysite.com", "--out", tmp + "/nope/out.csv")
            self.assertEqual(r.returncode, 2)
            self.assertIn("cannot write --out", r.stderr)

    def test_bad_workers_is_rejected(self):
        r = self.run_cli("x.txt", "--domains", "mysite.com", "--workers", "0")
        self.assertEqual(r.returncode, 2)

    def test_csv_goes_to_stdout_and_report_to_stderr(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            urls = Path(tmp) / "u.txt"
            urls.write_text(f"{self.base}/plain\n{self.base}/empty\n")
            r = self.run_cli(str(urls), "--domains", "mysite.com", tmp=tmp)
            self.assertEqual(r.returncode, 0)
            rows = list(csv.DictReader(io.StringIO(r.stdout)))
            self.assertEqual([row["link_url"] for row in rows],
                             ["https://mysite.com/a", "https://www.mysite.com/b", ""])
            self.assertIn("Scan report", r.stderr)
            self.assertNotIn("Scan report", r.stdout)

    def test_output_follows_input_order(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            urls = Path(tmp) / "u.txt"
            urls.write_text(f"{self.base}/empty\n{self.base}/plain\n{self.base}/empty\n")  # dupe dropped
            r = self.run_cli(str(urls), "--domains", "mysite.com", tmp=tmp)
            rows = list(csv.DictReader(io.StringIO(r.stdout)))
            self.assertEqual([row["source_url"] for row in rows],
                             [self.base + "/empty", self.base + "/plain", self.base + "/plain"])
            self.assertIn("URLs scanned                : 2", r.stderr)

    def test_all_domains_invalid_is_a_usage_error(self):
        r = self.run_cli("x.txt", "--domains", "a..com,-bad.com")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no usable domain", r.stderr)

    def test_broken_pipe_does_not_traceback(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            urls = Path(tmp) / "u.txt"
            urls.write_text("\n".join(f"{self.base}/plain" for _ in range(1)) + "\n")
            proc = subprocess.run(
                f"{sys.executable} {SCRIPT} {urls} --domains mysite.com 2>/dev/null | head -1",
                shell=True, capture_output=True, text=True, cwd=tmp)
            self.assertEqual(proc.returncode, 0)
            self.assertNotIn("Traceback", proc.stderr)

    def test_domains_file_is_picked_up_from_cwd(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "domains.txt").write_text("# mine\nhttps://mysite.com/\n")
            urls = Path(tmp) / "u.txt"
            urls.write_text(self.base + "/plain\n")
            r = self.run_cli(str(urls), tmp=tmp)
            self.assertEqual(r.returncode, 0)
            self.assertIn("https://mysite.com/a", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
