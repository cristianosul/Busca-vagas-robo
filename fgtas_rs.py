#!/usr/bin/env python3
"""Coletor público experimental de informações FGTAS/SINE do RS.

Não acessa o Emprega Brasil autenticado, não usa gov.br e não grava em banco.
Separa números agregados de oportunidades detalhadas em entrevistas publicadas.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

BASE = "https://www.fgtas.rs.gov.br"
EMPREGOS_URL = f"{BASE}/empregos-no-rs"
NOTICIAS_URL = f"{BASE}/noticias"
ALLOWED_HOST = "www.fgtas.rs.gov.br"
USER_AGENT = "BuscaVagasBot/0.1 (+respectful public job listing collection)"
MAX_ARTICLES = 15
MAX_HTML_BYTES = 5 * 1024 * 1024

JOB_WORDS = re.compile(r"\b(vagas?|empregos?|oportunidades de emprego)\b", re.I)
DATE_RE = re.compile(
    r"(?:Publica[cç][aã]o:\s*)?(\d{1,2})/(\d{1,2})/(\d{4})"
    r"|(?:Publica[cç][aã]o:\s*)?(\d{1,2})\s+de\s+"
    r"(janeiro|fevereiro|março|abril|maio|junho|julho|agosto|"
    r"setembro|outubro|novembro|dezembro)\s+de\s+(\d{4})",
    re.I,
)
COUNT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:[.,]\d{3})*|\d+)\s+vagas?\b", re.I)
AGENCY_RE = re.compile(
    r"Ag[eê]ncia\s+FGTAS\s*/?\s*Sine\s+(.+?)(?:\s*\(([^)]*)\))?$",
    re.I,
)
BULLET_RE = re.compile(
    r"^[.\-•]\s*(.+?)\s*[–—-]\s*(\d{1,4})\s+vagas?\s*[–—-]\s*(.+)$",
    re.I,
)
MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}


class CollectionError(RuntimeError):
    pass


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def as_int(value: str) -> int:
    return int(value.replace(".", "").replace(",", ""))


def safe_get(client: httpx.Client, url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {ALLOWED_HOST, "fgtas.rs.gov.br"}:
        raise CollectionError(f"URL fora da lista permitida: {url}")

    last_error = None

    for attempt in range(1, 4):
        try:
            response = client.get(url)

            final_url = urlparse(str(response.url))
            if final_url.scheme != "https" or final_url.hostname not in {
                ALLOWED_HOST, "fgtas.rs.gov.br"
            }:
                raise CollectionError(
                    f"Redirecionamento para URL fora da lista permitida: {response.url}"
                )

            response.raise_for_status()

            if len(response.content) > MAX_HTML_BYTES:
                raise CollectionError(
                    f"Página excedeu {MAX_HTML_BYTES} bytes: {url}"
                )

            return response.text

        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code not in {408, 425, 429, 500, 502, 503, 504}:
                raise

        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_error = exc

        if attempt < 3:
            wait_seconds = attempt * 3
            print(
                f"AVISO: tentativa {attempt}/3 falhou para {url}. "
                f"Nova tentativa em {wait_seconds}s.",
                file=sys.stderr,
            )
            time.sleep(wait_seconds)

    raise CollectionError(
        f"Falha após 3 tentativas para {url}: "
        f"{type(last_error).__name__}: {last_error}"
    )


def get_article_links(html: str, source_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for anchor in soup.find_all("a", href=True):
        title = clean(anchor.get_text(" ", strip=True))
        url = urljoin(source_url, anchor["href"])
        parsed = urlparse(url)
        if (
            title and JOB_WORDS.search(title)
            and parsed.scheme == "https"
            and parsed.hostname in {ALLOWED_HOST, "fgtas.rs.gov.br"}
            and parsed.path not in {"/empregos-no-rs", "/noticias"}
        ):
            found[url] = {"title": title, "url": url}
    return list(found.values())


def extract_date(text: str) -> str | None:
    match = DATE_RE.search(text)
    if not match:
        return None
    if match.group(1):
        day, month, year = map(int, match.groups()[:3])
    else:
        day = int(match.group(4))
        month = MONTHS[match.group(5).lower()]
        year = int(match.group(6))
    return f"{year:04d}-{month:02d}-{day:02d}"


def extract_aggregate(text: str, title: str, url: str) -> dict | None:
    # O total estadual é aceito somente quando aparece em contexto explícito de RS/Estado.
    lines = [clean(line) for line in text.splitlines() if clean(line)]
    statewide_total = None
    for index, line in enumerate(lines):
        context = " ".join(lines[max(0, index - 1): index + 2])
        if re.search(r"\b(RS|Estado|Rio Grande do Sul)\b", context, re.I):
            match = COUNT_RE.search(line)
            if match:
                statewide_total = as_int(match.group(1))
                break

    agency_counts = []
    agency_section = False
    for line in lines:
        if re.search(r"Ag[eê]ncias?.{0,80}(?:maiores|maior quantidade|maiores n[uú]meros)", line, re.I):
            agency_section = True
        if agency_section:
            # Formato comum: Nome da cidade (123)
            for name, count in re.findall(
                r"([A-ZÀ-Ý][A-Za-zÀ-ÿ'’ .-]{1,45}?)\s*\((\d{1,3}(?:\.\d{3})*)\)",
                line,
            ):
                if name.lower() not in {"rio grande do sul", "porto alegre"}:
                    agency_counts.append({"agency_or_city": clean(name), "announced_openings": as_int(count)})
            if agency_counts and re.search(r"ocupa[cç][oõ]es", line, re.I):
                agency_section = False

    if statewide_total is None and not agency_counts:
        return None
    return {
        "report_title": title,
        "publication_date": extract_date(text),
        "state": "RS",
        "statewide_announced_openings": statewide_total,
        "agency_counts": agency_counts,
        "source_url": url,
        "data_kind": "aggregate_only",
        "note": "Números agregados; não representam registros individuais de vagas.",
    }


def extract_interviews(text: str, title: str, url: str) -> list[dict]:
    lines = [clean(line) for line in text.splitlines()]
    current_agency = None
    results = []
    for line in lines:
        if not line:
            continue
        agency_match = AGENCY_RE.match(line)
        if agency_match:
            current_agency = clean(agency_match.group(1)).strip(" -:")
            continue
        bullet = BULLET_RE.match(line)
        if bullet and current_agency:
            when, quantity, roles = bullet.groups()
            results.append({
                "agency": current_agency,
                "municipality": current_agency.split(" Zona ")[0].strip(),
                "state": "RS",
                "schedule_or_date": clean(when),
                "announced_openings": int(quantity),
                "positions_description": clean(roles),
                "report_title": title,
                "publication_date": extract_date(text),
                "source_url": url,
                "data_kind": "published_interview_opportunity",
                "application_note": (
                    "Oportunidade divulgada para entrevista/agendamento na agência. "
                    "Confirmar data e disponibilidade diretamente com a FGTAS/SINE."
                ),
            })
    return results


def parse_article(html: str, title: str, url: str) -> tuple[dict | None, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    # Remover elementos de navegação que contaminam a extração.
    for node in soup(["script", "style", "nav", "footer", "header", "form"]):
        node.decompose()
    text = soup.get_text("\n", strip=True)
    return extract_aggregate(text, title, url), extract_interviews(text, title, url)


def collect() -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    timeout = httpx.Timeout(45.0, connect=20.0)
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        main_html = safe_get(client, EMPREGOS_URL)
        news_html = safe_get(client, NOTICIAS_URL)

        article_map = {}
        for source_html, source_url in (
            (main_html, EMPREGOS_URL),
            (news_html, NOTICIAS_URL),
        ):
            for item in get_article_links(source_html, source_url):
                article_map[item["url"]] = item

        # A página de empregos pode conter o resumo corrente no próprio conteúdo.
        main_aggregate, main_interviews = parse_article(
            main_html, "Empregos no RS (página principal)", EMPREGOS_URL
        )
        aggregate_reports = [main_aggregate] if main_aggregate else []
        interview_opportunities = list(main_interviews)
        checked_articles = []

        for item in list(article_map.values())[:MAX_ARTICLES]:
            html = safe_get(client, item["url"])
            aggregate, interviews = parse_article(html, item["title"], item["url"])
            if aggregate:
                aggregate_reports.append(aggregate)
            interview_opportunities.extend(interviews)
            checked_articles.append({
                "title": item["title"],
                "url": item["url"],
                "publication_date": extract_date(BeautifulSoup(html, "html.parser").get_text(" ", strip=True)),
                "individual_interview_records": len(interviews),
                "aggregate_data_found": bool(aggregate),
            })

    # Deduplicar eventos repetidos publicados em mais de uma página.
    unique = {}
    for item in interview_opportunities:
        key = (item["agency"], item["schedule_or_date"], item["announced_openings"], item["positions_description"], item["source_url"])
        unique[key] = item

    today = datetime.now(ZoneInfo("America/Sao_Paulo")).date().isoformat()
    return {
        "collector": "FGTAS/SINE RS - protótipo público",
        "generated_at_local_date": today,
        "state": "Rio Grande do Sul",
        "municipality_universe": 497,
        "municipalities_with_individual_records": sorted({
            item["municipality"] for item in unique.values()
        }),
        "individual_municipality_count": len({
            item["municipality"] for item in unique.values()
        }),
        "aggregate_reports": aggregate_reports,
        "published_interview_opportunities": list(unique.values()),
        "checked_articles": checked_articles,
        "coverage_status": (
            "partial_public_publications_only"
            if unique or aggregate_reports else "no_records_extracted"
        ),
        "important_limit": (
            "Este coletor não acessa o Emprega Brasil autenticado. "
            "Não representa cobertura integral dos 497 municípios. "
            "Somente inclui dados encontrados em páginas públicas oficiais."
        ),
        "sources": [EMPREGOS_URL, NOTICIAS_URL],
    }


def self_test() -> None:
    sample = """
    Publicação: 26/11/2026 às 10h00
    As Agências FGTAS/Sine oferecem 5.123 vagas de emprego no RS.
    As Agências com maiores números são: Erechim (300), Vacaria (200).
    Agência FGTAS/Sine Caxias do Sul (Rua Exemplo, 100)
    . sexta-feira (27) – 9h – 12 vagas – auxiliar de produção
    """
    aggregate = extract_aggregate(sample, "Teste", "https://www.fgtas.rs.gov.br/teste")
    interviews = extract_interviews(sample, "Teste", "https://www.fgtas.rs.gov.br/teste")
    assert aggregate and aggregate["statewide_announced_openings"] == 5123
    assert interviews and interviews[0]["announced_openings"] == 12
    assert interviews[0]["municipality"] == "Caxias do Sul"
    print("AUTOTESTE OK: agregados e oportunidades detalhadas separados.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("resultado-fgtas-rs.json"))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    try:
        result = collect()
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"JSON gerado: {args.output}")
        print(f"Relatórios agregados: {len(result['aggregate_reports'])}")
        print(f"Entrevistas detalhadas: {len(result['published_interview_opportunities'])}")
        print(f"Municípios com registros individuais: {result['individual_municipality_count']}/497")
        print("Nenhum banco de dados foi acessado.")
        return 0
    except (httpx.HTTPError, CollectionError, OSError, ValueError) as exc:
        print(f"ERRO NA COLETA: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
