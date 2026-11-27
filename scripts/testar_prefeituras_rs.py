#!/usr/bin/env python3
"""Valida alvos oficiais e regras específicas das prefeituras do RS via Tailscale BR."""

import re
import time
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

# Substitua pelas URLs oficiais confirmadas de Caxias do Sul e Novo Hamburgo
URL_CAXIAS_FGTAS_SINE = "https://caxias.rs.gov.br/"
URL_NOVO_HAMBURGO_FGTAS_SINE = "https://www.novohamburgo.rs.gov.br/"
URL_BENTO_GONCALVES = "https://bentogoncalves.atende.net/cidadao/pagina/balcao-de-empregos"
URL_CANOAS_BANCO = "https://canoas.rs.gov.br/servicos/banco-de-oportunidades/"
URL_PELOTAS_OPORTUNIDADES = "https://www.pelotas.com.br/oportunidades"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

REGEX_SELECAO_PUBLICA = re.compile(
    r"(concurso|sele[cç][aã]o\s+p[uú]blica|processo\s+seletivo|pss|edital|est[aá]gio)",
    re.I,
)
REGEX_VAGA_PRIVADA = re.compile(
    r"(vaga|emprego|balc[aã]o|sine|trabalhador|curr[ií]culo|desenvolvimento\s+econ[oô]mico|sala\s+do\s+empreendedor)",
    re.I,
)


def clean(txt: str) -> str:
    return re.sub(r"\s+", " ", txt or "").strip()


def fetch_html(client: httpx.Client, url: str) -> httpx.Response:
    return client.get(url)


def checar_pagina_padrao(client: httpx.Client, cidade: str, url: str) -> None:
    print(f"\n=== {cidade} ===")
    print(f"URL: {url}")
    try:
        resp = fetch_html(client, url)
        print(f"HTTP {resp.status_code} | Final: {resp.url} | Bytes: {len(resp.content)}")
        if resp.status_code != 200:
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        titulo = clean(soup.title.get_text() if soup.title else "")
        print(f"Título: {titulo[:90]}")

        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        texto = clean(soup.get_text(" ", strip=True))
        m_vagas = re.findall(r".{0,45}\b\d{1,4}\s+vagas?\b.{0,45}", texto, re.I)
        if m_vagas:
            print("Trechos com contagem de vagas:")
            for trecho in m_vagas[:5]:
                print(f"  * {trecho}")

        links = []
        vistos = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(str(resp.url), a["href"].strip())
            label = clean(a.get_text(" ", strip=True))
            if not href.startswith("http") or href in vistos:
                continue
            vistos.add(href)
            if REGEX_VAGA_PRIVADA.search(label) or REGEX_VAGA_PRIVADA.search(href):
                links.append((label or "(sem texto)", href))

        print(f"Links relacionados ({len(links)}):")
        for label, href in links[:12]:
            print(f"  - {label[:65]} -> {href}")
    except Exception as exc:
        print(f"ERRO: {type(exc).__name__}: {exc}")


def testar_bento_repetidas(url: str, tentativas: int = 3, pausa_seg: float = 2.0) -> None:
    print("\n=== Bento Gonçalves (múltiplas tentativas) ===")
    print(f"URL: {url}")
    resultados = []

    for i in range(1, tentativas + 1):
        inicio = time.monotonic()
        try:
            with httpx.Client(headers=HEADERS, timeout=15.0, follow_redirects=True) as client:
                resp = client.get(url)
            duracao = time.monotonic() - inicio
            status_str = f"HTTP {resp.status_code} em {duracao:.2f}s"
            resultados.append(("ok", status_str))
            print(f"  Tentativa {i}/{tentativas}: {status_str} (final: {resp.url})")
        except Exception as exc:
            duracao = time.monotonic() - inicio
            status_str = f"{type(exc).__name__} em {duracao:.2f}s"
            resultados.append(("falha", status_str))
            print(f"  Tentativa {i}/{tentativas}: FALHA ({status_str}: {exc})")

        if i < tentativas:
            time.sleep(pausa_seg)

    falhas = sum(1 for tipo, _ in resultados if tipo == "falha")
    if falhas == tentativas:
        print("Diagnóstico: timeout/falha consistente em todas as tentativas (indisponível ou bloqueio de rede/IP).")
    elif falhas > 0:
        print(f"Diagnóstico: instabilidade intermitente ({tentativas - falhas}/{tentativas} responderam).")
    else:
        print("Diagnóstico: respondeu em todas as tentativas.")


