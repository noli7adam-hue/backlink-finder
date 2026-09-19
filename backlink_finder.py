#!/usr/bin/env python3
"""
Scan a list of URLs and find links pointing to your own domains.

Usage:
  python3 backlink_finder.py urls.txt --domains mysite.com
  python3 backlink_finder.py urls.txt --domains-file domains.txt --out found.csv
  python3 backlink_finder.py urls.txt --domains a.com,b.com --workers 16

Output (stdout or --out), CSV:
  source_url, found_domain, link_url, anchor_text, rel, nofollow, error

A per-run summary is printed to stderr, so `--out -` stays pipe-clean.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import os
import re
import ssl
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, urljoin
from urllib.request import Request, HTTPRedirectHandler, HTTPSHandler, build_opener
from urllib.error import URLError, HTTPError

_INSECURE_CTX = ssl.create_default_context()
_INSECURE_CTX.check_hostname = False
_INSECURE_CTX.verify_mode = ssl.CERT_NONE

SCHEMES = ("http", "https")


class HttpOnlyRedirect(HTTPRedirectHandler):
    """urllib happily follows a redirect into ftp:// and friends. We don't."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme.lower() not in SCHEMES:
            raise HTTPError(newurl, code, f"redirect to non-http(s) URL: {newurl}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = build_opener(HttpOnlyRedirect())
_INSECURE_OPENER = build_opener(HttpOnlyRedirect(), HTTPSHandler(context=_INSECURE_CTX))

DEFAULT_DOMAINS_FILE = "domains.txt"

UA = "Mozilla/5.0 (compatible; backlink-finder/1.0; +https://github.com/izzipizzy/backlink-finder)"
TIMEOUT_SEC = 15
MAX_BYTES = 4 * 1024 * 1024  # 4MB cap per page
HTML_CTYPES = ("text/html", "application/xhtml", "application/xml", "text/xml", "text/plain")
GENERIC_CTYPES = ("", "application/octet-stream", "binary/octet-stream")

EXIT_OK = 0
EXIT_USAGE = 2


def die(msg: str) -> "NoReturn":  # noqa: F821
    print(f"backlink_finder.py: error: {msg}", file=sys.stderr)
    raise SystemExit(EXIT_USAGE)


_LABEL = re.compile(r"^[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?$")


def canon_host(value: str) -> str:
    """Canonicalize a host or a domain the user typed: strip scheme/path/query/port/userinfo,
    a leading 'www.', a trailing dot, and IDNA-encode so unicode and punycode match.
    Returns "" for anything that is not a usable host."""
    h = value.strip().lower()
    if not h:
        return ""
    if "//" in h:
        h = urlparse(h).hostname or ""      # already unbracketed and lowercased
    else:
        h = h.split("/")[0].split("?")[0].split("#")[0].split("@")[-1]
        if h.startswith("[") and "]" in h:  # [2001:db8::1]:8080
            h = h[1:h.index("]")]
    if h.count(":") >= 2:                   # IPv6 literal: no ports, no IDNA, no www
        return h
    h = h.split(":")[0].strip(".")
    if h.startswith("www."):
        h = h[4:]
    if not h:
        return ""
    try:
        h = h.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return ""                           # empty or over-long label
    if not all(_LABEL.match(part) for part in h.split(".")):
        return ""                           # leading/trailing hyphen, stray characters
    return h


def domain_matches(host: str, our: list[str]) -> str | None:
    """Return the longest matching 'our' domain, or None."""
    h = canon_host(host)
    if not h:
        return None
    best: str | None = None
    for d in our:
        if h == d or h.endswith("." + d):
            if best is None or len(d) > len(best):
                best = d
    return best


class LinkExtractor(HTMLParser):
    """Collect <a href> with anchor text. Nested anchors are invalid HTML; like a browser,
    an opening <a> closes the one before it, so text never leaks between links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict] = []
        self.base_href: str | None = None
        self._anchor: dict | None = None
        self._skip_depth = 0             # inside <script>/<style>

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip_depth += 1
            return
        if tag == "base" and self.base_href is None:
            href = (a.get("href") or "").strip()
            if href:
                self.base_href = href
            return
        if tag == "img" and self._anchor is not None:
            alt = (a.get("alt") or "").strip()
            if alt:
                self._anchor["text"] += " " + alt
            return
        if tag != "a":
            return
        self._anchor = None              # an <a> always ends the previous one
        href = (a.get("href") or "").strip()
        if not href:
            return
        rel = (a.get("rel") or "").strip()
        self._anchor = {
            "href": href,
            "rel": rel,
            "nofollow": "nofollow" in rel.lower().split(),
            "text": "",
        }
        self.links.append(self._anchor)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <base/>, <img/> and <a/> must not touch the script/style depth
        if tag.lower() in ("base", "img", "a"):
            self.handle_starttag(tag, attrs)
            if tag.lower() == "a":
                self._anchor = None

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "a":
            self._anchor = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and self._anchor is not None:
            self._anchor["text"] += data


def _is_ssl_error(reason: object) -> bool:
    if isinstance(reason, ssl.SSLError):
        return True
    name = type(reason).__name__
    return "SSL" in name or "Certificate" in name


def _content_type(raw_header: str | None) -> str:
    return (raw_header or "").split(";")[0].strip().lower()


def fetch(url: str, insecure: bool) -> tuple[str, bytes | None, str | None, str | None, bool]:
    """Returns (final_url, body|None, content_type|None, error|None, insecure_used)."""
    insecure_used = False
    attempts = (False, True) if insecure else (False,)
    for use_insecure in attempts:
        opener = _INSECURE_OPENER if use_insecure else _OPENER
        try:
            req = Request(
                url,
                headers={"User-Agent": UA, "Accept": "text/html,*/*", "Accept-Encoding": "identity"},
            )
            with opener.open(req, timeout=TIMEOUT_SEC) as resp:
                final = resp.geturl() or url
                ctype = _content_type(resp.headers.get("Content-Type"))
                if ctype not in GENERIC_CTYPES and not ctype.startswith(HTML_CTYPES):
                    return final, None, ctype, f"non-html content-type: {ctype or '?'}", insecure_used
                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                    return final, None, ctype, f"page > {MAX_BYTES // 1024 // 1024}MB, skipped", insecure_used
                data = resp.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    return final, None, ctype, f"page > {MAX_BYTES // 1024 // 1024}MB, skipped", insecure_used
                charset = resp.headers.get_content_charset()
                return final, data, charset or ctype, None, insecure_used
        except HTTPError as e:
            with e:  # an HTTPError is also an open response
                reason = f"HTTP {e.code}" if e.code >= 400 else f"{e.reason}"
            return url, None, None, reason, insecure_used
        except URLError as e:
            if not use_insecure and insecure and _is_ssl_error(e.reason):
                insecure_used = True
                continue
            if _is_ssl_error(e.reason):
                return url, None, None, f"ssl: {e.reason}", insecure_used
            return url, None, None, f"network: {e.reason}", insecure_used
        except ssl.SSLError as e:
            if not use_insecure and insecure:
                insecure_used = True
                continue
            return url, None, None, f"ssl: {e}", insecure_used
        except Exception as e:
            return url, None, None, f"{type(e).__name__}: {e}", insecure_used
    return url, None, None, "unreachable", insecure_used


_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?\s*([\w.:+-]+)""", re.I)
_XML_DECL = re.compile(rb"""^\s*<\?xml[^>]*?encoding=["']([\w.:+-]+)["']""", re.I)


