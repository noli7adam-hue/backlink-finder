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
import re
import ssl
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, urljoin
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

_INSECURE_CTX = ssl.create_default_context()
_INSECURE_CTX.check_hostname = False
_INSECURE_CTX.verify_mode = ssl.CERT_NONE

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


def canon_host(value: str) -> str:
    """Canonicalize a host or a domain the user typed: strip scheme/path/port/userinfo,
    a leading 'www.', a trailing dot, and IDNA-encode so unicode and punycode match."""
    h = value.strip().lower()
    if not h:
        return ""
    if "//" in h:
        h = urlparse(h).hostname or ""
    else:
        h = h.split("/")[0].split("@")[-1]
        # strip :port, but leave a bare IPv6 literal alone
        if not h.startswith("[") and ":" in h:
            h = h.split(":")[0]
    h = h.strip(".")
    if h.startswith("www."):
        h = h[4:]
    if not h:
        return ""
    try:
        h = h.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        pass
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
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict] = []
        self.base_href: str | None = None
        self._anchors: list[dict] = []   # stack, tolerates malformed nesting
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
        if tag == "img" and self._anchors:
            alt = (a.get("alt") or "").strip()
            if alt:
                self._anchors[-1]["text"] += " " + alt
            return
        if tag != "a":
            return
        href = (a.get("href") or "").strip()
        if not href:
            return
        rel = (a.get("rel") or "").strip()
        anchor = {
            "href": href,
            "rel": rel,
            "nofollow": "nofollow" in rel.lower().split(),
            "text": "",
        }
        self._anchors.append(anchor)
        self.links.append(anchor)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <base ... /> and <img ... /> must not push/pop skip state
        if tag.lower() in ("base", "img"):
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "a" and self._anchors:
            self._anchors.pop()

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and self._anchors:
            self._anchors[-1]["text"] += data


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
        ctx = _INSECURE_CTX if use_insecure else None
        try:
            req = Request(
                url,
                headers={"User-Agent": UA, "Accept": "text/html,*/*", "Accept-Encoding": "identity"},
            )
            with urlopen(req, timeout=TIMEOUT_SEC, context=ctx) as resp:
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
            return url, None, None, f"HTTP {e.code}", insecure_used
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
_XML_ENCODING = re.compile(rb"""encoding=["']([\w.:+-]+)["']""", re.I)


def decode_html(raw: bytes, declared: str | None = None) -> str:
    """Decode using BOM > HTTP charset > <meta charset> / XML declaration > utf-8 > cp1252."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8):].decode("utf-8", errors="replace")
    for bom, enc in ((codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if raw.startswith(bom):
            return raw[len(bom):].decode(enc, errors="replace")

    candidates: list[str] = []
    if declared and "/" not in declared:
        candidates.append(declared)
    head = raw[:4096]
    for rx in (_META_CHARSET, _XML_ENCODING):
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
    final, raw, declared, err, insecure_used = fetch(url, insecure)
    if raw is None:
        return [_row(url, error=err or "")], insecure_used

    html = decode_html(raw, declared)
    p = LinkExtractor()
    try:
        p.feed(html)
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

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for url, rows, insecure_used in ex.map(run, urls):  # input order, streamed
                if insecure_used:
                    insecure_count += 1
                    if len(insecure_examples) < 5:
                        insecure_examples.append(url)
                for r in rows:
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
    finally:
        if out is not sys.stdout:
            out.close()

    # ---- Final report (stderr so it doesn't pollute CSV stdout) ----
    sources_clean = total_urls - len(sources_with_match) - len(sources_with_error)
    print("", file=sys.stderr)
    print("=== Scan report ====================================================", file=sys.stderr)
    print(f"  URLs scanned                : {total_urls}", file=sys.stderr)
    print(f"  Sources with our link(s)    : {len(sources_with_match)}", file=sys.stderr)
    print(f"  Sources with ZERO matches   : {max(0, sources_clean)}", file=sys.stderr)
    print(f"  Sources with errors         : {len(sources_with_error)}", file=sys.stderr)
    print(f"  Total link occurrences      : {found_link_count}", file=sys.stderr)
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