def checar_canoas_anuncios(client: httpx.Client, url: str) -> None:
    print("\n=== Canoas (Banco de Oportunidades) ===")
    print(f"URL: {url}")
    try:
        resp = fetch_html(client, url)
        print(f"HTTP {resp.status_code} | Final: {resp.url}")
        if resp.status_code != 200:
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        cards = soup.find_all(["article", "li"], class_=re.compile(r"(vaga|oportunidade|card|item|noticia)", re.I))
        indicios_anuncio = re.findall(
            r"(sal[aá]rio|carga\s+hor[aá]ria|requisitos?|escolaridade|benef[ií]cios?|como\s+se\s+candidatar)",
            clean(soup.get_text(" ", strip=True)),
            re.I,
        )

        print(f"Elementos candidatos a card/item: {len(cards)}")
        print(f"Indícios de campos de anúncio individual no texto: {len(indicios_anuncio)}")

        vistos = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(str(resp.url), a["href"].strip())
            label = clean(a.get_text(" ", strip=True))
            if href in vistos or not href.startswith("http"):
                continue
            vistos.add(href)
            if REGEX_VAGA_PRIVADA.search(label) or REGEX_VAGA_PRIVADA.search(href):
                print(f"  - {label[:65] or '(sem texto)'} -> {href}")
    except Exception as exc:
        print(f"ERRO: {type(exc).__name__}: {exc}")


def checar_pelotas_separado(client: httpx.Client, url: str) -> None:
    print("\n=== Pelotas (Seleções Públicas vs. Vagas Privadas) ===")
    print(f"URL: {url}")
    try:
        resp = fetch_html(client, url)
        print(f"HTTP {resp.status_code} | Final: {resp.url}")
        if resp.status_code != 200:
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        selecoes_publicas = []
        vagas_privadas = []
        vistos = set()

        for a in soup.find_all("a", href=True):
            href = urljoin(str(resp.url), a["href"].strip())
            label = clean(a.get_text(" ", strip=True))
            alvo_texto = f"{label} {href}"
            if href in vistos or not href.startswith("http"):
                continue
            vistos.add(href)

            if REGEX_SELECAO_PUBLICA.search(alvo_texto):
                selecoes_publicas.append((label or "(sem texto)", href))
            elif REGEX_VAGA_PRIVADA.search(alvo_texto):
                vagas_privadas.append((label or "(sem texto)", href))

        print(f"Seleções públicas / concursos / estágios ({len(selecoes_publicas)}):")
        for label, href in selecoes_publicas:
            print(f"  [PÚBLICA] {label[:65]} -> {href}")

        print(f"Vagas privadas / emprego / desenvolvimento ({len(vagas_privadas)}):")
        for label, href in vagas_privadas:
            print(f"  [PRIVADA] {label[:65]} -> {href}")
    except Exception as exc:
        print(f"ERRO: {type(exc).__name__}: {exc}")


def main() -> None:
    with httpx.Client(headers=HEADERS, timeout=25.0, follow_redirects=True) as client:
        checar_pagina_padrao(client, "Caxias do Sul (FGTAS/SINE)", URL_CAXIAS_FGTAS_SINE)
        checar_pagina_padrao(client, "Novo Hamburgo (FGTAS/SINE)", URL_NOVO_HAMBURGO_FGTAS_SINE)
        checar_canoas_anuncios(client, URL_CANOAS_BANCO)
        checar_pelotas_separado(client, URL_PELOTAS_OPORTUNIDADES)

    testar_bento_repetidas(URL_BENTO_GONCALVES, tentativas=3, pausa_seg=2.0)


if __name__ == "__main__":
    main()
