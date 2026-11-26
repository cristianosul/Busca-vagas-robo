
#!/usr/bin/env python3
"""Coletor público experimental de informações FGTAS/SINE do RS.

Não acessa o Emprega Brasil autenticado, não usa gov.br e não grava em banco.
Separa números agregados de oportunidades de entrevista publicadas.
Valida datas, dias da semana e nomes dos municípios.
"""
from __future__ import annotations

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
USER_AGENT = "BuscaVagasBot/0.1 (+respectful public job listing collection)"
MAX_ARTICLES = 15
MAX_HTML_BYTES = 5 * 1024 * 1024

JOB_WORDS = re.compile(
    r"\b(vagas?|empregos?|oportunidades de emprego)\b", re.I
)

DATE_RE = re.compile(
    r"(?:Publica[cç][aã]o:\s*)?(\d{1,2})/(\d{1,2})/(\d{4})"
    r"|(?:Publica[cç][aã]o:\s*)?(\d{1,2})\s+de\s+"
    r"(janeiro|fevereiro|março|abril|maio|junho|julho|agosto|"
    r"setembro|outubro|novembro|dezembro)\s+de\s+(\d{4})",
    re.I,
)

COUNT_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:[.,]\d{3})*|\d+)\s+vagas?\b", re.I
)

AGENCY_RE = re.compile(
    r"Ag[eê]ncia\s+FGTAS\s*/?\s*Sine\s+(.+)$", re.I
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

WEEKDAYS = {
    "segunda": 0, "terça": 1, "terca": 1,
    "quarta": 2, "quinta": 3, "sexta": 4,
    "sábado": 5, "sabado": 5, "domingo": 6,
}

MUNICIPALITY_ALIASES = {
    "cachoeira do s": "Cachoeira do Sul",
    "carlos barbos": "Carlos Barbosa",
    "taquara": "Taquara",
}


class CollectionError(RuntimeError):
    pass


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def as_int(value: str) -> int:
    return int(value.replace(".", "").replace(",", ""))


def normalize_municipality(value: str) -> str:
    name = clean(value).strip(" ()-:;,")
    name = re.sub(r"\s*\([^)]*$", "", name).strip()
    name = re.sub(r"\s+", " ", name)

    key = re.sub(r"[^a-zà-ÿ ]", "", name.lower()).strip()
    return MUNICIPALITY_ALIASES.get(key, name)


def safe_get(client: httpx.Client, url: str) -> str:
    parsed = urlparse(url)

    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise CollectionError(f"URL fora da lista permitida: {url}")

    last_error = None

    for attempt in range(1, 4):
        try:
            response = client.get(url)

            final_url = urlparse(str(response.url))
            if (
                final_url.scheme != "https"
                or final_url.hostname not in ALLOWED_HOSTS
            ):
                raise CollectionError(
                    f"Redirecionamento para URL não permitida: {response.url}"
                )

            response.raise_for_status()

            if len(response.content) > MAX_HTML_BYTES:
                raise CollectionError(
                    f"Página excedeu {MAX_HTML_BYTES} bytes: {url}"
                )

            return response.text

        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code not in {
                408, 425, 429, 500, 502, 503, 504
            }:
                raise

        except httpx.TransportError as exc:
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
            title
            and JOB_WORDS.search(title)
            and parsed.scheme == "https"
            and parsed.hostname in ALLOWED_HOSTS
            and parsed.path not in {"/empregos-no-rs", "/noticias"}
        ):
            found[url] = {"title": title, "url": url}

    return list(found.values())


def extract_date(text: str) -> str | None:
    match = DATE_RE.search(text)

    if not match:
        return None

    try:
        if match.group(1):
            day, month, year = map(int, match.groups()[:3])
        else:
            day = int(match.group(4))
            month = MONTHS[match.group(5).lower()]
            year = int(match.group(6))

        return date(year, month, day).isoformat()

    except (ValueError, TypeError):
        return None


def extract_aggregate(text: str, title: str, url: str) -> dict | None:
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
        if re.search(
            r"Ag[eê]ncias?.{0,80}"
            r"(?:maiores|maior quantidade|maiores n[uú]meros)",
            line,
            re.I,
        ):
            agency_section = True

        if agency_section:
            for name, count in re.findall(
                r"([A-ZÀ-Ý][A-Za-zÀ-ÿ'’ .-]{1,45}?)"
                r"\s*\((\d{1,3}(?:\.\d{3})*)\)",
                line,
            ):
                if name.lower() not in {
                    "rio grande do sul", "porto alegre"
                }:
                    agency_counts.append({
                        "agency_or_city": normalize_municipality(name),
                        "announced_openings": as_int(count),
                    })

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
        "note": (
            "Números agregados; não representam registros individuais "
            "de vagas e não devem ser somados a outros relatórios."
        ),
    }


