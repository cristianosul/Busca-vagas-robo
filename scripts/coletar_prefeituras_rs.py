#!/usr/bin/env python3
"""Diagnóstico conservador de serviços públicos de emprego no RS; não grava em banco."""

import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

TZ_POA = ZoneInfo("America/Sao_Paulo")
HEADERS = {
    "User-Agent": "BuscaVagasBot/0.4 (+public municipal employment research)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "pt-BR,pt;q=0.9",
}
# Endereços oficiais ou plataformas apontadas por páginas oficiais.
# Portal raiz nunca é classificado como quadro de vagas.
TARGETS = [
    {
        "municipality": "Caxias do Sul",
        "url": os.getenv("URL_CAXIAS_FGTAS_SINE", "https://caxias.rs.gov.br/"),
        "kind": "municipal_portal",
        "expected": "portal_root",
        "note": "Portal municipal confirmado. O endereço específico do Balcão de Empregos não foi validado; não usar URL antiga que retornou 404.",
    },
    {
        "municipality": "Novo Hamburgo",
        "url": os.getenv(
            "URL_NOVO_HAMBURGO_FGTAS_SINE",
            "https://novohamburgo.rs.gov.br/smdei/diretoria-trabalho/gerencia-trabalho",
        ),
        "kind": "employment_service",
        "expected": "employment_service",
        "note": "Página oficial da Gerência do Trabalho / Oportunidades de Trabalho.",
    },
    {
        "municipality": "Bento Gonçalves",
        "url": os.getenv("URL_BENTO_GONCALVES", "https://bentogoncalves.atende.net/"),
        "kind": "municipal_portal",
        "expected": "portal_root",
        "note": "Portal oficial Atende.Net; não presumir que seja um quadro de vagas.",
    },
    {
        "municipality": "Canoas",
        "url": os.getenv(
            "URL_CANOAS_BANCO",
            "https://sistemas.canoas.rs.gov.br/bancodeoportunidades/",
        ),
        "kind": "employment_platform",
        "expected": "employment_platform",
        "note": "Plataforma Banco de Oportunidades; acesso/cadastro não significa vagas individuais extraídas.",
    },
    {
        "municipality": "Pelotas",
        "url": os.getenv("URL_PELOTAS_OPORTUNIDADES", "https://www.pelotas.com.br/oportunidades"),
        "kind": "public_opportunities_portal",
        "expected": "public_selection_portal",
        "note": "Seção municipal de oportunidades, concursos, estágios e seleções públicas; não é quadro geral de empregos privados.",
    },
]

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
MAX_HTML_BYTES = 4 * 1024 * 1024
PLACEHOLDER_TERMS = re.compile(r"\b(teste|testes|homologacao|homologação|ambiente de teste)\b", re.I)
CULTURE_EXCLUSIONS = re.compile(
    r"\b(pnab|cultura|cultural|artista|artes[aã]o|edital cultural|"
    r"resultado preliminar.*edital)\b", re.I
)
JOB_TERMS = re.compile(
    r"\b(vaga(s)? de emprego|oportunidade(s)? de trabalho|"
    r"processo seletivo simplificado|sele[cç][aã]o p[uú]blica|concurso(s)?)\b", re.I
)


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def host_allowed(url, original_url):
    parsed = urlparse(url)
    original = urlparse(original_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    # Redirects and links stay within the original official domain.
    domain = original.hostname.lower()
    host = parsed.hostname.lower()
    return host == domain or host.endswith("." + domain)


def fetch_with_retry(client, url, attempts=4):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.get(url)
            if not host_allowed(str(response.url), url):
                return None, "rejected_redirect_host"
            if response.status_code in RETRYABLE_STATUS and attempt < attempts:
                time.sleep(min(attempt * 2, 8))
                continue
            response.raise_for_status()
            if len(response.content) > MAX_HTML_BYTES:
                return None, "response_too_large"
            return response, None
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(attempt * 2, 8))
        except httpx.HTTPStatusError as exc:
            return None, f"http_{exc.response.status_code}"
    if last_error:
        return None, f"{type(last_error).__name__}: {last_error}"
    return None, "request_failed_after_retries"


