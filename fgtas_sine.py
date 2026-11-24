#!/usr/bin/env python3
"""Coletor experimental do painel FGTAS/SINE de Caxias do Sul.

Consulta a página oficial, baixa o PDF diário e gera JSON compatível com
o importador FGTAS já existente no Robô 1. Não acessa nem altera o banco.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

PAGE_URL = (
    "https://caxias.rs.gov.br/servicos/desenvolvimento-economico/"
    "vagas-de-emprego-fgtas-sine"
)
ALLOWED_PDF_HOST = "gcpstorage.caxias.rs.gov.br"
CITY = "Caxias do Sul"
SOURCE = "FGTAS/SINE Caxias do Sul"
USER_AGENT = "BuscaVagasBot/0.1 (+coleta respeitosa de vagas publicas)"
MAX_PDF_BYTES = 20 * 1024 * 1024
JOB_START = re.compile(r"(?<!\d)(\d{7})\s*[–—-]\s*")
SALARY = re.compile(r"R\$\s*[\d.]+,\d{2}(?:\s*(?:A|À)\s*R\$\s*[\d.]+,\d{2})?", re.I)


class CollectionError(RuntimeError):
    pass


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,*/*"},
        timeout=30.0,
        follow_redirects=True,
    )


def find_latest_pdf(client: httpx.Client) -> tuple[str, str]:
    response = client.get(PAGE_URL)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    page_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    match = re.search(
        r"Vagas\s+de\s+Emprego\s+para\s+(\d{1,2})\s+de\s+"
        r"(janeiro|fevereiro|março|abril|maio|junho|julho|agosto|"
        r"setembro|outubro|novembro|dezembro)\s+de\s+(\d{4})",
        page_text, re.I,
    )
    if not match:
        raise CollectionError("Não foi possível identificar a data da edição na página oficial.")

    months = {
        "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
        "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
        "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
    }
    day, month_name, year = match.groups()
    edition_date = date(int(year), months[month_name.lower()], int(day)).isoformat()

    for anchor in soup.find_all("a", href=True):
        href = urljoin(PAGE_URL, anchor["href"])
        parsed = urlparse(href)
        label = anchor.get_text(" ", strip=True).lower()
        if (
            parsed.scheme == "https"
            and parsed.hostname == ALLOWED_PDF_HOST
            and (parsed.path.lower().endswith(".pdf") or "painel" in label or "download" in label)
        ):
            return href, edition_date

    raise CollectionError("A página oficial não apresentou um link PDF reconhecido.")


def download_pdf(client: httpx.Client, url: str) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_PDF_HOST:
        raise CollectionError("O PDF não pertence ao domínio oficial permitido.")

    chunks = []
    total = 0
    with client.stream("GET", url) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                raise CollectionError("O PDF excedeu o limite de 20 MB.")
            chunks.append(chunk)
    data = b"".join(chunks)
    if not data.startswith(b"%PDF"):
        raise CollectionError("O endereço oficial não retornou um PDF válido.")
    return data


def _pdf_text(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
        if reader.is_encrypted:
            raise CollectionError("O PDF está protegido.")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except CollectionError:
        raise
    except Exception as exc:
        raise CollectionError(f"Falha ao extrair o texto do PDF: {exc}") from exc
    if len(text.strip()) < 500:
        raise CollectionError("O PDF não contém texto suficiente; pode exigir OCR.")
    return text


def _employment_type(text: str) -> str:
    upper = text.upper()
    if "JOVEM APRENDIZ" in upper or "APRENDIZ" in upper:
        return "Jovem aprendiz"
    if "ESTÁGIO" in upper or "ESTAGIO" in upper:
        return "Estágio"
    if any(term in upper for term in ("TEMPORÁRIA", "TEMPORARIA", "TEMPORÁRIO")):
        return "Temporário"
    if "FREELANCE" in upper:
        return "Freelance"
    return "Não informado"


def parse_jobs(pdf_bytes: bytes, edition_date: str, pdf_url: str) -> dict:
    text = re.sub(r"\s+", " ", _pdf_text(pdf_bytes)).replace("\u00a0", " ")
    starts = list(JOB_START.finditer(text))
    if len(starts) < 10:
        raise CollectionError(f"Foram encontrados apenas {len(starts)} códigos de vaga.")

    items = []
    seen = set()
    title_end = re.compile(
        r"\s+-\s*(?=(?:OBRIGAT[ÓO]RIO|N[ÃA]O\s+PRECISA|"
        r"VAGA\s+(?:DE|PARA|EXCLUSIVA|ACEITA)|TRABALHAR|"
        r"PARA\s+(?:TRABALHAR|ATUAR|AUXILIAR)|EMPRESA\s+BUSCA))",
        re.I,
    )

    for index, start in enumerate(starts):
        code = start.group(1)
        if code in seen:
            continue
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[start.end():end].strip(" -–—•")
        if not block:
            continue

        title_match = title_end.search(block)
        title = (block[:title_match.start()] if title_match else block[:100]).strip(" -–—")
        title = re.sub(r"\s+", " ", title).strip()
        if not title or len(title) > 120:
            continue

        salaries = list(SALARY.finditer(block))
        salary = salaries[-1].group(0).strip() if salaries else ""
        upper = block.upper()
        items.append({
            "source_id": code,
            "title": title,
            "company": "Confidencial (vaga intermediada pelo FGTAS/SINE)",
            "location": f"{CITY}, RS",
            "mode": "Presencial",
            "employment_type": _employment_type(block),
            "salary": salary,
            "description": block[:5000],
            "source": SOURCE,
            "source_document": "PAINEL PDF.pdf",
            "publication_date": edition_date,
            "valid_through": edition_date,
            "pcd_exclusive": "EXCLUSIVA PCD" in upper or "VAGA PCD" in upper,
            "no_experience_required": (
                "NÃO PRECISA DE EXPERIÊNCIA" in upper
                or "NAO PRECISA DE EXPERIENCIA" in upper
            ),
            # O PDF é a referência oficial; a candidatura é presencial na agência.
            "application_url": pdf_url,
            "application_instructions": (
                "Consulte a disponibilidade antes de se deslocar. Anote o código "
                "da vaga e dirija-se à agência FGTAS/SINE, Rua Bento Gonçalves, "
                f"1901, Centro, Caxias do Sul/RS. Painel oficial: {pdf_url}"
            ),
        })
        seen.add(code)

    if len(items) < 10:
        raise CollectionError(f"Foram extraídos apenas {len(items)} registros válidos.")

    count = re.search(r"oferece\s+(\d+)\s+vagas", text, re.I)
    return {
        "source": SOURCE,
        "city": CITY,
        "state": "RS",
        "publication_date": edition_date,
        "announced_openings": int(count.group(1)) if count else len(items),
        "job_records": len(items),
        "source_page": PAGE_URL,
        "source_document_url": pdf_url,
        "notes": (
            "Extraído do painel PDF oficial. Vagas anunciadas podem representar "
            "postos de trabalho; job_records representa códigos/registros."
        ),
        "items": items,
    }


def collect(allow_stale: bool = False) -> dict:
    with _client() as client:
        pdf_url, edition_date = find_latest_pdf(client)
        today = datetime.now(ZoneInfo("America/Sao_Paulo")).date().isoformat()
        stale_warning = None
        if edition_date != today:
            msg = (
                f"A página anuncia {edition_date}, mas hoje é {today}."
            )
            if not allow_stale:
                raise CollectionError(
                    f"{msg} Coleta interrompida para evitar importar painel antigo."
                )
            stale_warning = msg
        pdf_bytes = download_pdf(client, pdf_url)
    payload = parse_jobs(pdf_bytes, edition_date, pdf_url)
    if stale_warning:
        payload["warning"] = stale_warning
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--stdout", action="store_true")
    parser.add_argument(
        "--allow-older",
        action="store_true",
        help="Permite coletar mesmo quando a data da edição for diferente de hoje.",
    )
    args = parser.parse_args()

    try:
        payload = collect(allow_stale=args.allow_older)
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.stdout:
            sys.stdout.write(serialized)
        else:
            output = args.output or Path(
                f"fgtas_sine_caxias_{payload['publication_date']}.json"
            )
            output.write_text(serialized, encoding="utf-8")
            print(
                f"FGTAS/SINE: {payload['job_records']} registros; "
                f"{payload['announced_openings']} vagas anunciadas. Arquivo: {output}"
            )
        return 0
    except (httpx.HTTPError, CollectionError, OSError, ValueError) as exc:
        logging.error("Coleta FGTAS/SINE não concluída: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