def validate_interview_schedule(
    schedule: str,
    publication_date: str | None,
) -> dict:
    result = {
        "event_dates": [],
        "schedule_validation": "date_not_determined",
        "availability_status": "needs_confirmation",
        "schedule_warning": None,
    }

    if not publication_date:
        result["schedule_warning"] = (
            "Data da publicação não identificada; entrevista não validada."
        )
        return result

    try:
        published = date.fromisoformat(publication_date)
    except ValueError:
        result["schedule_warning"] = (
            "Data de publicação inválida; entrevista não validada."
        )
        return result

    weekdays = []

    weekday_pattern = (
        r"\b(segunda|terça|terca|quarta|quinta|sexta|"
        r"sábado|sabado|domingo)(?:-feira)?\b"
    )

    for word in re.findall(weekday_pattern, schedule.lower()):
        if word in WEEKDAYS and WEEKDAYS[word] not in weekdays:
            weekdays.append(WEEKDAYS[word])

    day_group = re.search(
        r"\((\d{1,2}(?:\s*(?:e|,|/)\s*\d{1,2})*)\)",
        schedule,
    )

    if not day_group:
        result["schedule_warning"] = (
            "O anúncio não informa dia do mês de forma estruturada."
        )
        return result

    days = [
        int(value)
        for value in re.findall(r"\d{1,2}", day_group.group(1))
    ]

    checks = []

    for index, day in enumerate(days):
        try:
            event_date = published.replace(day=day)
        except ValueError:
            checks.append({
                "date": None,
                "weekday_matches": None,
                "status": "invalid_calendar_date",
            })
            continue

        expected_weekday = (
            weekdays[index]
            if index < len(weekdays)
            else weekdays[0] if len(weekdays) == 1 else None
        )

        matches = (
            event_date.weekday() == expected_weekday
            if expected_weekday is not None
            else None
        )

        if event_date < published:
            status = "past"
        elif matches is False:
            status = "weekday_mismatch"
        else:
            status = "future_unverified"

        checks.append({
            "date": event_date.isoformat(),
            "weekday_matches": matches,
            "status": status,
        })

    result["event_dates"] = checks
    statuses = [item["status"] for item in checks]

    if statuses and all(item == "past" for item in statuses):
        result.update({
            "schedule_validation": "past_date",
            "availability_status": "expired",
            "schedule_warning": (
                "Data da entrevista já passou em relação à publicação. "
                "Não tratar como oportunidade atual."
            ),
        })

    elif any(
        item in {"weekday_mismatch", "invalid_calendar_date"}
        for item in statuses
    ):
        result.update({
            "schedule_validation": "weekday_or_calendar_mismatch",
            "availability_status": "not_validated",
            "schedule_warning": (
                "Dia da semana não corresponde à data informada "
                "ou a data é inválida. Conferir a publicação original."
            ),
        })

    elif statuses and all(
        item == "future_unverified" for item in statuses
    ):
        result.update({
            "schedule_validation": "date_consistent",
            "availability_status": "future_unverified",
            "schedule_warning": (
                "Data futura compatível com o dia da semana, mas a "
                "disponibilidade deve ser confirmada com a FGTAS/SINE."
            ),
        })

    else:
        result.update({
            "schedule_validation": "date_requires_review",
            "availability_status": "not_validated",
            "schedule_warning": "A data exige conferência manual.",
        })

    return result


def extract_interviews(
    text: str,
    title: str,
    url: str,
) -> list[dict]:
    lines = [clean(line) for line in text.splitlines()]
    current_agency = None
    results = []
    publication_date = extract_date(text)

    for line in lines:
        if not line:
            continue

        agency_match = AGENCY_RE.match(line)

        if agency_match:
            current_agency = clean(
                agency_match.group(1)
            ).strip(" -:")
            continue

        bullet = BULLET_RE.match(line)

        if bullet and current_agency:
            when, quantity, roles = bullet.groups()

            municipality = normalize_municipality(
                current_agency.split(" Zona ")[0]
            )

            validation = validate_interview_schedule(
                clean(when),
                publication_date,
            )

            results.append({
                "agency": current_agency,
                "municipality": municipality,
                "state": "RS",
                "schedule_or_date": clean(when),
                **validation,
                "announced_openings": int(quantity),
                "positions_description": clean(roles),
                "report_title": title,
                "publication_date": publication_date,
                "source_url": url,
                "data_kind": "published_interview_opportunity",
                "is_current_opportunity": (
                    validation["availability_status"] == "future_unverified"
                ),
                "application_note": (
                    "Registro de entrevista divulgado pela FGTAS/SINE. "
                    "Mesmo com data futura compatível, confirmar data, "
                    "horário e disponibilidade diretamente com a agência."
                ),
            })

    return results


