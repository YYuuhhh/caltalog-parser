"""Manufacturer authority and evidence stay fixed across tasks and resumes."""

import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import fitz
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import manufacturer_sources as sources
import local_review
import subscription_review as review


def document(tmp_path, text="www.exampletools.com"):
    path = tmp_path / "catalog.pdf"
    with fitz.open() as pdf:
        pdf.new_page().insert_text((40, 60), text)
        pdf.save(path)
    return path


def fetched(url, text="Article 00123\nd1 = 1,25 mm\nTiAlN coating", links=()):
    body = ("<html>" + text + "</html>").encode()
    return dict(
        url=url,
        text=text,
        body=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        content_type="text/html",
        links=list(links),
    )


def test_authority_is_automatic_domain_restricted_and_frozen_on_resume(
    tmp_path, monkeypatch
):
    source = document(tmp_path)
    calls = []

    def fetch(url, domains):
        assert sources.allowed(url, domains)
        calls.append(url)
        return fetched(
            url,
            links=[
                "https://guehring.com/catalog/one",
                "https://guehring.com/tech/ConflictMinerals.pdf",
                "https://dealer.example/catalog",
                "https://guehring.com.evil.example/product",
            ],
        )

    monkeypatch.setattr(sources, "fetch", fetch)
    path = tmp_path / "official-sources.json"
    snapshot = sources.prepare(
        source, "guhring", "Gühring", path, extra_urls="https://dealer.example/product"
    )
    assert snapshot["resolution_basis"] == "manufacturer_directory"
    assert snapshot["status"] == "ready"
    assert snapshot["official_domains"] == ["guehring.com", "guhring.com"]
    assert len(calls) == 3
    first_bytes = path.read_bytes()
    monkeypatch.setattr(
        sources, "fetch", lambda *args: pytest.fail("Resume fetched new evidence")
    )
    assert (
        sources.prepare(
            source,
            "guhring",
            "Gühring",
            path,
            extra_urls="https://dealer.example/product",
        )
        == snapshot
    )
    assert path.read_bytes() == first_bytes
    packet = sources.packet(snapshot, {"article_number": "00123", "d1": "1,25"})
    assert packet == sources.packet(snapshot, {"article_number": "00123", "d1": "1,25"})
    assert all("1,25 mm" in item["text"] for item in packet["evidence"])
    assert (
        local_review.sources(
            {"stage": "internet", "context": {"official_sources": packet}}, {}
        )
        == packet["evidence"]
    )
    with pytest.raises(ValueError, match="selection changed"):
        sources.prepare(
            source, "guhring", "Gühring", path, extra_urls="https://guhring.com/new"
        )
    body = tmp_path / snapshot["evidence"][0]["body_file"]
    body.write_bytes(b"changed")
    with pytest.raises(ValueError, match="modified"):
        sources.verify(snapshot, tmp_path)


def test_unknown_manufacturer_uses_ocr_catalog_domain_without_web_guessing(
    tmp_path, monkeypatch
):
    source = document(tmp_path, "Scanned cover without native URLs")
    artifact = tmp_path / "page/files"
    artifact.mkdir(parents=True)
    (artifact / "catalog_content_list.json").write_text(
        json.dumps(
            [
                {
                    "type": "text",
                    "text": "Contact www.exampletools.com or info@exampletools.com",
                }
            ]
        )
    )
    manifest = tmp_path / "catalog.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "page_states": [
                    {
                        "status": "parsed",
                        "source_sha256": hashlib.sha256(
                            source.read_bytes()
                        ).hexdigest(),
                        "artifact_path": str(artifact.parent / "manifest.json"),
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(sources, "fetch", lambda url, domains: fetched(url))
    snapshot = sources.prepare(
        source,
        "exampletools",
        "ExampleTools",
        tmp_path / "sources.json",
        manifest=manifest,
    )
    assert snapshot["resolution_basis"] == "catalog_declared_brand_domain"
    assert snapshot["official_domains"] == ["exampletools.com"]
    monkeypatch.setattr(
        sources,
        "fetch",
        lambda *a: pytest.fail("Guessed a source for an unknown brand"),
    )
    unresolved = sources.prepare(
        source, "unknown-brand", "Unknown", tmp_path / "unknown.json"
    )
    assert unresolved["status"] == "unavailable" and not unresolved["evidence"]


def test_official_failure_or_empty_page_is_explicit_and_cannot_approve(
    tmp_path, monkeypatch
):
    source = document(tmp_path)
    monkeypatch.setattr(sources, "fetch", lambda url, domains: fetched(url, text=""))
    snapshot = sources.prepare(source, "guhring", "Gühring", tmp_path / "sources.json")
    assert snapshot["status"] == "unavailable" and len(snapshot["errors"]) == 2
    from test_pipeline_ui import task, result

    request = task(tmp_path, "internet")
    request["context"]["official_sources"] = sources.packet(snapshot, {})
    with pytest.raises(ValueError, match="frozen official"):
        review.validate_result(request, result(request))


def test_official_fetch_rejects_off_domain_redirect_before_request(monkeypatch):
    response = Mock(
        is_redirect=True, headers={"Location": "https://dealer.example/catalog"}
    )
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    client = Mock(trust_env=True)
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.get.return_value = response
    monkeypatch.setattr(sources.requests, "Session", lambda: client)
    monkeypatch.setattr(
        sources.socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("8.8.8.8", 443))],
    )
    with pytest.raises(ValueError, match="official domains"):
        sources.fetch("https://guehring.com/catalog", ["guehring.com"])
    assert client.get.call_count == 1 and client.trust_env is False


def test_catalog_content_ranks_ahead_of_unrelated_pdfs():
    urls = [
        "https://guhring.com/tech/general.pdf",
        "https://guhring.com/milling/catalog.pdf",
        "https://guhring.com/products/milling/",
        "https://guhring.com/downloads/",
    ]
    assert sorted(urls, key=lambda u: sources.link_rank(u, {"milling"}))[0] == urls[1]
    assert sources.link_rank(urls[3], {"milling"}) < sources.link_rank(
        "https://guhring.com/milling-success-story/", {"milling"}
    )
