#!/usr/bin/env python3
"""Diagnóstico conservador FGTAS/SINE RS V3.5; não grava em banco."""

import argparse
import io
import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

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
        "url": os.getenv(
            "URL_CAXIAS_FGTAS_SINE",
            "https://caxias.rs.gov.br/servicos/desenvolvimento-economico/vagas-de-emprego-fgtas-sine",
        ),
        "kind": "official_employment_publication",
        "expected": "employment_service",
        "note": "Página oficial da Prefeitura com publicações de vagas FGTAS/Sine; o painel publicado pode ser PDF e não deve ser confundido com registros individuais extraídos.",
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
        "url": os.getenv(
            "URL_BENTO_GONCALVES",
            "https://bentogoncalves.atende.net/cidadao/pagina/sala-do-empreendedor",
        ),
        "fallback_urls": [
            "https://bentogoncalves.atende.net/",
            "https://www.bentogoncalves.rs.gov.br/",
        ],
        "kind": "municipal_economic_service",
        "expected": "municipal_service",
        "note": "Página Sala do Empreendedor indicada em documento oficial municipal; não é presumida como quadro de vagas. Se falhar, testa o portal raiz Atende.Net e a página inicial oficial da Prefeitura.",
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
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDFS_PER_PAGE = 3
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


def canonical_url(url):
    """Remove query e fragmento para identificar o mesmo arquivo por URLs variantes."""
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


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


def fetch_pdf(client, url, original_url):
    """Baixa somente PDF HTTPS do mesmo domínio oficial/subdomínio da página."""
    if not host_allowed(url, original_url):
        raise ValueError("PDF fora do domínio oficial permitido")
    response = client.get(url)
    if not host_allowed(str(response.url), url):
        raise ValueError("Redirecionamento do PDF para domínio não permitido")
    response.raise_for_status()
    if len(response.content) > MAX_PDF_BYTES:
        raise ValueError(f"PDF excede o limite de {MAX_PDF_BYTES} bytes")
    if not response.content.startswith(b"%PDF"):
        raise ValueError("O documento vinculado não possui assinatura PDF")
    return response.content

def parse_job_records_from_text(full_text, title, url, publication_date=None):
    """Extrai apenas blocos com código de vaga de sete dígitos; não usa total agregado como registros."""
    starts = list(re.finditer(r"(?m)(?<!\d)(\d{7})\s*[–—-]\s*", full_text or ""))
    records, seen_codes = [], set()
    for index, match in enumerate(starts):
        code = match.group(1)
        if code in seen_codes:
            continue
        seen_codes.add(code)
        end = starts[index + 1].start() if index + 1 < len(starts) else len(full_text)
        block = re.sub(r"\s+", " ", full_text[match.start():end]).strip(" •▪\t\r\n\x7f")
        code_match = re.match(r"(\d{7})\s*[–—-]\s*(.*)", block)
        if not code_match:
            continue
        remainder = code_match.group(2).strip()
        if len(remainder) < 3:
            continue
        # Só separa título/descrição quando há separador claro; mantém o texto integral em description.
        title_match = re.match(r"(.{3,140}?)(?:\s+[–—]\s+|\s+-\s+)(.+)$", remainder)
        job_title = title_match.group(1).strip(" -–—") if title_match else remainder[:140].strip()
        description = remainder
        salary_match = re.search(r"R\$\s*[\d.]+,\d{2}(?:\s*(?:A\s*HORA|/HORA))?", description, re.I)
        records.append({
            "source_id": code, "title": job_title,
            "company": "Confidencial (vaga intermediada pelo FGTAS/SINE)",
            "location": "Caxias do Sul, RS", "mode": "Presencial",
            "employment_type": "Não informado", "salary": salary_match.group(0) if salary_match else None,
            "description": description, "source": "FGTAS/SINE Caxias do Sul",
            "source_document": title, "publication_date": publication_date,
            "application_url": None,
            "application_instructions": "Anote o código da vaga e confirme a disponibilidade diretamente na agência FGTAS/SINE. O painel orienta atendimento na Rua Bento Gonçalves, 1901, Caxias do Sul.",
            "data_kind": "individual_job_record",
            "availability_status": "needs_confirmation",
            "availability_note": "Registro extraído de documento público; disponibilidade atual não foi confirmada.",
            "is_current_opportunity": False,
            "source_url": url,
        })
    return records


