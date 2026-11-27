#!/usr/bin/env python3
"""Coletor público experimental FGTAS/SINE-RS V3.3. Não acessa banco."""

import argparse
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

BASE = "https://www.fgtas.rs.gov.br"
EMPREGOS_URL = f"{BASE}/empregos-no-rs"
NOTICIAS_URL = f"{BASE}/noticias"
ALLOWED_HOSTS = {"www.fgtas.rs.gov.br", "fgtas.rs.gov.br"}
TZ_POA = ZoneInfo("America/Sao_Paulo")
USER_AGENT = "BuscaVagasBot/0.2 (+public job listing research)"
MAX_ARTICLES = 15
MAX_HTML_BYTES = 5 * 1024 * 1024

MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}
DATE_RE = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4})|"
    r"(\d{1,2})\s+de\s+(janeiro|fevereiro|março|abril|maio|junho|"
    r"julho|agosto|setembro|outubro|novembro|dezembro)\s+de\s+(\d{4})",
    re.I,
)
COUNT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d{3})*|\d+)\s+vagas?\b", re.I)
AGENCY_RE = re.compile(r"Ag[eê]ncia\s+FGTAS\s*/?\s*Sine\s+(.+)$", re.I)
BULLET_RE = re.compile(r"^[.\-•]\s*(.+?)\s*[–—-]\s*(\d{1,4})\s+vagas?\s*[–—-]\s*(.+)$", re.I)
WEEKDAYS = {"segunda": 0, "terça": 1, "terca": 1, "quarta": 2, "quinta": 3,
            "sexta": 4, "sábado": 5, "sabado": 5, "domingo": 6}


class CollectionError(RuntimeError):
    pass


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def today_poa(reference_date=None):
    return reference_date or datetime.now(TZ_POA).date()


def extract_date(text):
    match = DATE_RE.search(text or "")
    if not match:
        return None
    try:
        if match.group(1):
            return date(int(match.group(3)), int(match.group(2)), int(match.group(1))).isoformat()
        return date(int(match.group(6)), MONTHS[match.group(5).lower()], int(match.group(4))).isoformat()
    except (ValueError, TypeError):
        return None


