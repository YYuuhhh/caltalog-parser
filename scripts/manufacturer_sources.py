"""Resolve manufacturer origins once, then freeze and verify review evidence."""

from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
import system_diagnostics as files

# Manufacturer-controlled origins, with exact-label subdomain matching.
# Unknown brands are resolved only from brand-matching URLs declared in their
# supplied catalog. A search result alone cannot establish an official origin.
DIRECTORY = {
    "guhring": ["https://guehring.com/de-en/", "https://guhring.com/"],
    "promatool": ["https://promarsenal.ru/promatool"],
    "sandvik-coromant": ["https://www.sandvik.coromant.com/"],
    "seco": ["https://www.secotools.com/"],
    "walter": ["https://www.walter-tools.com/"],
    "kennametal": ["https://www.kennametal.com/"],
    "widia": ["https://www.widia.com/"],
    "iscar": ["https://www.iscar.com/"],
    "taegutec": ["https://www.taegutec.com/"],
    "tungaloy": ["https://tungaloy.com/"],
    "mitsubishi-materials": ["https://www.mmc-carbide.com/"],
    "sumitomo-electric": ["https://www.sumitool.com/"],
    "kyocera": ["https://global.kyocera.com/prdct/tool/"],
    "osg": ["https://www.osg.co.jp/en/"],
    "nachi": ["https://www.nachi-fujikoshi.co.jp/eng/"],
    "yamawa": ["https://www.yamawa.com/"],
    "union-tool": ["https://www.uniontool.co.jp/en/"],
    "yg-1": ["https://www.yg1.kr/"],
    "korloy": ["https://www.korloy.com/"],
    "dormer-pramet": ["https://www.dormerpramet.com/"],
    "ceratizit": ["https://www.ceratizit.com/"],
    "mapal": ["https://mapal.com/"],
    "emuge-franken": ["https://www.emuge-franken.com/"],
    "horn": ["https://www.horn-group.com/"],
    "haimer": ["https://www.haimer.com/"],
    "schunk": ["https://schunk.com/"],
    "rego-fix": ["https://www.rego-fix.com/"],
    "big-daishowa": ["https://www.bigdaishowa.com/"],
    "lmt-tools": ["https://www.lmt-tools.com/"],
    "fraisa": ["https://www.fraisa.com/"],
    "mikron-tool": ["https://www.mikrontool.com/"],
    "vargus": ["https://www.vargus.com/"],
    "carmex": ["https://www.carmex.com/"],
    "allied-machine": ["https://www.alliedmachine.com/"],
    "harvey-tool": ["https://www.harveytool.com/"],
    "helical-solutions": ["https://www.helicaltool.com/"],
    "ingersoll": ["https://www.ingersoll-imc.com/"],
    "m-a-ford": ["https://www.maford.com/"],
    "sgs": ["https://www.kyocera-sgstool.com/"],
    "ruko": ["https://ruko.de/en/"],
    "dijet": ["https://www.dijet.co.jp/"],
}
VERSION = 1
EXCLUDED = re.compile(
    r"privacy|impressum|legal|cookie|transparency|conflict.?mineral|compliance|terms.of|supply.chains|datenschutz|career|jobs",
    re.I,
)


def link_rank(url, terms):
    """Prefer catalog-specific technical content, not whichever PDF sorts first."""
    path = urlsplit(url).path.lower()
    hits = sum(term in path for term in terms)
    catalog = any(
        word in path for word in ("catalog", "katalog", "download", "brochure")
    )
    pdf = path.endswith(".pdf")
    return (-(3 * hits + 4 * catalog + pdf), not catalog, not pdf, url)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def canonical(url):
    value = urlsplit(url.strip())
    if (
        value.scheme not in ("http", "https")
        or not value.hostname
        or value.username
        or value.password
        or value.port not in (None, 443, 80)
    ):
        raise ValueError(
            "Manufacturer sources need public HTTPS URLs without credentials."
        )
    return urlunsplit(
        (
            "https",
            value.hostname.lower().encode("idna").decode(),
            value.path or "/",
            value.query,
            "",
        )
    )


def allowed(url, domains):
    try:
        host = urlsplit(canonical(url)).hostname
    except (ValueError, UnicodeError):
        return False
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def domains(urls):
    return sorted(
        {urlsplit(canonical(url)).hostname.removeprefix("www.") for url in urls}
    )


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.links = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
        if tag == "a":
            self.links.extend(v for k, v in attrs if k == "href" and v)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.text.append(data.strip())


