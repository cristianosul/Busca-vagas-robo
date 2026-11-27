#!/usr/bin/env python3
"""Autoteste local das regras municipais V3.1; não acessa a rede."""

from coletar_prefeituras_rs import (
    TARGETS, PLACEHOLDER_TERMS, host_allowed, clean,
)


def main():
    assert len(TARGETS) == 5
    by_city = {item["municipality"]: item for item in TARGETS}

    # Caxias: manter somente o portal raiz até confirmar URL específica válida.
    assert by_city["Caxias do Sul"]["url"] == "https://caxias.rs.gov.br/"
    assert "não foi validado" in by_city["Caxias do Sul"]["note"]

    assert by_city["Novo Hamburgo"]["url"].endswith(
        "/smdei/diretoria-trabalho/gerencia-trabalho"
    )
    assert by_city["Canoas"]["url"].startswith(
        "https://sistemas.canoas.rs.gov.br/bancodeoportunidades/"
    )
    assert by_city["Pelotas"]["url"].endswith("/oportunidades")

    # Bento: tentativas de conexão aumentadas no coletor; URL permanece oficial.
    assert by_city["Bento Gonçalves"]["url"] == "https://bentogoncalves.atende.net/"

    assert host_allowed(
        "https://sistemas.canoas.rs.gov.br/bancodeoportunidades/",
        "https://sistemas.canoas.rs.gov.br/bancodeoportunidades/",
    )
    assert not host_allowed(
        "https://site-falso.example/vagas",
        "https://canoas.rs.gov.br/",
    )

    # Bloqueia links de teste/homologação como os encontrados em Pelotas.
    assert PLACEHOLDER_TERMS.search("Concursos e estágios teste")
    assert PLACEHOLDER_TERMS.search("/oportunidades/selecao-teste")
    assert not PLACEHOLDER_TERMS.search("Oportunidades de trabalho")

    assert clean("  Gerência   do Trabalho ") == "Gerência do Trabalho"
    print("Autoteste municipal V3.1: OK")
    print("5 municípios; proteção contra links de teste; Caxias sem URL presumida.")


if __name__ == "__main__":
    main()