def extract_publication_date(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    match = re.search(r"\bPublica[cç][aã]o\s*:", text, re.I)
    return extract_date(text[match.start():match.start() + 120]) if match else extract_date(text[:500])


# Aliases explícitos: manter o texto bruto para auditoria e normalizar apenas o campo tratado.
MUNICIPALITY_ALIASES = {"cachoeira do s": "Cachoeira do Sul"}
AGENCY_ALIASES = {"cachoeira do s": "Cachoeira do Sul"}

def normalize_agency_name(value):
    """Remove pontuação órfã no final e aplica apenas aliases documentados."""
    raw = clean(value)
    cleaned = re.sub(r"\s*\([^)]*$", "", raw).strip().strip(" -:;,()")
    return AGENCY_ALIASES.get(cleaned.casefold(), cleaned)

def normalize_municipality(value):
    cleaned = re.sub(r"\s*\([^)]*$", "", clean(value)).strip().strip(" -:;,()")
    return MUNICIPALITY_ALIASES.get(cleaned.casefold(), cleaned)


def safe_get(client, url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise CollectionError(f"URL fora da lista permitida: {url}")
    last_error = None
    for attempt in range(1, 4):
        try:
            response = client.get(url)
            final = urlparse(str(response.url))
            if final.scheme != "https" or final.hostname not in ALLOWED_HOSTS:
                raise CollectionError(f"Redirecionamento para host não permitido: {response.url}")
            response.raise_for_status()
            if len(response.content) > MAX_HTML_BYTES:
                raise CollectionError(f"Página excedeu {MAX_HTML_BYTES} bytes")
            return response.text
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code not in {408, 425, 429, 500, 502, 503, 504}:
                raise
        except httpx.TransportError as exc:
            last_error = exc
        if attempt < 3:
            time.sleep(attempt * 2)
    raise CollectionError(f"Falha após 3 tentativas: {type(last_error).__name__}: {last_error}")


def get_article_links(html, source_url):
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for anchor in soup.find_all("a", href=True):
        title = clean(anchor.get_text(" ", strip=True))
        url = urljoin(source_url, anchor["href"])
        parsed = urlparse(url)
        if (title and re.search(r"\b(vagas?|empregos?|oportunidades de emprego)\b", title, re.I)
                and parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS
                and parsed.path not in {"/empregos-no-rs", "/noticias"}):
            found[url] = {"title": title, "url": url}
    return list(found.values())


def validate_interview_schedule(schedule, publication_date, reference_date=None):
    """Valida datas sem presumir mês seguinte ou converter entrevistas em vagas."""
    today = today_poa(reference_date)
    result = {
        "reference_date_poa": today.isoformat(), "event_dates": [],
        "schedule_validation": "date_not_determined",
        "availability_status": "needs_confirmation", "is_today": False,
        "schedule_warning": "Data da entrevista não validada.",
    }
    if not publication_date:
        result["schedule_warning"] = "Data da publicação ausente; não validar entrevista."
        return result
    try:
        published = date.fromisoformat(publication_date)
    except (TypeError, ValueError):
        result["schedule_warning"] = "Data da publicação inválida."
        return result
    if published > today:
        result.update(schedule_validation="publication_date_in_future",
                      availability_status="not_validated",
                      schedule_warning="A data de publicação está no futuro em relação à coleta.")
        return result

    weekday_words = re.findall(
        r"\b(segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo)(?:-feira)?\b",
        (schedule or "").lower()
    )
    weekdays = [WEEKDAYS[word] for word in weekday_words]
    group = re.search(r"\((\d{1,2}(?:\s*(?:e|,|/)\s*\d{1,2})*)\)", schedule or "")
    if not group:
        result["schedule_warning"] = "Dia do mês não identificado de forma estruturada."
        return result
    days = [int(x) for x in re.findall(r"\d{1,2}", group.group(1))]
    if not days or any(day < 1 or day > 31 for day in days):
        result.update(schedule_validation="invalid_day_number", availability_status="not_validated",
                      schedule_warning="Número de dia inválido no texto da entrevista.")
        return result
    checks = []
    for index, day in enumerate(days):
        try:
            event = published.replace(day=day)
        except ValueError:
            checks.append({"date": None, "weekday_matches": None, "status": "invalid_calendar_date"})
            continue
        expected = weekdays[index] if index < len(weekdays) else (weekdays[0] if len(weekdays) == 1 else None)
        matches = event.weekday() == expected if expected is not None else None
        # Se o dia já passou no mês da publicação, não presumir que se refere ao mês seguinte.
        if day < published.day:
            status = "ambiguous_month"
        elif matches is False:
            status = "weekday_mismatch"
        elif event < today:
            status = "past"
        elif event == today:
            status = "today"
        else:
            status = "future_unverified"
        checks.append({"date": event.isoformat(), "weekday_matches": matches, "status": status})
    statuses = [item["status"] for item in checks]
    result["event_dates"] = checks
    if any(status in {"weekday_mismatch", "invalid_calendar_date", "ambiguous_month"} for status in statuses):
        result.update(schedule_validation="date_or_weekday_not_validated",
                      availability_status="not_validated",
                      schedule_warning="Data ambígua, dia da semana divergente ou data inválida; conferir publicação.")
    elif statuses and all(status == "past" for status in statuses):
        result.update(schedule_validation="past_date", availability_status="expired",
                      schedule_warning=f"Entrevista passada em relação a {today.isoformat()} (Porto Alegre).")
    elif statuses and all(status in {"today", "future_unverified"} for status in statuses):
        is_today = "today" in statuses
        result.update(schedule_validation="date_consistent",
                      availability_status="today_unverified" if is_today else "future_unverified",
                      is_today=is_today,
                      schedule_warning="Data compatível com calendário, mas confirmar com a agência.")
    else:
        result.update(schedule_validation="date_requires_review", availability_status="not_validated",
                      schedule_warning="Data exige conferência manual.")
    return result


def extract_interviews(text, title, url, publication_date=None, reference_date=None):
    publication_date = publication_date or extract_date(text)
    current_agency, results = None, []
    for line in [clean(x) for x in text.splitlines()]:
        if not line:
            continue
        agency = AGENCY_RE.match(line)
        if agency:
            agency_raw = clean(agency.group(1))
            current_agency = {"raw": agency_raw, "normalized": normalize_agency_name(agency_raw)}
            continue
        bullet = BULLET_RE.match(line)
        if bullet and current_agency:
            schedule, quantity, roles = bullet.groups()
            validation = validate_interview_schedule(schedule, publication_date, reference_date)
            is_current_interview = validation["availability_status"] in {"today_unverified", "future_unverified"}
            agency_raw = current_agency["raw"]
            agency_name = current_agency["normalized"]
            municipality_raw = agency_raw.split(" Zona ")[0]
            results.append({
                "agency": agency_name, "agency_raw": agency_raw,
                "municipality": normalize_municipality(municipality_raw),
                "municipality_raw": municipality_raw,
                "state": "RS", "schedule_or_date": clean(schedule), **validation,
                "announced_openings": int(quantity), "positions_description": clean(roles),
                "report_title": title, "publication_date": publication_date,
                "source_url": url, "data_kind": "published_interview_opportunity",
                "is_current_unverified_interview": is_current_interview,
                "is_current_opportunity": False,
                "application_note": "Confirmar data, horário e disponibilidade diretamente com a agência.",
            })
    return results


def extract_aggregate(text, title, url, publication_date=None):
    lines = [clean(x) for x in text.splitlines() if clean(x)]
    total = None
    for i, line in enumerate(lines):
        context = " ".join(lines[max(0, i-1):i+2])
        if re.search(r"\b(RS|Estado|Rio Grande do Sul)\b", context, re.I):
            match = COUNT_RE.search(line)
            if match:
                total = int(match.group(1).replace(".", ""))
                break
    if total is None:
        return None
    return {
        "report_title": title, "publication_date": publication_date or extract_date(text),
        "state": "RS", "statewide_announced_openings": total, "agency_counts": [],
        "source_url": url, "data_kind": "aggregate_only",
        "note": "Número agregado; não representa vagas individuais e não deve ser somado a outro relatório.",
    }


def parse_article(html, title, url, reference_date=None):
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "nav", "footer", "header", "form"]):
        node.decompose()
    text = soup.get_text("\n", strip=True)
    pub_date = extract_publication_date(html)
    return (extract_aggregate(text, title, url, pub_date),
            extract_interviews(text, title, url, pub_date, reference_date))


