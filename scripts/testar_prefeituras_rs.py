#!/usr/bin/env python3
"""Inspeciona páginas de oportunidades de Canoas/Pelotas e descobre rotas de Caxias/Novo Hamburgo."""

import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

TERMOS = re.compile(
    r"(vaga|emprego|balc[aã]o|oportunidade|sine|boletim|trabalh|curr[ií]culo|banco)",
    re.I,
)


def clean(txt: str) -> str:
    return re.sub(r"\s+", " ", txt or "").strip()


def inspecionar_pagina(client: httpx.Client, nome: str, url: str) -> None:
    print(f"\n==========================================")
    print(f"INSPECAO: {nome}")
    print(f"URL     : {url}")
    try:
        resp = client.get(url)
        print(f"STATUS  : HTTP {resp.status_code} | Final: {resp.url}")
        if resp.status_code != 200:
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        texto = clean(soup.get_text(" ", strip=True))
        print(f"RESUMO TEXTO: {texto[:450]}...")

        print("LINKS NO CONTEUDO PRINCIPAL:")
        vistos = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(str(resp.url), a["href"].strip())
            label = clean(a.get_text(" ", strip=True))
            if not href.startswith("http") or href in vistos:
                continue
            vistos.add(href)
            print(f"  - {label[:60] or '(sem texto)'} -> {href}")

    except Exception as exc:
        print(f"ERRO: {type(exc).__name__}: {exc}")


def descobrir_rotas_home(client: httpx.Client, nome: str, base_url: str, caminhos_extra: list[str]) -> None:
    print(f"\n==========================================")
    print(f"DESCOBERTA: {nome}")
    urls_para_testar = [base_url] + [urljoin(base_url, c) for c in caminhos_extra]
    encontrados: dict[str, str] = {}

    for u in urls_para_testar:
        try:
            resp = client.get(u)
            print(f"  [{resp.status_code}] {u}")
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = urljoin(str(resp.url), a["href"].strip())
                label = clean(a.get_text(" ", strip=True))
                if TERMOS.search(label) or TERMOS.search(href):
                    if urlparse(href).hostname == urlparse(base_url).hostname:
                        encontrados[href] = label or "(link)"
        except Exception as exc:
            print(f"  [ERRO] {u} -> {type(exc).__name__}")

    print(f"ROTAS RELACIONADAS A EMPREGO EM {nome} ({len(encontrados)}):")
    for href, label in sorted(encontrados.items())[:20]:
        print(f"  * {label[:60]} -> {href}")


def main() -> None:
    with httpx.Client(headers=HEADERS, timeout=25.0, follow_redirects=True) as client:
        # 1. Inspeciona as páginas já confirmadas com HTTP 200
        inspecionar_pagina(
            client,
            "Canoas - Banco de Oportunidades",
            "https://canoas.rs.gov.br/servicos/banco-de-oportunidades/",
        )
        inspecionar_pagina(
            client,
            "Canoas - Categoria Emprego e Qualificacao",
            "https://canoas.rs.gov.br/servicos_cat/emprego-e-qualificacao/",
        )
        inspecionar_pagina(
            client,
            "Pelotas - Oportunidades",
            "https://www.pelotas.com.br/oportunidades",
        )

        # 2. Descobre as rotas atuais de Caxias do Sul e Novo Hamburgo
        descobrir_rotas_home(
            client,
            "Caxias do Sul",
            "https://caxias.rs.gov.br/",
            ["/servicos", "/carta-de-servicos", "/secretarias/desenvolvimento-economico", "/noticias"],
        )
        descobrir_rotas_home(
            client,
            "Novo Hamburgo",
            "https://www.novohamburgo.rs.gov.br/",
            ["/servicos", "/sedec", "/noticias", "/trabalho"],
        )


if __name__ == "__main__":
    main()