def apply_publication_date_guard(doc, records, reference_date=None):
    """Marca datas futuras/ausentes e impede que registros sejam tratados como atuais."""
    today = reference_date or datetime.now(TZ_POA).date()
    publication_date = doc.get("publication_date")
    if publication_date:
        try:
            parsed_date = date.fromisoformat(publication_date)
            is_future = parsed_date > today
            doc["publication_date_status"] = (
                "future_date_blocked" if is_future else "not_future_date_needs_confirmation"
            )
            doc["publication_date_is_future"] = is_future
        except ValueError:
            doc["publication_date_status"] = "invalid_date_needs_review"
            doc["publication_date_is_future"] = None
    else:
        doc["publication_date_status"] = "missing_date_needs_review"
        doc["publication_date_is_future"] = None

    for record in records:
        record["is_current_opportunity"] = False
        if doc.get("publication_date_is_future") is True:
            record["availability_status"] = "future_publication_date_needs_review"
            record["availability_note"] = (
                "A data impressa no PDF é posterior à data da coleta; registro bloqueado "
                "para qualquer classificação como oportunidade atual até revisão da fonte."
            )
        elif doc.get("publication_date_is_future") is None:
            record["availability_status"] = "publication_date_needs_review"
            record["availability_note"] = (
                "A data de publicação está ausente ou inválida; disponibilidade atual não pode ser confirmada."
            )
    return doc, records


def parse_caxias_pdf(pdf_bytes, title, url):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = [(page.extract_text() or "") for page in reader.pages]
    full_text = "\n".join(pages)
    compact = re.sub(r"[ \t]+", " ", full_text)
    date_match = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", compact)
    publication_date = None
    if date_match:
        try:
            from datetime import datetime as _dt
            publication_date = _dt.strptime(date_match.group(1), "%d/%m/%Y").date().isoformat()
        except ValueError:
            publication_date = None
    total_match = re.search(r"oferece\s+(\d{1,4})\s+vagas?", compact, re.I)
    declared_total = int(total_match.group(1)) if total_match else None
    records = parse_job_records_from_text(full_text, title, url, publication_date)
    if not full_text.strip():
        extraction_status = "no_text_extracted"
    elif records:
        extraction_status = "individual_records_extracted_needing_confirmation"
    else:
        extraction_status = "text_extracted_no_individual_records"
    return {
        "title": title[:180], "url": url, "content_type": "application/pdf",
        "download_status": "ok", "pages": len(reader.pages),
        "extracted_text_characters": len(full_text),
        "publication_date": publication_date,
        "declared_vacancy_total": declared_total,
        "parsed_individual_records": len(records),
        "extraction_status": extraction_status,
        "aggregate_total_is_not_record_count": True,
    }, records

def extract_pdf_links(soup, page_url):
    found = {}
    aliases = {}
    for anchor in soup.find_all("a", href=True):
        href = urljoin(page_url, anchor["href"].strip())
        parsed = urlparse(href)
        label = clean(anchor.get_text(" ", strip=True)) or "Documento PDF"
        is_pdf = parsed.path.lower().endswith(".pdf") or "application/pdf" in str(anchor.get("type", "")).lower()
        if label.casefold() in {"abrir no navegador", "download"}:
            label = "Painel de vagas FGTAS/SINE Caxias do Sul"
        if parsed.scheme == "https" and is_pdf and host_allowed(href, page_url):
            canonical = canonical_url(href)
            found.setdefault(canonical, label)
            aliases.setdefault(canonical, []).append(href)
    for frame in soup.find_all(["iframe", "embed", "object"]):
        href = frame.get("src") or frame.get("data")
        if not href:
            continue
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        mime = str(frame.get("type", "")).lower()
        is_pdf = parsed.path.lower().endswith(".pdf") or "application/pdf" in mime
        if parsed.scheme == "https" and is_pdf and host_allowed(absolute, page_url):
            canonical = canonical_url(absolute)
            found.setdefault(canonical, "Documento PDF incorporado")
            aliases.setdefault(canonical, []).append(absolute)
    return [(url, label, sorted(set(aliases.get(url, [url])))) for url, label in list(found.items())[:MAX_PDFS_PER_PAGE]]