def decode_html(raw: bytes, declared: str | None = None) -> str:
    """Decode using BOM > HTTP charset > <meta charset> / <?xml?> > utf-8 > cp1252."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8):].decode("utf-8", errors="replace")
    for bom, enc in ((codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
                     (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if raw.startswith(bom):
            return raw[len(bom):].decode(enc, errors="replace")

    candidates: list[str] = []
    if declared and "/" not in declared:
        candidates.append(declared)
    head = raw[:4096]
    for rx in (_META_CHARSET, _XML_DECL):
        m = rx.search(head)
        if m:
            candidates.append(m.group(1).decode("ascii", errors="ignore"))
    candidates += ["utf-8", "cp1252"]

    for enc in candidates:
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


_WS = re.compile(r"\s+")


def _row(source: str, error: str = "", **kw) -> dict:
    row = {
        "source_url": source,
        "found_domain": "",
        "link_url": "",
        "anchor_text": "",
        "rel": "",
        "nofollow": "",
        "error": error,
    }
    row.update(kw)
    return row


def scan(url: str, our: list[str], insecure: bool) -> tuple[list[dict], bool]:
    """Returns (rows, insecure_used). `source_url` stays the requested URL, so rows join
    back to the input list; relative links resolve against the final (post-redirect) URL."""
    if urlparse(url).scheme.lower() not in SCHEMES:
        return [_row(url, error="unsupported scheme (http/https only)")], False

    final, raw, declared, err, insecure_used = fetch(url, insecure)
    if raw is None:
        return [_row(url, error=err or "")], insecure_used

    html = decode_html(raw, declared)
    p = LinkExtractor()
    try:
        p.feed(html)
        p.close()          # flush anything the parser is still holding
    except Exception as e:
        return [_row(url, error=f"parse: {e}")], insecure_used

    base = urljoin(final, p.base_href) if p.base_href else final

    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()  # (link_url, anchor_text, rel) per source

    for a in p.links:
        absolute = urljoin(base, a["href"])
        parsed = urlparse(absolute)
        if parsed.scheme.lower() not in ("http", "https"):
            continue
        matched = domain_matches(parsed.hostname or "", our)
        if not matched:
            continue
        text = _WS.sub(" ", a["text"]).strip()[:200]
        key = (absolute, text, a["rel"])
        if key in seen:
            continue
        seen.add(key)

        rows.append(
            _row(
                url,
                found_domain=matched,
                link_url=absolute,
                anchor_text=text,
                rel=a["rel"],
                nofollow="yes" if a["nofollow"] else "",
            )
        )

    if not rows:
        # Source scanned successfully, just nothing found.
        rows.append(_row(url))
    return rows, insecure_used


# ---------------------------------------------------------------------------
# Фильтр служебных ссылок («навигация площадки»).
#
# Сканер отдаёт ВСЕ ссылки на заданный домен — включая меню, логотип, футер,
# хлебные крошки и пагинацию. Для проверки размещений это шум: интересует
# ссылка с осмысленным анкором, а не навигация самого донора. Ниже — эвристика
# с четырьмя причинами; срабатывание любой означает «служебная».
# ---------------------------------------------------------------------------

# Анкоры, которые на сайтах-донорах практически всегда означают навигацию.
_NAV_ANCHORS = {
    # ru
    "главная", "на главную", "главная страница", "о нас", "о компании", "о сайте",
    "контакты", "контакт", "обратная связь", "вопросы", "вопросы и ответы",
    "блог", "новости", "войти", "вход", "регистрация", "зарегистрироваться",
    "меню", "ещё", "еще", "показать ещё", "показать все", "далее", "назад",
    "следующий", "предыдущий", "партнеры", "партнёры", "каталог", "корзина",
    "поиск", "подписаться", "настройки", "профиль", "аккаунт", "личный кабинет",
    "выйти", "карта сайта", "политика конфиденциальности", "политика",
    "условия", "пользовательское соглашение", "cookies", "cookie", "язык",
    "поддержка", "помощь", "документация", "реклама", "авторизация",
    # en
    "home", "homepage", "about", "about us", "contacts", "contact", "contact us",
    "faq", "blog", "news", "login", "log in", "sign in", "signin", "register",
    "sign up", "signup", "join", "menu", "more", "show more", "show all",
    "next", "previous", "prev", "back", "partners", "catalog", "cart", "basket",
    "search", "subscribe", "settings", "profile", "account", "my account",
    "logout", "log out", "sign out", "sitemap", "privacy", "privacy policy",
    "terms", "terms of use", "language", "support", "help", "docs",
    "documentation", "advertise", "authorization",
}

# Служебные пути: ссылка на них — навигация, а не размещение.
_NAV_PATHS = {
    "/about", "/about-us", "/o-nas", "/contacts", "/contact", "/kontakty", "/faq",
    "/login", "/signin", "/sign-in", "/register", "/signup", "/sign-up", "/join",
    "/cart", "/basket", "/search", "/menu", "/sitemap", "/feed", "/rss",
    "/privacy", "/privacy-policy", "/terms", "/policy", "/profile", "/account",
    "/settings", "/logout", "/auth", "/help", "/support", "/docs", "/partners",
    "/blog", "/news", "/catalog", "/category", "/tags", "/tag",
}

_NAV_PATH_RE = re.compile(r"^/(?:page|p)/\d+$")
_NAV_QUERY_RE = re.compile(r"(?:^|&)(?:page|p|paged|pg|offset)=\d+(?:&|$)")
_ANCHOR_EDGE = " \t\n\r→»«\"'`.,:;!?|—–-()[]"


def nav_reason(link_url: str, anchor_text: str, source_url: str) -> str:
    """Почему ссылку считаем служебной: anchor-empty | self-link | url-root |
    url-service | url-pagination | anchor-nav. Пустая строка — обычная ссылка.

    Разделение важно: признаки, привязанные к адресу (корень сайта, служебный
    путь, пагинация), применяются только к ВНУТРЕННИМ ссылкам площадки — иначе
    можно выбросить реальное размещение, которое ведёт на наш корень.
    Анкорные признаки (пустой анкор, навигационное слово) применяются всегда:
    так выглядят шаблонные ссылки в меню и футере."""
    anchor = _WS.sub(" ", (anchor_text or "")).strip().lower().replace("ё", "е")
    anchor = anchor.strip(_ANCHOR_EDGE).strip()

    # 1. Пустой или чисто декоративный анкор (картинка, иконка, счётчик).
    if not re.search(r"[0-9a-zа-я]", anchor):
        return "anchor-empty"

    a, s = urlparse(link_url), urlparse(source_url)
    internal = a.netloc.lower() == s.netloc.lower()

    # 2. Ссылка на саму эту же страницу — логотип, «наверх», хлебные крошки.
    if internal and (a.path or "/").rstrip("/") == (s.path or "/").rstrip("/"):
        return "self-link"

    if internal:
        path = (a.path or "/").rstrip("/").lower() or "/"
        # 3. Корень сайта и служебные разделы площадки.
        if path == "/":
            return "url-root"
        if path in _NAV_PATHS or _NAV_PATH_RE.match(path):
            return "url-service"
        # 4. Пагинация: /page/2, ?page=2, ?p=3 …
        if _NAV_QUERY_RE.search(a.query or ""):
            return "url-pagination"

    # 5. Анкор из навигационного словаря — шаблонная ссылка.
    if anchor in _NAV_ANCHORS:
        return "anchor-nav"

    return ""


def positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}")
    if not 1 <= n <= 64:
        raise argparse.ArgumentTypeError("must be between 1 and 64")
    return n


def read_lines(path: str, what: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    except OSError as e:
        die(f"cannot read {what} {path!r}: {e.strerror or e}")
    except UnicodeDecodeError as e:
        die(f"{what} {path!r} is not valid UTF-8: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="text file with URLs, one per line")
    ap.add_argument("--out", default="-", help="CSV output path (default: stdout)")
    ap.add_argument("--domains", default="", help="comma-separated domains to look for")
    ap.add_argument("--domains-file", default="", help=f"file with one domain per line (default: ./{DEFAULT_DOMAINS_FILE} if it exists)")
    ap.add_argument("--workers", type=positive_int, default=8, help="parallel HTTP workers, 1-64 (default 8)")
    ap.add_argument("--insecure", action="store_true", help="retry TLS failures with certificate verification OFF")
    ap.add_argument("--nav-filter", choices=("on", "off"), default="on",
                    help="drop site navigation (menu, logo, footer, breadcrumbs, pagination) from the report; "
                         "'off' restores upstream behaviour (default on)")
    ap.add_argument("--nav-out", default="", help="optional CSV path to also write the rows dropped as navigation")
    args = ap.parse_args()

    raw_domains: list[str] = []
    if args.domains:
        raw_domains.extend([d for d in args.domains.split(",") if d.strip()])

    dom_file = args.domains_file or (DEFAULT_DOMAINS_FILE if Path(DEFAULT_DOMAINS_FILE).exists() else "")
    if dom_file:
        raw_domains.extend(read_lines(dom_file, "domains file"))

    our = sorted({canon_host(d) for d in raw_domains} - {""})
    if not our:
        if raw_domains:
            die("no usable domain in --domains/--domains-file")
        die("no domains given: use --domains, --domains-file, or create ./" + DEFAULT_DOMAINS_FILE)

    urls: list[str] = []
    seen_urls: set[str] = set()
    for u in read_lines(args.file, "URL file"):
        if u not in seen_urls:
            seen_urls.add(u)
            urls.append(u)
    if not urls:
        die(f"no URLs in {args.file!r}")

    if args.out == "-":
        out = sys.stdout
    else:
        try:
            out = open(args.out, "w", encoding="utf-8", newline="")
        except OSError as e:
            die(f"cannot write --out {args.out!r}: {e.strerror or e}")

    writer = csv.DictWriter(
        out,
        fieldnames=["source_url", "found_domain", "link_url", "anchor_text", "rel", "nofollow", "error"],
    )
    writer.writeheader()

    nav_filter_on = args.nav_filter == "on"
    nav_writer = None
    nav_out_fh = None
    if args.nav_out:
        try:
            nav_out_fh = open(args.nav_out, "w", encoding="utf-8", newline="")
        except OSError as e:
            die(f"cannot write --nav-out {args.nav_out!r}: {e.strerror or e}")
        nav_writer = csv.DictWriter(
            nav_out_fh,
            fieldnames=["source_url", "found_domain", "link_url", "anchor_text", "rel", "nofollow", "error"],
        )
        nav_writer.writeheader()

    nav_filtered = 0
    nav_reason_counter: Counter[str] = Counter()
    nav_only_sources: set[str] = set()

    total_urls = len(urls)
    found_link_count = 0           # number of (source × link × our-domain) rows
    sources_with_match: set[str] = set()
    sources_with_error: set[str] = set()
    domain_counter: Counter[str] = Counter()    # how many link rows per our-domain
    source_per_domain: dict[str, set[str]] = {}  # how many distinct sources link to each our-domain
    error_counter: Counter[str] = Counter()
    insecure_count = 0
    insecure_examples: list[str] = []

    def run(u: str) -> tuple[str, list[dict], bool]:
        try:
            rows, used = scan(u, our, args.insecure)
        except Exception as e:  # never let one URL kill the run
            rows, used = [_row(u, error=f"{type(e).__name__}: {e}")], False
        return u, rows, used

    write_error: str | None = None
    broken_pipe = False
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for url, rows, insecure_used in ex.map(run, urls):  # input order, streamed
                if insecure_used:
                    insecure_count += 1
                    if len(insecure_examples) < 5:
                        insecure_examples.append(url)
                url_matched = 0
                url_kept = 0
                for r in rows:
                    if r["found_domain"]:
                        url_matched += 1
                        if nav_filter_on:
                            reason = nav_reason(r["link_url"], r["anchor_text"], r["source_url"])
                            if reason:
                                nav_filtered += 1
                                nav_reason_counter[reason] += 1
                                if nav_writer is not None:
                                    nav_writer.writerow(r)
                                continue
                        url_kept += 1
                    writer.writerow(r)
                    if r["found_domain"]:
                        found_link_count += 1
                        sources_with_match.add(r["source_url"])
                        domain_counter[r["found_domain"]] += 1
                        source_per_domain.setdefault(r["found_domain"], set()).add(r["source_url"])
                    if r["error"]:
                        sources_with_error.add(r["source_url"])
                        # Coarse classification of error.
                        err = r["error"]
                        if err.startswith("HTTP "):
                            error_counter[err[:8]] += 1   # "HTTP 404", "HTTP 503", etc.
                        elif err.startswith("network:"):
                            error_counter["network"] += 1
                        elif err.startswith("ssl:"):
                            error_counter["ssl"] += 1
                        elif err.startswith("non-html"):
                            error_counter["non-html"] += 1
                        elif err.startswith("page >"):
                            error_counter["too-large"] += 1
                        elif err.startswith("parse:"):
                            error_counter["parse"] += 1
                        else:
                            error_counter["other"] += 1
                if url_matched and not url_kept:
                    # Всё, что нашлось на источнике, оказалось навигацией.
                    nav_only_sources.add(url)
    except BrokenPipeError:
        broken_pipe = True            # downstream closed the pipe, e.g. `| head`
    except OSError as e:
        write_error = f"cannot write output: {e.strerror or e}"
    finally:
        try:
            if nav_out_fh is not None:
                nav_out_fh.close()
        except OSError:
            pass
        try:
            if out is not sys.stdout:
                out.close()
            else:
                out.flush()
        except BrokenPipeError:
            broken_pipe = True
        except (OSError, ValueError) as e:
            write_error = write_error or f"cannot write output: {getattr(e, 'strerror', None) or e}"

    if broken_pipe:
        # keep the interpreter from raising again while flushing at exit
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK
    if write_error:
        die(write_error)

    # ---- Final report (stderr so it doesn't pollute CSV stdout) ----
    sources_clean = total_urls - len(sources_with_match) - len(sources_with_error)
    print("", file=sys.stderr)
    print("=== Scan report ====================================================", file=sys.stderr)
    print(f"  URLs scanned                : {total_urls}", file=sys.stderr)
    print(f"  Sources with our link(s)    : {len(sources_with_match)}", file=sys.stderr)
    print(f"  Sources with ZERO matches   : {max(0, sources_clean)}", file=sys.stderr)
    print(f"  Sources with errors         : {len(sources_with_error)}", file=sys.stderr)
    print(f"  Total link occurrences      : {found_link_count}", file=sys.stderr)
    if nav_filter_on:
        print("  --- Navigation filter (on) ---", file=sys.stderr)
        detail = ""
        if nav_filtered:
            detail = "  reasons: " + ", ".join(f"{k} {v}" for k, v in nav_reason_counter.most_common())
        print(f"    dropped as navigation    : {nav_filtered} link(s){detail}", file=sys.stderr)
        if nav_only_sources:
            print(f"    sources whose only matches were navigation: {len(nav_only_sources)}"
                  "  (they count towards ZERO matches above)", file=sys.stderr)
        if nav_filtered == 0:
            print("    (nothing looked like site navigation on these pages)", file=sys.stderr)
    else:
        print("  Navigation filter           : off (upstream behaviour)", file=sys.stderr)
    if args.insecure:
        print(f"  Scanned with TLS verify OFF : {insecure_count}"
              + (f"  e.g. {', '.join(insecure_examples[:3])}" if insecure_examples else ""),
              file=sys.stderr)
    if domain_counter:
        print("  --- Our domains found ---", file=sys.stderr)
        width = max(len(d) for d in domain_counter)
        for d, n in domain_counter.most_common():
            uniq_sources = len(source_per_domain.get(d, set()))
            print(f"    {d:<{width}}  {n:>5} link(s)  on {uniq_sources:>4} source(s)", file=sys.stderr)
    if error_counter:
        print("  --- Errors by type ---", file=sys.stderr)
        for kind, n in error_counter.most_common():
            print(f"    {kind:<12}  {n:>5}", file=sys.stderr)
    print("=====================================================================", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
