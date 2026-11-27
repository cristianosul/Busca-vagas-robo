#!/usr/bin/env python3
"""Autoteste local das regras do diagnóstico municipal; não acessa a rede."""

from coletar_prefeituras_rs import TARGETS, host_allowed


def main():
    assert len(TARGETS) == 5
    by_city = {item["municipality"]: item for item in TARGETS}
    assert by_city["Novo Hamburgo"]["url"].endswith(
        "/smdei/diretoria-trabalho/gerencia-trabalho"
    )
    assert by_city["Canoas"]["url"].startswith(
        "https://sistemas.canoas.rs.gov.br/bancodeoportunidades/"
    )
    assert by_city["Pelotas"]["url"].endswith("/oportunidades")
    assert host_allowed(
        "https://sistemas.canoas.rs.gov.br/bancodeoportunidades/",
        "https://sistemas.canoas.rs.gov.br/bancodeoportunidades/",
    )
    assert not host_allowed(
        "https://site-falso.example/vagas",
        "https://canoas.rs.gov.br/",
    )
    print("Autoteste municipal V3: OK")
    print("5 municípios configurados; URLs e validação de domínio conferidas.")


if __name__ == "__main__":
    main()
