#!/usr/bin/env python3
"""Autoteste das regras municipais e do parser PDF V3.5; não acessa a rede."""

from datetime import date
from coletar_prefeituras_rs import (
    TARGETS, PLACEHOLDER_TERMS, host_allowed, clean,
    parse_job_records_from_text, canonical_url, apply_publication_date_guard,
)


def main():
    assert len(TARGETS) == 5
    by_city = {item["municipality"]: item for item in TARGETS}
    caxias = by_city["Caxias do Sul"]
    assert caxias["url"].endswith("/vagas-de-emprego-fgtas-sine")
    assert caxias["expected"] == "employment_service"
    assert by_city["Novo Hamburgo"]["url"].endswith("/smdei/diretoria-trabalho/gerencia-trabalho")
    assert by_city["Canoas"]["url"].startswith("https://sistemas.canoas.rs.gov.br/bancodeoportunidades/")
    assert by_city["Pelotas"]["url"].endswith("/oportunidades")
    bento = by_city["Bento Gonçalves"]
    assert bento["url"].endswith("/cidadao/pagina/sala-do-empreendedor")
    assert "https://bentogoncalves.atende.net/" in bento["fallback_urls"]
    assert "https://www.bentogoncalves.rs.gov.br/" in bento["fallback_urls"]
    assert host_allowed("https://gcpstorage.caxias.rs.gov.br/documents/painel.pdf", caxias["url"])
    assert not host_allowed("https://site-falso.example/vagas", "https://canoas.rs.gov.br/")
    assert PLACEHOLDER_TERMS.search("Concursos e estágios teste")
    assert PLACEHOLDER_TERMS.search("/oportunidades/selecao-teste")
    assert not PLACEHOLDER_TERMS.search("Oportunidades de trabalho")
    assert clean("  Gerência   do Trabalho ") == "Gerência do Trabalho"

    # URL original e link com query de download precisam resultar na mesma identidade.
    base = "https://gcpstorage.caxias.rs.gov.br/documents/painel.pdf"
    variant = base + "?download=PAINEL%20PDF.pdf#viewer"
    assert canonical_url(base) == canonical_url(variant) == base

    sample = (
        "Painel FGTAS\nCódigo 1234567 - Auxiliar de produção - salário R$ 1.800,00\n"
        "1234567 - duplicado\n7654321 – Operador de caixa"
    )
    records = parse_job_records_from_text(
        sample, "Painel teste", base, "2026-11-25"
    )
    assert [item["source_id"] for item in records] == ["1234567", "7654321"]
    assert records[0]["availability_status"] == "needs_confirmation"
    assert records[0]["is_current_opportunity"] is False
    assert records[0]["publication_date"] == "2026-11-25"

    # Regra temporal: data posterior ao dia de referência deve ser bloqueada.
    reference_day = date(2026, 11, 28)
    future_doc = {"publication_date": "2026-11-30"}
    future_records = [{"source_id": "1234567", "is_current_opportunity": True}]
    future_doc, future_records = apply_publication_date_guard(
        future_doc, future_records, reference_day
    )
    assert future_doc["publication_date_status"] == "future_date_blocked"
    assert future_records[0]["availability_status"] == "future_publication_date_needs_review"
    assert future_records[0]["is_current_opportunity"] is False

    missing_doc = {"publication_date": None}
    missing_records = [{"source_id": "7654321"}]
    missing_doc, missing_records = apply_publication_date_guard(
        missing_doc, missing_records, reference_day
    )
    assert missing_doc["publication_date_status"] == "missing_date_needs_review"
    assert missing_records[0]["availability_status"] == "publication_date_needs_review"
    assert missing_records[0]["is_current_opportunity"] is False

    # Diferença entre total anunciado e códigos extraídos permanece explícita.
    # Regressão do resultado recebido: 140 anunciadas, 71 identificadores únicos extraídos.
    declared_total, unique_codes = 140, 71
    assert declared_total - unique_codes == 69
    assert declared_total != unique_codes

    print("Autoteste municipal V3.5: OK")
    print("URLs canônicas, códigos únicos, divergência de totais, data futura, fallback de Bento e filtros conferidos.")


if __name__ == "__main__":
    main()