def parse_article(
    html: str,
    title: str,
    url: str,
) -> tuple[dict | None, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")

    for node in soup([
        "script", "style", "nav", "footer", "header", "form"
    ]):
        node.decompose()

    text = soup.get_text("\n", strip=True)

    return (
        extract_aggregate(text, title, url),
        extract_interviews(text, title, url),
    )


def collect() -> dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    }

    timeout = httpx.Timeout(45.0, connect=20.0)
    checked_articles = []

    with httpx.Client(
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
    ) as client:

        main_html = safe_get(client, EMPREGOS_URL)
        news_html = safe_get(client, NOTICIAS_URL)

        article_map = {}

        for source_html, source_url in (
            (main_html, EMPREGOS_URL),
            (news_html, NOTICIAS_URL),
        ):
            for item in get_article_links(source_html, source_url):
                article_map[item["url"]] = item

        main_aggregate, main_interviews = parse_article(
            main_html,
            "Empregos no RS (página principal)",
            EMPREGOS_URL,
        )

        aggregate_reports = (
            [main_aggregate] if main_aggregate else []
        )
        interview_opportunities = list(main_interviews)

        checked_articles.extend([
            {
                "title": "Empregos no RS (página principal)",
                "url": EMPREGOS_URL,
                "publication_date": extract_date(
                    BeautifulSoup(
                        main_html, "html.parser"
                    ).get_text(" ", strip=True)
                ),
                "individual_interview_records": len(main_interviews),
                "aggregate_data_found": bool(main_aggregate),
                "status": "ok",
                "page_kind": "main_public_page",
            },
            {
                "title": "Notícias FGTAS/SINE",
                "url": NOTICIAS_URL,
                "publication_date": extract_date(
                    BeautifulSoup(
                        news_html, "html.parser"
                    ).get_text(" ", strip=True)
                ),
                "individual_interview_records": 0,
                "aggregate_data_found": False,
                "status": "ok",
                "page_kind": "news_index",
            },
        ])

        for item in list(article_map.values())[:MAX_ARTICLES]:
            try:
                html = safe_get(client, item["url"])

                aggregate, interviews = parse_article(
                    html,
                    item["title"],
                    item["url"],
                )

                if aggregate:
                    aggregate_reports.append(aggregate)

                interview_opportunities.extend(interviews)

                checked_articles.append({
                    "title": item["title"],
                    "url": item["url"],
                    "publication_date": extract_date(
                        BeautifulSoup(
                            html, "html.parser"
                        ).get_text(" ", strip=True)
                    ),
                    "individual_interview_records": len(interviews),
                    "aggregate_data_found": bool(aggregate),
                    "status": "ok",
                    "page_kind": "article",
                })

            except (
                httpx.HTTPError,
                CollectionError,
                ValueError,
            ) as exc:
                print(
                    f"AVISO: notícia ignorada após falha: "
                    f"{item['url']} - {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

                checked_articles.append({
                    "title": item["title"],
                    "url": item["url"],
                    "publication_date": None,
                    "individual_interview_records": 0,
                    "aggregate_data_found": False,
                    "status": "error",
                    "page_kind": "article",
                    "error": f"{type(exc).__name__}: {exc}",
                })

    unique = {}

    for item in interview_opportunities:
        key = (
            item["agency"],
            item["schedule_or_date"],
            item["announced_openings"],
            item["positions_description"],
            item["source_url"],
        )
        unique[key] = item

    unique_aggregates = {}

    for report in aggregate_reports:
        if report:
            key = (
                report["source_url"],
                report["report_title"],
            )
            unique_aggregates[key] = report

    records = list(unique.values())

    municipalities = sorted({
        item["municipality"]
        for item in records
        if item["municipality"]
    })

    today = datetime.now(
        ZoneInfo("America/Sao_Paulo")
    ).date().isoformat()

    return {
        "collector": "FGTAS/SINE RS - protótipo público",
        "generated_at_local_date": today,
        "state": "Rio Grande do Sul",
        "municipality_universe": 497,
        "municipalities_with_individual_records": municipalities,
        "individual_municipality_count": len(municipalities),
        "aggregate_reports": list(unique_aggregates.values()),
        "published_interview_opportunities": records,
        "current_unverified_interview_opportunities": [
            item for item in records
            if item["is_current_opportunity"]
        ],
        "expired_interview_opportunities": [
            item for item in records
            if item["availability_status"] == "expired"
        ],
        "not_validated_interview_opportunities": [
            item for item in records
            if item["availability_status"] == "not_validated"
        ],
        "checked_articles": checked_articles,
        "coverage_status": (
            "partial_public_publications_only"
            if records or unique_aggregates
            else "no_records_extracted"
        ),
        "important_limit": (
            "Este coletor não acessa o Emprega Brasil autenticado. "
            "Não representa cobertura integral dos 497 municípios. "
            "Somente inclui dados encontrados em páginas públicas oficiais. "
            "Entrevistas futuras ainda exigem confirmação; registros vencidos "
            "ou com divergência de calendário não são oportunidades atuais."
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
    Agência FGTAS/Sine Cachoeira do Sul
    . domingo (22) – 8h – 10 vagas – auxiliar de produção
    Agência FGTAS/Sine Carlos Barbosa (
    . quarta-feira (25) – 14h – 5 vagas – auxiliar de produção
    """

    aggregate = extract_aggregate(
        sample,
        "Teste",
        "https://www.fgtas.rs.gov.br/teste",
    )

    interviews = extract_interviews(
        sample,
        "Teste",
        "https://www.fgtas.rs.gov.br/teste",
    )

    assert aggregate
    assert aggregate["statewide_announced_openings"] == 5123

    assert len(interviews) == 3

    assert interviews[0]["municipality"] == "Caxias do Sul"
    assert interviews[0]["availability_status"] == "future_unverified"

    assert interviews[1]["municipality"] == "Cachoeira do Sul"
    assert interviews[1]["availability_status"] == "expired"

    assert interviews[2]["municipality"] == "Carlos Barbosa"
    assert interviews[2]["availability_status"] == "not_validated"

    assert interviews[2]["schedule_validation"] == (
        "weekday_or_calendar_mismatch"
    )

    assert normalize_municipality("Taquara (") == "Taquara"

    print(
        "AUTOTESTE OK: municípios normalizados; datas vencidas "
        "e divergentes sinalizadas; agregados separados."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("resultado-fgtas-rs.json"),
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
    )

    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    try:
        result = collect()

        args.output.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        print(f"JSON gerado: {args.output}")
        print(
            f"Relatórios agregados: "
            f"{len(result['aggregate_reports'])}"
        )
        print(
            f"Entrevistas detalhadas: "
            f"{len(result['published_interview_opportunities'])}"
        )
        print(
            f"Entrevistas vencidas: "
            f"{len(result['expired_interview_opportunities'])}"
        )
        print(
            f"Entrevistas não validadas: "
            f"{len(result['not_validated_interview_opportunities'])}"
        )
        print(
            f"Municípios com registros individuais: "
            f"{result['individual_municipality_count']}/497"
        )
        print("Nenhum banco de dados foi acessado.")

        return 0

    except (
        httpx.HTTPError,
        CollectionError,
        OSError,
        ValueError,
    ) as exc:

        diagnostic = {
            "collector": "FGTAS/SINE RS - protótipo público",
            "generated_at_local_date": datetime.now(
                ZoneInfo("America/Sao_Paulo")
            ).date().isoformat(),
            "state": "Rio Grande do Sul",
            "coverage_status": "collection_error",
            "aggregate_reports": [],
            "published_interview_opportunities": [],
            "checked_articles": [],
            "error": f"{type(exc).__name__}: {exc}",
            "important_limit": (
                "Falha de comunicação durante a coleta. "
                "Nenhuma oportunidade individual deve ser considerada "
                "confirmada por este diagnóstico."
            ),
            "sources": [EMPREGOS_URL, NOTICIAS_URL],
        }

        try:
            args.output.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            args.output.write_text(
                json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            print(
                f"JSON de diagnóstico gerado: {args.output}"
            )

        except OSError as write_error:
            print(
                f"Não foi possível salvar diagnóstico: {write_error}",
                file=sys.stderr,
            )

        print(
            f"ERRO NA COLETA: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
