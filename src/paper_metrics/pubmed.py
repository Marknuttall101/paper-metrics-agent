"""Search PubMed and fetch abstracts (NCBI E-utilities, no API key needed).

This module never calls a model. It only gets papers for the agents to read.

Examples
  paper-metrics pubmed search --author "Smith J" --max 8 --out output/papers.json
  paper-metrics pubmed fetch 39012345 38999999 --out output/papers.json
  paper-metrics pubmed search --author "Smith J" --term "heart failure" --max 5

Output: a JSON list of {pmid, title, journal, year, authors, abstract}.
Papers without an abstract are kept but flagged with "abstract": "".

Optional environment variables
  NCBI_API_KEY  raises NCBI's limit from 3 to 10 requests a second
  NCBI_EMAIL    NCBI asks heavy users to identify themselves
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from ._console import utf8_output

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
TOOL = "paper-metrics-agent"


def _get(endpoint: str, params: dict) -> bytes:
    params = dict(params, tool=TOOL)
    if os.environ.get("NCBI_API_KEY"):
        params["api_key"] = os.environ["NCBI_API_KEY"]
    if os.environ.get("NCBI_EMAIL"):
        params["email"] = os.environ["NCBI_EMAIL"]
    url = BASE + endpoint + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": TOOL})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    time.sleep(0.12 if os.environ.get("NCBI_API_KEY") else 0.35)  # stay under NCBI limits
    return data


def search(author: str, term: str | None = None, max_results: int = 10) -> list[str]:
    query = f"{author}[au]"
    if term:
        query += f" AND ({term})"
    raw = _get("esearch.fcgi", {"db": "pubmed", "term": query, "retmax": max_results,
                                "retmode": "json", "sort": "pub_date"})
    return json.loads(raw)["esearchresult"]["idlist"]


def parse_pubmed_xml(xml_bytes: bytes) -> list[dict]:
    """Turn an efetch XML response into simple paper records."""
    root = ET.fromstring(xml_bytes)
    papers = []
    for art in root.iter("PubmedArticle"):
        pmid = art.findtext(".//MedlineCitation/PMID", default="")
        article = art.find(".//MedlineCitation/Article")
        if article is None:
            continue
        title = "".join(article.find("ArticleTitle").itertext()) if article.find("ArticleTitle") is not None else ""
        journal = article.findtext("Journal/Title", default="")
        year = (article.findtext("Journal/JournalIssue/PubDate/Year")
                or (article.findtext("Journal/JournalIssue/PubDate/MedlineDate") or "")[:4])
        parts = []
        for node in article.findall("Abstract/AbstractText"):
            text = "".join(node.itertext()).strip()
            label = node.get("Label")
            parts.append(f"{label}: {text}" if label else text)
        authors = []
        for a in article.findall("AuthorList/Author"):
            last, initials = a.findtext("LastName"), a.findtext("Initials")
            if last:
                authors.append(f"{last} {initials or ''}".strip())
            elif a.findtext("CollectiveName"):
                authors.append(a.findtext("CollectiveName"))
        papers.append({"pmid": pmid, "title": title.strip(), "journal": journal,
                       "year": year, "authors": authors, "abstract": " ".join(parts)})
    return papers


def fetch(pmids: list[str]) -> list[dict]:
    papers = []
    for i in range(0, len(pmids), 100):
        chunk = pmids[i:i + 100]
        raw = _get("efetch.fcgi", {"db": "pubmed", "id": ",".join(chunk), "retmode": "xml"})
        papers.extend(parse_pubmed_xml(raw))
    return papers


class PubMedError(RuntimeError):
    """PubMed could not be reached or returned nothing usable."""


def search_and_fetch(author: str, term: str | None = None, max_results: int = 10) -> list[dict]:
    """An author's most recent papers with abstracts. Raises PubMedError on failure or no results."""
    try:
        ids = search(author, term, max_results)
        if not ids:
            raise PubMedError(f"No PubMed results for {author!r}. Try 'Lastname Initials', e.g. 'Smith JA'.")
        return fetch(ids)
    except (OSError, ET.ParseError, KeyError, ValueError) as e:  # network, HTTP, malformed responses
        raise PubMedError(f"PubMed request failed: {e}") from e


def _write(papers: list[dict], out: str | None) -> None:
    text = json.dumps(papers, indent=2, ensure_ascii=False)
    if out:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        missing = sum(1 for p in papers if not p["abstract"])
        print(f"Saved {len(papers)} papers to {out} ({missing} without an abstract)")
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    ap = argparse.ArgumentParser(prog="paper-metrics pubmed", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="find an author's papers and fetch their abstracts")
    s.add_argument("--author", required=True, help='PubMed author name, e.g. "Smith J"')
    s.add_argument("--term", help="optional extra search terms")
    s.add_argument("--max", type=int, default=10)
    s.add_argument("--out")
    f = sub.add_parser("fetch", help="fetch abstracts for known PMIDs")
    f.add_argument("pmids", nargs="+")
    f.add_argument("--out")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "search":
            papers = search_and_fetch(args.author, args.term, args.max)
        else:
            papers = fetch(args.pmids)
    except PubMedError as e:
        print(e, file=sys.stderr)
        return 1
    except OSError as e:  # network errors, timeouts, HTTP errors
        print(f"PubMed request failed: {e}. Check your connection and try again.", file=sys.stderr)
        return 2
    _write(papers, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
