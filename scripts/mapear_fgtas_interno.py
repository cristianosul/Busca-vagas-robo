#!/usr/bin/env python3
"""Mapeia páginas internas, PDFs e boletins de vagas dentro de fgtas.rs.gov.br."""

import re
from collections import deque
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

ALLOWED_HOSTS = {"www.fgtas.rs.gov.br", "fgtas.rs.gov.br"}
USER_AGENT = "BuscaVagasBot/0.1 (+public job listing discovery)"

SEEDS = [
    "https://www.fgtas.rs.gov.br/",
    "https://www.fgtas.rs.gov.br/empregos-no-rs",
    "https://www.fgtas.rs.gov.br/agencias-fgtas-sine",
    "https://www.fgtas.rs.gov.br/noticias",
]

KEYWORDS = re.compile(
    r"(vaga|emprego|sine|boletim|painel|agencia|ag[eê]ncia|oportunidade|mutir[aã]o|balc[aã]o|pdf|relat[oó]rio)",
    re.I,
)


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalizar_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl()


def mapear_fgtas(max_paginas: int = 45) -> None:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    }

    visitadas: set[str] = set()
    fila: deque[tuple[str, int]] = deque((s, 0) for s in SEEDS)

    pdfs: dict[str, str] = {}
    paginas_agencias: dict[str, str] = {}
    paginas_vagas: dict[str, str] = {}

    with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True) as client:
        while fila and len(visitadas) < max_paginas:
            url, nivel = fila.popleft()
            url = normalizar_url(url)
            if url in visitadas:
                continue

            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in ALLOWED_HOSTS:
                continue

            visitadas.add(url)

            try:
                resp = client.get(url)
                ctype = resp.headers.get("content-type", "").lower()
                if "pdf" in ctype or url.lower().endswith(".pdf"):
                    pdfs[url] = "(PDF direto)"
                    continue
                if resp.status_code != 200 or "html" not in ctype:
                    continue
            except Exception as exc:
                print(f"[ERRO] {url} -> {type(exc).__name__}: {exc}")
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            titulo_pagina = clean(soup.title.get_text() if soup.title else "")

            for a in soup.find_all("a", href=True):
                href_abs = normalizar_url(urljoin(url, a["href"].strip()))
                texto = clean(a.get_text(" ", strip=True))
                p_href = urlparse(href_abs)

                if p_href.hostname not in ALLOWED_HOSTS:
                    continue

                rotulo = f"{texto or '(sem texto)'} [origem: {titulo_pagina[:45]}]"

                if href_abs.lower().endswith(".pdf") or "/upload/" in href_abs.lower():
                    pdfs[href_abs] = rotulo
                    continue

                if re.search(r"ag[eê]ncia|sine-", href_abs, re.I) or re.search(r"ag[eê]ncia", texto, re.I):
                    paginas_agencias[href_abs] = rotulo

                if KEYWORDS.search(href_abs) or KEYWORDS.search(texto):
                    paginas_vagas[href_abs] = rotulo
                    if nivel < 2 and href_abs not in visitadas:
                        fila.append((href_abs, nivel + 1))

    print(f"Páginas HTML visitadas: {len(visitadas)}")

    print(f"\n=== 1. PDFs / ARQUIVOS ENCONTRADOS ({len(pdfs)}) ===")
    for u, desc in sorted(pdfs.items()):
        print(f"  - {desc[:85]} -> {u}")

    print(f"\n=== 2. PÁGINAS DE AGÊNCIAS FGTAS/SINE ({len(paginas_agencias)}) ===")
    for u, desc in sorted(paginas_agencias.items()):
        print(f"  - {desc[:85]} -> {u}")

    print(f"\n=== 3. DEMAIS PÁGINAS SOBRE VAGAS / BOLETINS ({len(paginas_vagas)}) ===")
    for u, desc in sorted(paginas_vagas.items()):
        if u not in paginas_agencias:
            print(f"  - {desc[:85]} -> {u}")


if __name__ == "__main__":
    mapear_fgtas()