def fetch(url, origins):
    """Bound public fetches, including every redirect, to the chosen authority."""
    with requests.Session() as client:
        client.trust_env = False
        for _ in range(5):
            url = canonical(url)
            if not allowed(url, origins):
                raise ValueError("Redirect left the manufacturer's official domains.")
            addresses = socket.getaddrinfo(
                urlsplit(url).hostname, 443, type=socket.SOCK_STREAM
            )
            if not addresses or any(
                not ipaddress.ip_address(row[4][0]).is_global for row in addresses
            ):
                raise ValueError(
                    "Manufacturer sources must resolve to public Internet addresses."
                )
            with client.get(
                url, timeout=(5, 12), stream=True, allow_redirects=False
            ) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                response.raise_for_status()
                kind = response.headers.get("Content-Type", "").lower()
                if not any(word in kind for word in ("html", "pdf")):
                    raise ValueError("Unsupported official source format.")
                body = bytearray()
                limit = 16 * 1024**2 if "pdf" in kind else 1024**2
                for part in response.iter_content(65536):
                    body.extend(part)
                    if len(body) > limit:
                        raise ValueError(
                            "Official source exceeds the bounded download limit."
                        )
                links = []
                if "pdf" in kind:
                    import fitz

                    with fitz.open(stream=bytes(body), filetype="pdf") as pdf:
                        text = "\n".join(
                            pdf[i].get_text() for i in range(min(64, len(pdf)))
                        )
                else:
                    page = Page()
                    page.feed(
                        bytes(body).decode(
                            response.encoding or "utf-8", errors="replace"
                        )
                    )
                    text = "\n".join(page.text)
                    links = [urljoin(url, link) for link in page.links]
                return {
                    "url": url,
                    "text": text,
                    "content_sha256": hashlib.sha256(body).hexdigest(),
                    "content_type": kind,
                    "body": bytes(body),
                    "links": links,
                }
        raise ValueError("Too many official-site redirects.")


def catalog_urls(source, manifest=None):
    import fitz

    found = set()
    texts = []
    with fitz.open(source) as pdf:
        pages = sorted(
            set(range(min(4, len(pdf)))) | set(range(max(0, len(pdf) - 4), len(pdf)))
        )
        for i in pages:
            texts.append(pdf[i].get_text())
            found.update(
                link["uri"]
                for link in pdf[i].get_links()
                if link.get("uri", "").startswith(("https://", "http://"))
            )
    if manifest:
        import yaml
        import extraction_quality as quality

        inventory = yaml.safe_load(Path(manifest).read_text())
        source_sha = hashlib.sha256(Path(source).read_bytes()).hexdigest()
        for state in inventory.get("page_states", []):
            if (
                state.get("source_sha256") == source_sha
                and state.get("status") == "parsed"
            ):
                content, _ = quality.artifacts(state)
                for block in quality.walk(content):
                    texts.extend(
                        block[key]
                        for key in ("text", "table_body")
                        if isinstance(block.get(key), str)
                    )
    for text in texts:
        found.update(
            re.findall(r"https?://[^\s<>\"')]+|www\.[\w.-]+(?:/[^\s<>\"')]+)?", text)
        )
        found.update(
            "https://" + host
            for host in re.findall(r"[\w.+-]+@([\w.-]+\.[a-zA-Z]{2,})", text)
        )
    result = []
    for item in sorted(found):
        try:
            result.append(
                canonical(
                    item.rstrip(".,;")
                    if "://" in item
                    else "https://" + item.rstrip(".,;")
                )
            )
        except ValueError:
            pass
    return sorted(set(result))


def verify(snapshot, root):
    value = {k: v for k, v in snapshot.items() if k != "snapshot_sha256"}
    if digest(value) != snapshot.get("snapshot_sha256"):
        raise ValueError("Official source snapshot was modified.")
    for item in snapshot["evidence"]:
        path = Path(root) / item["body_file"]
        if (
            not path.resolve().is_relative_to(Path(root).resolve())
            or hashlib.sha256(path.read_bytes()).hexdigest() != item["content_sha256"]
        ):
            raise ValueError("Official source snapshot body is missing or modified.")
        if not allowed(item["url"], snapshot["official_domains"]):
            raise ValueError("Snapshot contains a non-manufacturer source.")
    return snapshot


