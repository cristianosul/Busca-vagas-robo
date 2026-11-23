
from __future__ import annotations

import asyncio
import logging
import sys

from main import init_db, import_fgtas_jobs, run_crawl


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )

    # O coletor atual utiliza este logger.
    logger = logging.getLogger("uvicorn.error")
    logger.handlers = logging.getLogger().handlers
    logger.setLevel(logging.INFO)
    logger.propagate = False


def main() -> int:
    configure_logging()
    logger = logging.getLogger("busca_vagas.runner")

    try:
        logger.info("ROBÔ 1: iniciando execução independente.")

        logger.info("Verificando e inicializando o banco Neon.")
        init_db()
        logger.info("Banco inicializado com sucesso.")

        logger.info("Iniciando importação FGTAS/SINE.")
        imported = import_fgtas_jobs()

        if imported.get("skipped"):
            raise RuntimeError(
                "Importação FGTAS/SINE não executada: "
                + "; ".join(imported.get("errors", []))
            )

        logger.info(
            "FGTAS/SINE: registros lidos=%s; novas vagas=%s; erros=%s",
            imported.get("jobs_seen", 0),
            imported.get("jobs_added", 0),
            len(imported.get("errors", [])),
        )

        for error in imported.get("errors", []):
            logger.warning("FGTAS/SINE: %s", error)

        logger.info("Iniciando coleta nacional.")
        result = asyncio.run(run_crawl())

        if result.get("skipped"):
            raise RuntimeError(
                "Coleta não executada: "
                + "; ".join(result.get("errors", []))
            )

        logger.info(
            "COLETA CONCLUÍDA: fontes=%s; vagas encontradas=%s; "
            "novas=%s; duplicadas=%s; expiradas=%s; erros=%s",
            result.get("sources_checked", 0),
            result.get("jobs_seen", 0),
            result.get("jobs_added", 0),
            result.get("duplicates", 0),
            result.get("expired_jobs", 0),
            len(result.get("errors", [])),
        )

        for error in result.get("errors", []):
            logger.warning("COLETA: %s", error)

        logger.info("ROBÔ 1: execução finalizada.")
        return 0

    except Exception:
        logger.exception("ROBÔ 1: falha na execução.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
