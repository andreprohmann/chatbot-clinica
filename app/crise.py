"""Detecção de mensagens de risco (protocolo de crise)."""

import re
import unicodedata


def normalizar(texto: str) -> str:
    """Minúsculas e sem acentos, para 'SUICÍDIO', 'suicidio' e 'Suicídio' casarem com o mesmo padrão."""
    sem_acento = unicodedata.normalize("NFKD", texto)
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sem_acento.lower()).strip()


class DetectorCrise:
    def __init__(self, padroes: list[str]):
        self._regex = re.compile("|".join(f"(?:{p})" for p in padroes))

    def detectar(self, texto: str) -> bool:
        return bool(texto) and bool(self._regex.search(normalizar(texto)))
