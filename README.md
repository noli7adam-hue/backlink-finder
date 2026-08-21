# backlink-finder

Scan a list of pages and find which of them actually link to **your** domains — with the anchor text and the `rel` attribute.

Single file, standard library only, no dependencies.

## Why

Link building produces a spreadsheet of promised placements. This tells you which ones are real:

- Did the link actually go live on that page?
- What anchor text did they use?
- Is it `nofollow` / `sponsored` — or a real dofollow link?
- Did they quietly swap the URL, or point to a subdomain?

Point it at a list of donor/outreach URLs and you get one CSV row per link found.

## Requirements

Python 3.9+. Nothing else.

```bash
git clone https://github.com/izzipizzy/backlink-finder.git
cd backlink-finder
```

## Quick start

```bash
# 1. list the pages to check, one URL per line
cat > urls.txt <<'EOF'
https://blog.example-donor.com/best-widgets/
https://forum.example-donor.net/thread/1234
EOF

# 2. scan them for links to your domain(s)
python3 backlink_finder.py urls.txt --domains mysite.com --out found.csv
```

Domains can also live in a file (handy when you track many sites):

```bash
python3 backlink_finder.py urls.txt --domains-file domains.txt --out found.csv
```

If `./domains.txt` exists it is picked up automatically, so plain
`python3 backlink_finder.py urls.txt` is enough for day-to-day runs.

A domain can be written any way you happen to have it — `mysite.com`,
`www.mysite.com`, `https://mysite.com/path`, `MySite.com.`, `mysite.com:443`
all mean the same host. Unicode domains and their punycode form match each other,
and anything that isn't a usable host (`a..com`, `-bad.com`) is rejected up front
rather than silently matching nothing.

## Try it without touching the network

`examples/demo/` is a small page that exercises anchor text, `rel="nofollow sponsored"`,
subdomain matching and irrelevant links:

```bash
python3 -m http.server 8765 --directory examples/demo & DEMO_PID=$!
python3 backlink_finder.py examples/demo/urls.txt --domains mysite.com
kill $DEMO_PID
```

Output (saved verbatim as `examples/sample_output.csv`):

```
source_url,found_domain,link_url,anchor_text,rel,nofollow,error
http://127.0.0.1:8765/page.html,mysite.com,https://mysite.com/best-guide/,best guide to widgets,,,
http://127.0.0.1:8765/page.html,mysite.com,https://www.mysite.com/deals,check the deals,nofollow sponsored,yes,
```

To try it against live pages instead, `examples/urls.txt` + `examples/domains.txt` scan a
few public pages for links to `iana.org`:

```bash
python3 backlink_finder.py examples/urls.txt --domains-file examples/domains.txt
```

## Output

CSV goes to stdout (or `--out FILE`) **in input order**; the run summary goes to **stderr**,
so piping stays clean. The whole URL list is held in memory and every job is submitted at
once — fine for outreach sheets, not meant for million-line crawls.

| Column | Meaning |
|---|---|
| `source_url` | the URL from your list (redirects are followed, but this column keeps what you asked for, so rows join back to your sheet) |
| `found_domain` | which of your domains was matched (empty = nothing found) |
| `link_url` | absolute URL of the link (relative hrefs resolved against the final page URL, honouring `<base href>`) |
| `anchor_text` | anchor text, whitespace-collapsed, truncated to 200 chars; `<img alt>` is used for image links |
| `rel` | raw `rel` attribute |
| `nofollow` | `yes` when `rel` contains `nofollow` |
| `error` | why the page could not be scanned (empty on success) |

A row with an empty `found_domain` **and** an empty `error` means *scanned fine, nothing found*.

Dofollow links only (`csv`-aware, unlike a bare `grep`):

```bash
python3 backlink_finder.py urls.txt --domains mysite.com --out found.csv
python3 -c "import csv,sys
for r in csv.DictReader(open('found.csv')):
    rels = r['rel'].lower().split()
    if r['link_url'] and not ({'nofollow','sponsored','ugc'} & set(rels)):
        print(r['source_url'], r['link_url'], r['anchor_text'], sep='\t')"
```

The stderr summary looks like this:

```
=== Scan report ====================================================
  URLs scanned                : 120
  Sources with our link(s)    : 83
  Sources with ZERO matches   : 31
  Sources with errors         : 6
  Total link occurrences      : 97
  --- Our domains found ---
    mysite.com     71 link(s)  on   62 source(s)
    othersite.com  26 link(s)  on   21 source(s)
  --- Errors by type ---
    HTTP 404        4
    network         2
=====================================================================
```

## Options

| Flag | Default | Description |
|---|---|---|
| `file` | — | text file with URLs, one per line (`#` comments allowed) |
| `--domains` | — | comma-separated domains to look for |
| `--domains-file` | `./domains.txt` if present | file with one domain per line (`#` comments allowed) |
| `--out` | `-` (stdout) | write CSV to a file instead |
| `--workers` | `8` | parallel HTTP workers (1–64) |
| `--insecure` | off | retry TLS failures with certificate verification **off** |

At least one domain must be supplied via `--domains` or `--domains-file`.

Exit codes: `0` when the scan ran to completion (per-page failures are reported in the
`error` column, not via the exit status), `2` for usage and output problems — no usable
domain, an unreadable input file, an unwritable or failing `--out` path, a bad `--workers`
value. Closing the pipe early (`| head`) exits `0` quietly.

## Behaviour notes

- **Subdomains match.** `mysite.com` also matches `www.mysite.com` and `blog.mysite.com`. If you configure both `mysite.com` and `shop.mysite.com`, the more specific one is reported.
- **Redirects are followed** (to `http(s)` only — a redirect into another scheme is reported as an error), and relative links are resolved against the page that actually answered, via `<base href>` when the page sets one.
- **Deduplicated per source.** The same target URL linked twice with the *same* anchor and `rel` is reported once; a second link with different anchor text is kept.
- **Only `http(s)` links are considered** — `mailto:`, `tel:`, `javascript:`, `#anchor` and other schemes are skipped, on input URLs as well as on extracted links.
- **Nested anchors close like a browser closes them**: an opening `<a>` ends the previous one, so text never leaks from one link into another.
- **Encoding** comes from the BOM (UTF-8/16/32), then the HTTP `charset`, then `<meta charset>` or an `<?xml?>` declaration, then UTF-8, then CP1252.
- **Broken TLS fails by default.** Many small donor sites have expired certificates; `--insecure` retries those with verification off, and the summary counts how many pages were accepted that way. Content fetched this way is not authenticated — treat it accordingly.
- **4 MB defensive size limit.** A response is skipped when the server *declares* more than that in `Content-Length` (an untrusted claim — a lying header will skip a small page) or when the body actually exceeds it. Responses are requested uncompressed (`Accept-Encoding: identity`).
- **Non-HTML responses are skipped**; a missing or generic `Content-Type` is still parsed.
- **Raw HTML only.** Links injected by JavaScript after page load are not seen; the parser is `html.parser`, not a browser.

## Tests

```bash
python3 -m unittest -v
```

Stdlib `unittest` against a local `http.server` — no network, no dependencies.
44 tests covering matching, redirects, `<base>`, encodings, domain canonicalization,
row semantics and CLI exit behaviour.

## Politeness

`--workers 8` is a sane default against a mixed list of hosts. If your list is concentrated on a
single domain, lower it — the tool has no rate limiter and no `robots.txt` handling. Scan pages
you have a legitimate reason to check.

## License

MIT — see [LICENSE](LICENSE).
