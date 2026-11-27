#!/usr/bin/env python3
"""Testa acesso via Tailscale BR e mapeia links/PDFs de vagas nas prefeituras do RS."""

import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

PREFEITURAS_RS = [
    ("Caxias do Sul (Balcao)", "https://caxias.rs.gov.br/servicos/desenvolvimento-economico/balcao-de-empregos"),
    ("Caxias do Sul (Home)", "https://caxias.rs.gov.br/"),
    ("Novo Hamburgo (Agencia)", "https://www.novohamburgo.rs.gov.br/sedec/agencia-municipal-empregos"),
    ("Bento Goncalves (Balcao)", "https://bentogoncalves.atende.net/cidadao/pagina/balcao-de-empregos"),
    ("Canoas (Banco de Oportunidades)", "https://www.canoas.rs.gov.br/servicos/banco-de-oportunidades/"),
    ("Pelotas (Home)", "https://www.pelotas.com.br/"),
]

TERMOS_VAGA = re.compile(
    r"(vaga|emprego|balc[aã]o|oportunidade|sine|boletim|lista|pdf|painel)",
    re.I,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
}


def clean(txt: str) -> str:
    return re.sub(r"\s+", " ", txt or "").strip()


def testar_prefeitura(client: httpx.Client, nome: str, url: str) -> None:
    print("\n==========================================")
    print(f"ALVO: {nome}")
    print(f"URL : {url}")
    try:
        resp = client.get(url)
        print(f"STATUS: HTTP {resp.status_code} | URL Final: {resp.url} | Tamanho: {len(resp.content)}B")

        if resp.status_code != 200:
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        titulo = clean(soup.title.get_text() if soup.title else "")
        print(f"TITULO: {titulo[:80]}")

        pdfs = []
        links_relevantes = []
        vistos = set()

        for a in soup.find_all("a", href=True):
            href = urljoin(str(resp.url), a["href"].strip())
            texto = clean(a.get_text(" ", strip=True))
            if href in vistos:
                continue
            vistos.add(href)

            if href.lower().endswith(".pdf") or "/upload/" in href.lower() or "download" in href.lower():
                pdfs.append((texto or "(PDF)", href))
            elif TERMOS_VAGA.search(texto) or TERMOS_VAGA.search(href):
                if urlparse(href).hostname == urlparse(str(resp.url)).hostname:
                    links_relevantes.append((texto or "(link)", href))

        texto_pagina = clean(soup.get_text(" ", strip=True))
        m_vagas = re.findall(r".{0,40}\b\d{1,4}\s+vagas?\b.{0,40}", texto_pagina, re.I)

        if m_vagas:
            print("TRECHOS COM QUANTIDADE DE VAGAS NA PAGINA:")
            for trecho in m_vagas[:5]:
                print(f"  * {trecho}")

        if pdfs:
            print(f"PDFs / DOWNLOADS ENCONTRADOS ({len(pdfs)}):")
            for t, u in pdfs[:8]:
                print(f"  [PDF] {t[:55]} -> {u}")

        if links_relevantes:
            print(f"LINKS INTERNOS SOBRE VAGAS ({len(links_relevantes)}):")
            for t, u in links_relevantes[:8]:
                print(f"  [LINK] {t[:55]} -> {u}")

    except Exception as exc:
        print(f"FALHA: {type(exc).__name__}: {exc}")


def main() -> None:
    with httpx.Client(headers=HEADERS, timeout=25.0, follow_redirects=True) as client:
        for nome, url in PREFEITURAS_RS:
            testar_prefeitura(client, nome, url)


if __name__ == "__main__":
    main()
