"""POST /api/generate_results and GET /api/parse_index."""

import json

import pytest

from conftest import call, make_request, page_texts

import function_app as fa
from scoremodifier import index_meta
from scoremodifier.extract import extract_results

INDEX_HTML = """<html><head><title>Test Cup 2026</title></head><body>
<table><tr><td class="caption3">Pori / PML</td></tr>
<tr><td class="caption3">Isomäen Jäähalli, Pori</td></tr>
<tr><td class="caption3">19.09.2026</td></tr></table>
<table>
<tr><td class="CellLeft"><a href="CAT001RS.htm">Tulokkaat L1</a></td><td>Final</td></tr>
<tr><td class="CellLeft"><a href="CAT002RS.htm">SM-Noviisit</a></td><td>Final</td></tr>
<tr><td class="CellLeft"><a href="CAT003RS.htm">Noviisit L1</a></td><td>Final</td></tr>
</table></body></html>"""


def _run(pdf, params=None):
    resp = call(fa.generate_results, make_request("generate_results", body=pdf, params=params or {}))
    return resp, (json.loads(resp.get_body()) if resp.status_code == 200 else None)


def test_input_guards(synthetic_pdf):
    assert _run(b"")[0].status_code == 400
    assert _run(b"not a pdf")[0].status_code == 400
    resp, _ = _run(synthetic_pdf)
    assert resp.status_code == 400
    assert b"JUDGES DETAILS PER SKATER" in resp.get_body()


def test_results_without_index(team_sample_pdf, storage):
    sample_pdf = team_sample_pdf
    teams, segment = extract_results(sample_pdf)
    resp, body = _run(sample_pdf, {"competition": "Test Cup", "date": "19.09.2026", "venue": "Pori"})
    assert resp.status_code == 200, resp.get_body()
    assert set(body) == {"id", "name", "fileName", "downloadUrl", "htmlFileName", "htmlUrl", "pages", "expiration"}

    row = storage.competitions[("GLOBAL", body["id"])]
    assert row["Tool"] == "results"
    assert row["Competition"] == "Test Cup" and row["Venue"] == "Pori" and row["CompetitionDate"] == "19.09.2026"
    folder = row["FolderPath"]
    # No category and no catFile -> generic names; category badge falls back to the segment.
    assert body["htmlFileName"] == "results.htm"
    assert body["fileName"] == fa.RESULTS_PDF_FILENAME
    assert row["CatFile"] == "results.htm"
    pdf = storage.blobs[f"{folder}/output/{fa.RESULTS_PDF_FILENAME}"]
    html = storage.blobs[f"{folder}/output/results.htm"].decode("utf-8")
    assert storage.blobs[f"{folder}/source.pdf"] == sample_pdf

    texts = page_texts(pdf)
    assert len(texts) == 1 and body["pages"] == 1
    text = texts[0]
    assert "Test Cup" in text and "Pori" in text
    for t in teams:
        assert t.name in text
    # Podium only in the native HTML page, with nation flags.
    for t in teams:
        if t.rank <= 3:
            assert t.name in html
            assert f"../flags/{t.club.upper()}.GIF" in html
        else:
            assert t.name not in html
    assert segment in html  # caption falls back to the segment line

    # Both files get SAS rows.
    assert {rk for (pk, rk) in storage.papers if pk == body["id"]} == {fa.RESULTS_PDF_FILENAME, "results.htm"}
    assert body["downloadUrl"] == storage.papers[(body["id"], fa.RESULTS_PDF_FILENAME)]["Url"]
    assert body["htmlUrl"] == storage.papers[(body["id"], "results.htm")]["Url"]


def test_tulokkaat_reuses_fsm_export_filename_and_cat_file(team_sample_pdf, storage):
    resp, body = _run(team_sample_pdf, {"category": "Tulokkaat", "catFile": "CAT003RS.htm"})
    assert resp.status_code == 200
    assert body["fileName"] == fa.TULOKKAAT_PDF_FILENAME
    assert body["htmlFileName"] == "CAT003RS.htm"
    folder = storage.competitions[("GLOBAL", body["id"])]["FolderPath"]
    assert f"{folder}/output/{fa.TULOKKAAT_PDF_FILENAME}" in storage.blobs
    assert f"{folder}/output/CAT003RS.htm" in storage.blobs