def prepare(
    source,
    manufacturer_id,
    manufacturer,
    destination,
    extra_urls="",
    search_url="",
    manifest=None,
):
    destination = Path(destination)
    identity = {
        "version": VERSION,
        "manufacturer_id": manufacturer_id,
        "source_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest(),
        "extra_urls": extra_urls.split(),
        "search_url": search_url,
    }
    if destination.exists():
        saved = verify(files.read_json(destination), destination.parent)
        if saved["identity"] != identity:
            raise ValueError(
                "Official source selection changed; start a new run to obtain a new snapshot."
            )
        return saved
    declared = catalog_urls(source, manifest)
    roots = DIRECTORY.get(manufacturer_id, [])
    basis = "manufacturer_directory"
    if not roots:
        token = re.sub(r"[^a-z0-9]", "", manufacturer_id.lower())
        roots = sorted(
            {
                "https://" + urlsplit(url).hostname + "/"
                for url in declared
                if len(token) >= 4
                and token in re.sub(r"[^a-z0-9]", "", urlsplit(url).hostname.lower())
            }
        )
        basis = "catalog_declared_brand_domain"
    if not roots and extra_urls.strip():
        roots = sorted({canonical(url) for url in extra_urls.split()})
        basis = "explicit_operator_sources"
    origins = domains(roots)
    terms = {
        term
        for term in re.findall(
            r"[a-z0-9]{3,}", re.sub(r"^[0-9a-f]{12}-", "", Path(source).stem.lower())
        )
        if not term.isdigit()
        and term
        not in {"catalog", "catalogue", "cutters", "guhring", "guehring", "tools"}
    }
    queue = sorted(
        {canonical(u) for u in extra_urls.split() if allowed(u, origins)}
    ) + list(roots)
    queue += sorted(u for u in declared if allowed(u, origins) and u not in queue)
    errors = []
    evidence = []
    visited = set()
    started = time.monotonic()
    # Optional search is discovery only. Search snippets never become evidence.
    if search_url and origins:
        try:
            import local_review

            with local_review.session(search_url) as client:
                data = local_review.checked(
                    client.get(
                        search_url.rstrip("/") + "/search",
                        params={
                            "q": f"site:{origins[0]} {manufacturer} {Path(source).stem} catalog",
                            "format": "json",
                        },
                        timeout=10,
                        allow_redirects=False,
                    )
                )
            queue += sorted(
                {
                    canonical(row["url"])
                    for row in data.get("results", [])
                    if allowed(row.get("url", ""), origins)
                }
            )
        except Exception as exc:
            errors.append({"url": search_url, "error": str(exc)[:300]})
    while queue and len(visited) < 10 and time.monotonic() - started < 120:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            print("Official source: " + url, flush=True)
            item = fetch(url, origins)
            if not item["text"].strip():
                raise ValueError(
                    "Official source has no readable text; it cannot support approval."
                )
            name = "official-source-bodies/" + item["content_sha256"] + ".bin"
            target = destination.parent / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.pop("body"))
            links = item.pop("links")
            item.update(
                body_file=name,
                sha256=hashlib.sha256(item["text"].encode()).hexdigest(),
                fetched_at=time.time(),
            )
            evidence.append(item)
            ranked = sorted(
                {
                    canonical(link)
                    for link in links
                    if allowed(link, origins)
                    and not EXCLUDED.search(urlsplit(link).path)
                    and any(
                        word in link.lower()
                        for word in (
                            "catalog",
                            "download",
                            "milling",
                            "coating",
                            "product",
                            ".pdf",
                        )
                    )
                },
                key=lambda u: link_rank(u, terms),
            )
            queue.extend(
                link for link in ranked[:6] if link not in visited and link not in queue
            )
            queue.sort(key=lambda u: link_rank(u, terms))
        except Exception as exc:
            errors.append({"url": url, "error": str(exc)[:300]})
    snapshot = {
        "identity": identity,
        "manufacturer": manufacturer,
        "official_domains": origins,
        "official_urls": roots,
        "resolution_basis": basis,
        "status": "ready" if evidence else "unavailable",
        "evidence": evidence,
        "errors": errors,
        "created_at": time.time(),
        "reason": ""
        if evidence
        else "No official manufacturer evidence could be fetched; Internet checks must remain unverified.",
    }
    snapshot["snapshot_sha256"] = digest(snapshot)
    files.atomic(destination, snapshot)
    return snapshot


def packet(snapshot, context, include_text=True):
    result = {
        "manufacturer": snapshot["manufacturer"],
        "official_domains": snapshot["official_domains"],
        "official_urls": snapshot["official_urls"],
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "status": snapshot["status"],
        "evidence": [],
    }
    if not include_text:
        return result
    terms = set(
        re.findall(r"[\w-]{3,}", json.dumps(context, ensure_ascii=False).lower())
    )
    ranked = []
    for item in snapshot["evidence"]:
        lines = item["text"].splitlines()
        best = sorted(
            range(len(lines)),
            key=lambda i: (
                -len(set(re.findall(r"[\w-]{3,}", lines[i].lower())) & terms),
                i,
            ),
        )[:8]
        chosen = set()
        for index in best:
            for neighbor in (index - 1, index, index + 1):
                if (
                    0 <= neighbor < len(lines)
                    and len(
                        "\n".join(
                            lines[i] for i in sorted(chosen | {neighbor})
                        ).encode()
                    )
                    <= 1000
                ):
                    chosen.add(neighbor)
        text = "\n".join(lines[i] for i in sorted(chosen))
        if not text.strip():
            continue
        score = len(set(re.findall(r"[\w-]{3,}", text.lower())) & terms)
        ranked.append(
            (
                score,
                item["url"],
                {
                    **{
                        k: item[k]
                        for k in ("url", "sha256", "content_sha256", "fetched_at")
                    },
                    "text": text,
                    "excerpt": True,
                },
            )
        )
    result["evidence"] = [
        row[2] for row in sorted(ranked, key=lambda r: (-r[0], r[1]))[:3]
    ]
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manufacturer-id", required=True)
    parser.add_argument("--manufacturer", required=True)
    parser.add_argument("--extra-urls", default="")
    parser.add_argument("--search-url", default="")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    value = prepare(
        args.source,
        args.manufacturer_id,
        args.manufacturer,
        args.output,
        args.extra_urls,
        args.search_url,
        args.manifest,
    )
    print(
        json.dumps(
            {key: value[key] for key in ("status", "official_urls", "snapshot_sha256")},
            ensure_ascii=False,
        )
    )