def canonical_page_url(url):
    """Normaliza URL para deduplicação sem remover parâmetros funcionais."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return parsed._replace(
        scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower(),
        path=path, fragment="",
    ).geturl()


def collect():
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    timeout = httpx.Timeout(35.0, connect=15.0)
    aggregates, interviews, errors = [], [], []
    checked_by_url = {}
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        for index_url, index_title in [(EMPREGOS_URL, "Empregos no RS"), (NOTICIAS_URL, "Notícias FGTAS/SINE")]:
            canonical_index = canonical_page_url(index_url)
            if canonical_index in checked_by_url:
                continue
            try:
                html = safe_get(client, index_url)
                checked_by_url[canonical_index] = {
                    "title": index_title, "url": index_url, "status": "ok"
                }
                if index_url == EMPREGOS_URL:
                    agg, records = parse_article(html, index_title, index_url)
                    if agg: aggregates.append(agg)
                    interviews.extend(records)
                for item in get_article_links(html, index_url):
                    canonical = canonical_page_url(item["url"])
                    if canonical in checked_by_url:
                        continue
                    if len(checked_by_url) >= MAX_ARTICLES + 2:
                        break
                    try:
                        article = safe_get(client, item["url"])
                        agg, records = parse_article(article, item["title"], item["url"])
                        if agg: aggregates.append(agg)
                        interviews.extend(records)
                        checked_by_url[canonical] = {
                            "title": item["title"], "url": item["url"], "status": "ok"
                        }
                    except Exception as exc:
                        errors.append({"url": item["url"], "error": f"{type(exc).__name__}: {exc}"})
                        checked_by_url[canonical] = {
                            "title": item["title"], "url": item["url"], "status": "error"
                        }
            except Exception as exc:
                errors.append({"url": index_url, "error": f"{type(exc).__name__}: {exc}"})
                checked_by_url[canonical_index] = {
                    "title": index_title, "url": index_url, "status": "error"
                }

    checked = list(checked_by_url.values())
    unique = {}
    for item in interviews:
        key = (item["agency"], item["schedule_or_date"], item["announced_openings"],
               item["positions_description"], item["source_url"])
        unique[key] = item
    aggregates_unique = {x["source_url"]: x for x in aggregates}
    records = list(unique.values())
    return {
        "source": "FGTAS/SINE RS - diagnóstico V3.3",
        "generated_at_poa": datetime.now(TZ_POA).isoformat(),
        "timezone": "America/Sao_Paulo", "database_written": False,
        "summary": {
            "checked_pages": len(checked),
            "aggregate_reports": len(aggregates_unique),
            "interview_records": len(records),
            "current_unverified_interviews": sum(x.get("is_current_unverified_interview", False) for x in records),
            "current_confirmed_job_opportunities": 0,
            "expired_interviews": sum(x["availability_status"] == "expired" for x in records),
            "not_validated_interviews": sum(x["availability_status"] == "not_validated" for x in records),
        },
        "aggregate_reports": list(aggregates_unique.values()),
        "interview_records": records, "checked_pages": checked, "errors": errors,
        "limitations": [
            "Não acessa Emprega Brasil autenticado nem dados protegidos por login.",
            "Entrevistas não equivalem automaticamente a vagas abertas.",
            "Datas futuras permanecem não verificadas até confirmação com a agência.",
            "Relatórios agregados não são convertidos em vagas individuais.",
            "Nomes truncados são normalizados apenas quando há correspondência explícita; o texto original não é tratado como confirmação adicional.",
        ],
    }


def self_test():
    assert extract_date("Publicação: 26/11/2026") == "2026-11-26"
    assert extract_date("Publicação: 31/02/2026") is None
    expired = validate_interview_schedule("sexta-feira (17)", "2026-04-17", date(2026, 11, 27))
    assert expired["availability_status"] == "expired"
    mismatch = validate_interview_schedule("segunda-feira (17)", "2026-04-17", date(2026, 4, 1))
    assert mismatch["availability_status"] == "not_validated"
    ambiguous = validate_interview_schedule("quinta-feira (2)", "2026-11-27", date(2026, 11, 27))
    assert ambiguous["availability_status"] == "not_validated"
    future_publication = validate_interview_schedule("sexta-feira (28)", "2026-11-28", date(2026, 11, 27))
    assert future_publication["availability_status"] == "not_validated"
    assert normalize_agency_name("Carlos Barbosa (") == "Carlos Barbosa"
    assert normalize_agency_name("Taquara (") == "Taquara"
    assert normalize_agency_name("Cachoeira do S") == "Cachoeira do Sul"
    assert normalize_municipality("Cachoeira do S") == "Cachoeira do Sul"
    assert extract_aggregate("Rio Grande do Sul\n5.401 vagas", "Teste", "https://www.fgtas.rs.gov.br/x")
    sample = "Agência FGTAS/Sine Cachoeira do S\n• sexta-feira (17) - 2 vagas - Entrevistas"
    records = extract_interviews(sample, "Teste", "https://www.fgtas.rs.gov.br/x", "2026-04-17", date(2026, 4, 17))
    assert records[0]["agency"] == "Cachoeira do Sul"
    assert records[0]["agency_raw"] == "Cachoeira do S"
    assert records[0]["is_current_opportunity"] is False
    print("Autoteste FGTAS/SINE RS V3.3: OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="resultado-fgtas-rs/fgtas_sine_rs.json")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    payload = collect()
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"JSON salvo em: {path}")
    if payload["errors"]:
        print(f"Avisos de páginas com erro: {len(payload['errors'])}", file=sys.stderr)


if __name__ == "__main__":
    main()
