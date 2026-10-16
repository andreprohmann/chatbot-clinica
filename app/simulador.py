"""Converse com o bot pelo terminal, sem WhatsApp (o CRM simulado precisa estar rodando).

Uso:  python -m app.simulador [telefone]
      Digite o texto, ou o número de uma opção. Ctrl+C para sair.

Telefone padrão: 5511999990000 (paciente "Joana", já cadastrada no CRM simulado).
Use outro número para testar como paciente novo.
"""

import asyncio
import itertools
import sys
import tempfile
from pathlib import Path

from .acoes import ACOES
from .config import Config
from .crm import ClienteCRM
from .db import Banco
from .fluxo import carregar
from .motor import Motor
from .whatsapp import ClienteWhatsAppDev, Entrada


async def main(telefone: str) -> None:
    config = Config()
    whatsapp = ClienteWhatsAppDev()
    banco = Banco(str(Path(tempfile.mkdtemp()) / "simulador.db"))
    motor = Motor(config, banco, whatsapp, ClienteCRM(config.crm_url, config.crm_token),
                  carregar(config.fluxo_path, set(ACOES)))
    ids = itertools.count(1)
    lidas = 0
    opcoes = []
    print(f"Simulando o telefone {telefone}. CRM em {config.crm_url}. Digite algo para começar.\n")
    while True:
        try:
            texto = await asyncio.to_thread(input, "você> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        entrada = Entrada(wa_message_id=f"sim.{next(ids)}", telefone=telefone, nome_contato="Simulador",
                          tipo="texto", texto=texto)
        if texto.strip().isdigit() and 1 <= int(texto) <= len(opcoes):  # simula o toque no botão
            escolhida = opcoes[int(texto) - 1]
            entrada.tipo, entrada.texto, entrada.opcao_id = "opcao", escolhida.titulo, escolhida.id
        await motor.processar(entrada)
        for m in whatsapp.enviadas[lidas:]:
            print("\nbot> " + m["texto"].replace("\n", "\n     "))
            for i, o in enumerate(m["opcoes"], 1):
                print(f"     [{i}] {o.titulo}" + (f" – {o.descricao}" if o.descricao else ""))
            opcoes = m["opcoes"] or opcoes
        lidas = len(whatsapp.enviadas)
        c = banco.conversa_ativa(telefone)
        if c and c["estado"] != "bot":
            print(f"\n     (conversa com a equipe: {c['estado']}, motivo {c['motivo_atendimento']})")
        print()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "5511999990000"))