def inspect_target(client, target):
    url = target["url"]
    item = {
        "municipality": target["municipality"],
        "target_type": target["kind"],
        "expected_classification": target["expected"],
        "source_url": url,
        "status": "not_checked",
        "individual_job_records": [],
        "candidate_links": [],
        "excluded_placeholder_links": [],
        "is_confirmed_job_board": False,
        "note": target["note"],
    }
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        item.update(status="invalid_url", error="A URL precisa usar HTTPS e ter host.")
        return item

    response, error = fetch_with_retry(client, url)
    if error:
        item.update(status=error.split(":")[0], error=error)
        return item

    item.update(
        status="ok",
        http_status=response.status_code,
        final_url=str(response.url),
        response_bytes=len(response.content),
    )
    soup = BeautifulSoup(response.text, "html.parser")
    item["page_title"] = clean(soup.title.get_text(" ", strip=True))[:180] if soup.title else ""

    # Apenas links de texto dentro do mesmo domínio oficial. Não extrai uma vaga
    # de uma simples ocorrência da palavra "emprego" no portal.
    seen = set()
    for anchor in soup.find_all("a", href=True):
        label = clean(anchor.get_text(" ", strip=True))
        href = anchor["href"].strip()
        if not href.startswith("https://"):
            continue
        if not host_allowed(href, url):
            continue
        # Remove fragment and normalize trailing slash for duplicate detection.
        parsed_href = urlparse(href)
        normalized = parsed_href._replace(fragment="", path=parsed_href.path.rstrip("/") or "/").geturl()
        if normalized in seen:
            continue
        seen.add(normalized)
        combined = f"{label} {href}"
        if PLACEHOLDER_TERMS.search(combined):
            item["excluded_placeholder_links"].append({
                "title": label[:180],
                "url": href,
                "validation_status": "excluded_placeholder",
                "reason": "Título ou endereço contém indicação explícita de teste/homologação.",
            })
            continue
        if CULTURE_EXCLUSIONS.search(combined):
            continue
        if JOB_TERMS.search(combined) and label:
            item["candidate_links"].append({
                "title": label[:180],
                "url": normalized,
                "validation_status": "candidate_only_manual_review",
            })

    item["candidate_links"] = item["candidate_links"][:20]
    item["excluded_placeholder_links"] = item["excluded_placeholder_links"][:20]
    item["classification"] = target["expected"]
    item["is_confirmed_job_board"] = False
    item["classification_note"] = (
        "Acesso HTTP confirmado; isso não comprova a existência de vagas individuais "
        "nem a atualização do conteúdo."
    )
    return item


def collect_all(pause=1.0):
    results = []
    timeout = httpx.Timeout(35.0, connect=25.0)
    with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
        for index, target in enumerate(TARGETS):
            item = inspect_target(client, target)
            results.append(item)
            print(
                f"{item['municipality']}: {item['status']} | "
                f"HTTP {item.get('http_status', '-')} | "
                f"links candidatos: {len(item['candidate_links'])}"
            )
            if index < len(TARGETS) - 1:
                time.sleep(max(0.0, pause))

    return {
        "source": "Diagnóstico municipal FGTAS/SINE RS - versão 3.1",
        "generated_at_poa": datetime.now(TZ_POA).isoformat(),
        "timezone": "America/Sao_Paulo",
        "database_written": False,
        "summary": {
            "municipalities_tested": len(results),
            "http_200": sum(x.get("http_status") == 200 for x in results),
            "connection_errors": sum(
                x["status"] in {"ConnectTimeout", "ReadTimeout", "ConnectError", "NetworkError"}
                for x in results
            ),
            "candidate_links_for_manual_review": sum(len(x["candidate_links"]) for x in results),
            "excluded_placeholder_links": sum(len(x["excluded_placeholder_links"]) for x in results),
            "confirmed_job_boards": sum(bool(x["is_confirmed_job_board"]) for x in results),
            "individual_job_records": sum(len(x["individual_job_records"]) for x in results),
        },
        "municipalities": results,
        "rule": (
            "Portais, serviços e plataformas são classificados separadamente. "
            "Links com indicação de teste/homologação são excluídos; demais links seguem "
            "como candidatos para revisão manual e nunca viram vaga individual automaticamente."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="resultado-fgtas-rs/prefeituras_rs.json")
    parser.add_argument("--pause", type=float, default=1.0)
    args = parser.parse_args()
    payload = collect_all(args.pause)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"JSON salvo em: {path}")


if __name__ == "__main__":
    main()