def test_pdf_filename_override_is_sanitised(team_sample_pdf, storage):
    resp, body = _run(team_sample_pdf, {"pdfFile": "../../evil/name<>.PDF"})
    assert resp.status_code == 200
    assert body["fileName"] == "name.PDF"
    resp, body = _run(team_sample_pdf, {"pdfFile": "///"})
    assert body["fileName"] == fa.RESULTS_PDF_FILENAME


def test_index_url_autofills_and_names_the_cat_page(team_sample_pdf, storage, monkeypatch):
    sample_pdf = team_sample_pdf
    seen = {}

    def fake_fetch(url, *, allowed_hosts=None, timeout=10.0):
        seen["url"], seen["hosts"] = url, allowed_hosts
        return INDEX_HTML

    monkeypatch.setattr(fa, "fetch_index_html", fake_fetch)
    teams, segment = extract_results(sample_pdf)
    url = "https://www.figureskatingresults.fi/results/2526/TEST/index.htm"
    resp, body = _run(sample_pdf, {"indexUrl": url})
    assert resp.status_code == 200, resp.get_body()
    assert seen == {"url": url, "hosts": fa.INDEX_ALLOWED_HOSTS}

    idx = index_meta.parse_index_html(INDEX_HTML)
    matched = index_meta.match_category(idx, segment)
    assert matched is not None
    assert body["htmlFileName"] == matched.cat_file
    row = storage.competitions[("GLOBAL", body["id"])]
    assert row["Competition"] == "Test Cup 2026"
    html = storage.blobs[f"{row['FolderPath']}/output/{matched.cat_file}"].decode()
    assert f"<title>Test Cup 2026 - {matched.name}</title>" in html
    # Long values wrap inside their info-bar cell, so compare whitespace-normalised text.
    pdf_text = " ".join("\n".join(page_texts(storage.blobs[f"{row['FolderPath']}/output/{body['fileName']}"])).split())
    assert "Test Cup 2026" in pdf_text and "Isomäen Jäähalli, Pori" in pdf_text and "19.09.2026" in pdf_text


def test_index_url_failure_degrades_to_operator_fields(team_sample_pdf, monkeypatch):
    def boom(url, *, allowed_hosts=None, timeout=10.0):
        raise ValueError("Host not allowed")

    monkeypatch.setattr(fa, "fetch_index_html", boom)
    resp, body = _run(team_sample_pdf, {"indexUrl": "https://evil.example/index.htm", "competition": "Manual"})
    assert resp.status_code == 200
    assert body["htmlFileName"] == "results.htm"


def test_parse_index_endpoint(monkeypatch):
    resp = call(fa.parse_index, make_request("parse_index", method="GET"))
    assert resp.status_code == 400 and b"Missing url" in resp.get_body()

    # SSRF guard: real fetch, host outside INDEX_ALLOWED_HOSTS -> 400 before any network I/O.
    resp = call(fa.parse_index, make_request("parse_index", method="GET", params={"url": "https://169.254.169.254/latest/meta-data"}))
    assert resp.status_code == 400
    assert b"Host not allowed" in resp.get_body()
    resp = call(fa.parse_index, make_request("parse_index", method="GET", params={"url": "file:///etc/passwd"}))
    assert resp.status_code == 400

    monkeypatch.setattr(fa, "fetch_index_html", lambda url, *, allowed_hosts=None, timeout=10.0: INDEX_HTML)
    resp = call(fa.parse_index, make_request("parse_index", method="GET", params={"url": "https://www.figureskatingresults.fi/results/2526/X/index.htm"}))
    assert resp.status_code == 200
    body = json.loads(resp.get_body())
    assert body["competition"] == "Test Cup 2026"
    assert body["date"] == "19.09.2026"
    assert body["venue"] == "Isomäen Jäähalli, Pori"
    assert [c["catFile"] for c in body["categories"]] == ["CAT001RS.htm", "CAT002RS.htm", "CAT003RS.htm"]
    assert body["categories"][0]["name"] == "Tulokkaat L1"


@pytest.mark.parametrize("bad", ["", "results", "a/b\\c"])
def test_safe_pdf_filename(bad):
    out = fa._safe_pdf_filename(bad, "fallback.pdf")
    assert out.lower().endswith(".pdf") and "/" not in out and "\\" not in out