def inspect_target(client, target):
    urls = [target["url"], *target.get("fallback_urls", [])]
    attempts = []
    response = None
    error = None
    selected_url = target["url"]
    for candidate_url in urls:
        selected_url = candidate_url
        response, error = fetch_with_retry(client, candidate_url)
        attempts.append({
            "url": candidate_url,
            "status": "ok" if error is None else error.split(":")[0],
            "error": error,
        })
        if response is not None:
            break

    item = {
        "municipality": target["municipality"],
        "target_type": target["kind"],
        "expected_classification": target["expected"],
        "source_url": target["url"],
        "attempted_urls": attempts,
        "status": "not_checked",
        "individual_job_records": [],
        "duplicate_job_records_removed": 0,
        "candidate_links": [],
        "excluded_placeholder_links": [],
        "documents": [],
        "is_confirmed_job_board": False,
        "note": target["note"],
    }
    if error:
        item.update(status=error.split(":")[0], error=error)
        return item

    item.update(
        status="ok", http_status=response.status_code,
        successful_url=str(response.url), final_url=str(response.url),
        response_bytes=len(response.content),
    )
    soup = BeautifulSoup(response.text, "html.parser")
    item["page_title"] = clean(soup.title.get_text(" ", strip=True))[:180] if soup.title else ""

    # A página de Caxias publica o painel em PDF. Extrair o documento, não apenas os links HTML.
    if target["municipality"] == "Caxias do Sul":
        seen_job_codes = set()
        for pdf_url, pdf_title, url_aliases in extract_pdf_links(soup, str(response.url)):
            try:
                # A URL com ?download=... e a URL sem query podem apontar para o mesmo PDF.
                pdf_bytes = fetch_pdf(client, pdf_url, str(response.url))
                doc, pdf_records = parse_caxias_pdf(pdf_bytes, pdf_title, pdf_url)
                doc["canonical_url"] = pdf_url
                doc["url_aliases"] = url_aliases
                doc["duplicate_url_variants_collapsed"] = max(0, len(url_aliases) - 1)
                # Registros com data futura/ausente continuam visíveis para auditoria, mas bloqueados.
                doc, pdf_records = apply_publication_date_guard(doc, pdf_records)
                for record in pdf_records:
                    code = record.get("source_id")
                    if code in seen_job_codes:
                        item["duplicate_job_records_removed"] += 1
                        continue
                    seen_job_codes.add(code)
                    item["individual_job_records"].append(record)

                doc["parsed_individual_records"] = len(pdf_records)
                doc["unique_job_codes_kept"] = len({r.get("source_id") for r in pdf_records})
                declared = doc.get("declared_vacancy_total")
                parsed_count = doc["unique_job_codes_kept"]
                doc["declared_vs_parsed_difference"] = (
                    declared - parsed_count if declared is not None else None
                )
                doc["declared_total_matches_parsed_records"] = (
                    declared == parsed_count if declared is not None else None
                )
                if declared is not None and declared != parsed_count:
                    doc["count_reconciliation_status"] = "declared_total_differs_from_extracted_unique_records"
                else:
                    doc["count_reconciliation_status"] = "no_declared_total_or_counts_match"
                item["documents"].append(doc)
            except Exception as exc:
                item["documents"].append({
                    "title": pdf_title[:180], "url": pdf_url,
                    "canonical_url": pdf_url, "url_aliases": url_aliases,
                    "download_status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                })

    seen = set()
    for anchor in soup.find_all("a", href=True):
        label = clean(anchor.get_text(" ", strip=True))
        href = anchor["href"].strip()
        if not href.startswith("https://") or not host_allowed(href, selected_url):
            continue
        parsed_href = urlparse(href)
        normalized = parsed_href._replace(
            fragment="", path=parsed_href.path.rstrip("/") or "/"
        ).geturl()
        if normalized in seen:
            continue
        seen.add(normalized)
        combined = f"{label} {href}"
        if PLACEHOLDER_TERMS.search(combined):
            item["excluded_placeholder_links"].append({
                "title": label[:180], "url": href,
                "validation_status": "excluded_placeholder",
                "reason": "Título ou endereço contém indicação explícita de teste/homologação.",
            })
            continue
        if CULTURE_EXCLUSIONS.search(combined):
            continue
        if JOB_TERMS.search(combined) and label:
            item["candidate_links"].append({
                "title": label[:180], "url": normalized,
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
        "source": "Diagnóstico municipal FGTAS/SINE RS - versão 3.5",
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
            "unique_individual_job_records": sum(len({r.get("source_id") for r in x["individual_job_records"]}) for x in results),
            "duplicate_job_records_removed": sum(x.get("duplicate_job_records_removed", 0) for x in results),
            "duplicate_pdf_url_variants_collapsed": sum(
                d.get("duplicate_url_variants_collapsed", 0) for x in results for d in x["documents"]
            ),
            "future_dated_documents_blocked": sum(1 for x in results for d in x["documents"] if d.get("publication_date_is_future") is True),
            "documents_with_declared_vs_parsed_mismatch": sum(
                1 for x in results for d in x["documents"]
                if d.get("count_reconciliation_status") == "declared_total_differs_from_extracted_unique_records"
            ),
            "declared_vacancy_total": sum(
                d.get("declared_vacancy_total", 0) or 0 for x in results for d in x["documents"]
            ),
            "declared_minus_unique_extracted": sum(
                (d.get("declared_vacancy_total", 0) or 0) - d.get("unique_job_codes_kept", 0)
                for x in results for d in x["documents"]
            ),
            "pdf_documents_found": sum(len(x["documents"]) for x in results),
            "pdf_documents_extracted": sum(1 for x in results for d in x["documents"] if d.get("extraction_status") in {"text_extracted_no_individual_records", "individual_records_extracted_needing_confirmation"}),
            "pdf_documents_with_individual_records": sum(1 for x in results for d in x["documents"] if d.get("parsed_individual_records", 0) > 0),
            "individual_records_needing_confirmation": sum(len(x["individual_job_records"]) for x in results),
        },
        "municipalities": results,
        "rule": (
            "Portais, serviços e plataformas são classificados separadamente. "
            "Links com indicação de teste/homologação são excluídos; demais links seguem "
            "como candidatos para revisão manual. PDFs oficiais de Caxias são deduplicados por URL canônica "
            "e códigos de vaga únicos; totais declarados são comparados com os códigos extraídos. Datas futuras "
            "são bloqueadas para classificação como oportunidade atual. Totais agregados nunca são convertidos em registros."
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
