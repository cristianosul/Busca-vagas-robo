#!/usr/bin/env python3
"""Diagnóstico de acesso municipal RS; uma consulta por município, sem classificar vaga por palavra isolada."""

import json
import re
import time
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

TZ_POA = ZoneInfo("America/Sao_Paulo")
HEADERS = {"User-Agent": "BuscaVagasBot/0.2 (+public municipal source validation)",
           "Accept-Language": "pt-BR,pt;q=0.9"}
TARGETS = [
    ("Caxias do Sul", "https://caxias.rs.gov.br/"),
    ("Novo Hamburgo", "https://www.novohamburgo.rs.gov.br/"),
    ("Bento Gonçalves", "https://bentogoncalves.atende.net/"),
    ("Canoas", "https://canoas.rs.gov.br/"),
    ("Pelotas", "https://www.pelotas.com.br/"),
]
TERMS = re.compile(r"\b(vagas?|empregos?|balc[aã]o de empregos|banco de oportunidades|sine|concurso|edital|processo seletivo)\b", re.I)


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def check_one(client, municipality, url):
    item = {"municipality": municipality, "url": url, "status": "not_checked",
            "candidate_links": []}
    try:
        response = client.get(url)
        item.update(http_status=response.status_code, final_url=str(response.url),
                    response_bytes=len(response.content))
        parsed = urlparse(str(response.url))
        if parsed.scheme != "https" or not parsed.hostname:
            item["status"] = "unsafe_redirect"
            return item
        if response.status_code != 200:
            item["status"] = f"http_{response.status_code}"
            return item
        soup = BeautifulSoup(response.text, "html.parser")
        item["page_title"] = clean(soup.title.get_text(" ", strip=True))[:160] if soup.title else ""
        for node in soup(["script", "style", "nav", "footer", "header", "form"]):
            node.decompose()
        seen = set()
        for anchor in soup.find_all("a", href=True):
            label = clean(anchor.get_text(" ", strip=True))
            href = urljoin(str(response.url), anchor["href"].strip())
            parsed_link = urlparse(href)
            if href in seen or parsed_link.scheme != "https" or not parsed_link.hostname:
                continue
            seen.add(href)
            if TERMS.search(f"{label} {href}"):
                item["candidate_links"].append({"label": label[:120], "url": href})
        item["candidate_links"] = item["candidate_links"][:15]
        item["status"] = "ok"
        item["warning"] = "Links candidatos exigem abertura e validação manual; não são vagas confirmadas."
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        item.update(status="connection_error", error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        item.update(status="processing_error", error=f"{type(exc).__name__}: {exc}")
    return item


def main():
    results = []
    with httpx.Client(headers=HEADERS, timeout=httpx.Timeout(18.0, connect=8.0),
                      follow_redirects=True) as client:
        for index, (municipality, url) in enumerate(TARGETS):
            result = check_one(client, municipality, url)
            results.append(result)
            print(f"{municipality}: {result['status']} | HTTP {result.get('http_status', '-')}"
                  f" | links candidatos: {len(result['candidate_links'])}")
            if index < len(TARGETS) - 1:
                time.sleep(1.0)
    payload = {"generated_at_poa": datetime.now(TZ_POA).isoformat(),
               "municipalities": results,
               "summary": {"tested": len(results),
                           "http_200": sum(x.get("http_status") == 200 for x in results),
                           "connection_errors": sum(x["status"] == "connection_error" for x in results)}}
    Path("prefeituras_rs.txt").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Relatório salvo em prefeituras_rs.txt")


if __name__ == "__main__":
    main()
