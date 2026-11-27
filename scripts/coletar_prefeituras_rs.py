#!/usr/bin/env python3
"""Teste municipal conservador. Só registra links oficiais verificáveis; não grava no banco."""

import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

TZ_POA = ZoneInfo("America/Sao_Paulo")
HEADERS = {"User-Agent": "BuscaVagasBot/0.2 (+public municipal job research)",
           "Accept-Language": "pt-BR,pt;q=0.9"}
# Páginas-base oficiais conhecidas. Não são declaradas como páginas de vagas confirmadas.
TARGETS = [
    ("Caxias do Sul", "portal_municipal", os.getenv("URL_CAXIAS_FGTAS_SINE", "https://caxias.rs.gov.br/")),
    ("Novo Hamburgo", "portal_municipal", os.getenv("URL_NOVO_HAMBURGO_FGTAS_SINE", "https://www.novohamburgo.rs.gov.br/")),
    ("Bento Gonçalves", "portal_municipal", os.getenv("URL_BENTO_GONCALVES", "https://bentogoncalves.atende.net/")),
    ("Canoas", "portal_municipal", os.getenv("URL_CANOAS_BANCO", "https://canoas.rs.gov.br/")),
    ("Pelotas", "portal_municipal", os.getenv("URL_PELOTAS_OPORTUNIDADES", "https://www.pelotas.com.br/")),
]
JOB_TERMS = re.compile(r"\b(vagas?|empregos?|balc[aã]o de empregos|banco de oportunidades|sine|oportunidades de trabalho)\b", re.I)
PUBLIC_TERMS = re.compile(r"\b(concurso|edital|processo seletivo|sele[cç][aã]o p[uú]blica|est[aá]gio)\b", re.I)
PLACEHOLDER_TERMS = ("COLE_A_URL", "example.com", "/...")


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def valid_official_url(url):
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.hostname) and not any(x.lower() in url.lower() for x in PLACEHOLDER_TERMS)


def inspect_target(client, municipality, category, url):
    result = {"municipality": municipality, "target_type": category, "source_url": url,
              "status": "not_checked", "individual_opportunities": [],
              "public_selections": [], "is_confirmed_job_board": False}
    if not valid_official_url(url):
        result.update(status="rejected_placeholder_or_invalid_url",
                      error="URL provisória ou inválida; nenhum acesso realizado.")
        return result
    try:
        response = client.get(url)
        result.update(http_status=response.status_code, final_url=str(response.url))
        if not valid_official_url(str(response.url)):
            result.update(status="rejected_redirect_host", error="Redirecionamento para URL não HTTPS ou inválida.")
            return result
        if response.status_code != 200:
            result["status"] = f"http_{response.status_code}"
            return result
        soup = BeautifulSoup(response.text, "html.parser")
        title = clean(soup.title.get_text(" ", strip=True)) if soup.title else ""
        for node in soup(["script", "style", "nav", "footer", "header", "form"]):
            node.decompose()
        result["page_title"] = title[:180]
        seen = set()
        for anchor in soup.find_all("a", href=True):
            label = clean(anchor.get_text(" ", strip=True))
            href = urljoin(str(response.url), anchor["href"].strip())
            if href in seen or not valid_official_url(href):
                continue
            seen.add(href)
            text = f"{label} {href}"
            item = {"title": label[:180] or "Link relacionado", "url": href}
            if PUBLIC_TERMS.search(text):
                result["public_selections"].append(item)
            elif JOB_TERMS.search(text) and label and len(label) >= 5:
                # Link relacionado não é, por si só, anúncio individual de vaga.
                result["individual_opportunities"].append({**item, "validation_status": "link_requires_manual_review"})
        result["status"] = "ok"
        result["is_confirmed_job_board"] = False
        result["note"] = "Portal acessível; links candidatos não foram confirmados como anúncios individuais."
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        result.update(status="connection_error", error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        result.update(status="processing_error", error=f"{type(exc).__name__}: {exc}")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="resultado-fgtas-rs/prefeituras_rs.json")
    parser.add_argument("--pause", type=float, default=1.0)
    args = parser.parse_args()
    results = []
    timeout = httpx.Timeout(20.0, connect=8.0)
    with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
        for index, (municipality, category, url) in enumerate(TARGETS):
            results.append(inspect_target(client, municipality, category, url))
            if index < len(TARGETS) - 1:
                time.sleep(max(0.0, args.pause))
    payload = {
        "source": "Teste isolado de portais municipais RS",
        "generated_at_poa": datetime.now(TZ_POA).isoformat(),
        "database_written": False,
        "summary": {
            "municipalities_tested": len(results),
            "http_200": sum(x.get("http_status") == 200 for x in results),
            "connection_errors": sum(x["status"] == "connection_error" for x in results),
            "candidate_links_for_manual_review": sum(len(x["individual_opportunities"]) for x in results),
            "confirmed_job_boards": sum(x["is_confirmed_job_board"] for x in results),
        },
        "municipalities": results,
        "rule": "Nenhum link é promovido automaticamente a vaga; requer validação manual da página de destino.",
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"JSON salvo em: {path}")


if __name__ == "__main__":
    main()
