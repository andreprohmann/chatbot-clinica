"""CRM simulado: implementa o contrato de API que o bot usa e uma tela de atendimento de referência.

Uso:  python3 -u mock-crm/servidor.py          (porta 8000)
      Tela de atendimento: http://localhost:8000/atendimento

Variáveis: CRM_TOKEN (token que o bot usa para chamar este CRM), BOT_URL e BOT_API_TOKEN
(para a tela de atendimento chamar o bot). Dados ficam só em memória.
"""

import json
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("CRM_TOKEN", "TROQUE-PELO-TOKEN-DA-API")
BOT_URL = os.environ.get("BOT_URL", "http://localhost:8001").rstrip("/")
BOT_API_TOKEN = os.environ.get("BOT_API_TOKEN", "TROQUE-PELO-TOKEN-DO-BOT")
TZ = ZoneInfo("America/Sao_Paulo")
DIAS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
TELA = Path(__file__).with_name("atendimento.html")

PROFISSIONAIS = [
    {"id": "p1", "nome": "Dra. Ana Souza", "modalidades": {"Presencial", "Online"}},
    {"id": "p2", "nome": "Dr. Bruno Lima", "modalidades": {"Presencial"}},
    {"id": "p3", "nome": "Dra. Carla Reis", "modalidades": {"Online"}},
]
HORAS_POR_PERIODO = {"Manhã": [8, 9, 10, 11], "Tarde": [14, 15, 16, 17], "Noite": [18, 19, 20]}

# Estado em memória (os testes inspecionam e reiniciam).
estado: dict = {}
forcar_expediente: dict = {"aberto": None}  # None = usa o relógio; True/False = fixo (testes)


def resetar() -> None:
    estado.clear()
    estado.update(
        pacientes={
            # Paciente de exemplo, já cadastrado e com consentimento.
            "5511999990000": {"id": "pac-1", "nome": "Joana Martins", "primeiro_nome": "Joana",
                              "consentimento": True, "ultimo_profissional_id": "p1"},
        },
        agendamentos={}, ocupados=set(), lista_espera=[], atendimentos=[], alertas=[], eventos=[],
        fora_do_ar=False,
    )
    forcar_expediente["aberto"] = None


resetar()


def label_horario(inicio: datetime) -> str:
    return f"{DIAS[inicio.weekday()]} {inicio:%d/%m} às {inicio:%H}h"


def gerar_horarios(periodo: str, modalidade: str, profissional_id: str) -> list[dict]:
    profs = [p for p in PROFISSIONAIS
             if (not profissional_id or p["id"] == profissional_id)
             and (modalidade in ("", "Tanto faz") or modalidade in p["modalidades"])]
    hoje = datetime.now(TZ).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    slots = []
    for dia in range(1, 15):
        data = hoje + timedelta(days=dia)
        if data.weekday() >= 5:
            continue
        for hora in HORAS_POR_PERIODO.get(periodo, []):
            for prof in profs:
                inicio = data.replace(hour=hora)
                sid = f"{prof['id']}-{inicio:%Y%m%d%H}"
                if sid in estado["ocupados"] or (dia + hora + int(prof["id"][1])) % 3 == 0:
                    continue  # simula agenda parcialmente cheia
                slots.append({"id": sid, "label": label_horario(inicio), "profissional": prof["nome"]})
                if len(slots) == 8:
                    return slots
    return slots


def _slot(sid: str) -> tuple[dict, datetime]:
    prof_id, stamp = sid.split("-")
    return next(p for p in PROFISSIONAIS if p["id"] == prof_id), datetime.strptime(stamp, "%Y%m%d%H")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, data=None, tipo="application/json; charset=utf-8", bruto: bytes | None = None):
        body = bruto if bruto is not None else json.dumps(data or {}, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _rota(self):
        url = urlparse(self.path)
        return [p for p in url.path.split("/") if p], {k: v[0] for k, v in parse_qs(url.query).items()}

    def _api_ok(self) -> bool:
        if estado["fora_do_ar"]:
            self._send(503, {"erro": "CRM fora do ar (simulado)"})
            return False
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"erro": "token inválido"})
            return False
        return True

    # --------------------------------------------------------- tela de atendimento (referência)

    def _tela(self, metodo: str, partes: list[str]) -> None:
        if partes == ["atendimento"]:
            return self._send(200, tipo="text/html; charset=utf-8", bruto=TELA.read_bytes())
        # /atendimento/api/... -> repassa ao bot com o token. O token fica no servidor do CRM,
        # nunca no navegador: é assim que o CRM real deve fazer.
        destino = BOT_URL + "/api/atendimento/" + "/".join(partes[2:])
        if urlparse(self.path).query:
            destino += "?" + urlparse(self.path).query
        corpo = json.dumps(self._body()).encode() if metodo == "POST" else None
        req = urllib.request.Request(destino, data=corpo, method=metodo, headers={
            "Authorization": f"Bearer {BOT_API_TOKEN}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                self._send(r.status, bruto=r.read())
        except urllib.error.HTTPError as e:
            self._send(e.code, bruto=e.read())
        except OSError as e:
            self._send(502, {"detail": f"bot indisponível: {e}"})

    # --------------------------------------------------------- API usada pelo bot

    def do_GET(self):
        partes, q = self._rota()
        if partes[:1] == ["atendimento"]:
            return self._tela("GET", partes)
        if not self._api_ok():
            return
        partes = [p for p in partes if p != "api"]

        if partes == ["pacientes"]:
            p = estado["pacientes"].get(q.get("telefone", "")) or {}
            return self._send(200, {**p, "encontrado": bool(p.get("id")), "consentimento": bool(p.get("consentimento"))})

        if partes == ["expediente"]:
            if forcar_expediente["aberto"] is not None:
                return self._send(200, {"aberto": forcar_expediente["aberto"]})
            agora = datetime.now(TZ)
            return self._send(200, {"aberto": agora.weekday() < 5 and 8 <= agora.hour < 19})

        if partes == ["profissionais"]:
            mod = q.get("modalidade", "")
            profs = [p for p in PROFISSIONAIS if mod in ("", "Tanto faz") or mod in p["modalidades"]]
            return self._send(200, {"ids": [p["id"] for p in profs], "nomes": [p["nome"] for p in profs]})

        if partes == ["agenda", "horarios"]:
            slots = gerar_horarios(q.get("periodo", ""), q.get("modalidade", ""), q.get("profissional_id", ""))
            return self._send(200, {"total": len(slots), "ids": [s["id"] for s in slots],
                                    "labels": [s["label"] for s in slots],
                                    "profissionais": [s["profissional"] for s in slots]})

        if partes == ["agendamentos"]:
            meus = [a for a in estado["agendamentos"].values()
                    if a["telefone"] == q.get("telefone", "") and a["status"] == "agendado"]
            return self._send(200, {"total": len(meus), "ids": [a["id"] for a in meus],
                                    "labels": [a["label"] for a in meus],
                                    "descricoes": [a["profissional"] for a in meus]})

        self._send(404, {"erro": "rota não encontrada"})

    def do_POST(self):
        partes, _ = self._rota()
        if partes[:1] == ["atendimento"]:
            return self._tela("POST", partes)
        if not self._api_ok():
            return
        partes = [p for p in partes if p != "api"]
        body = self._body()

        if partes == ["consentimentos"]:
            p = estado["pacientes"].setdefault(body.get("telefone", ""), {})
            p["consentimento"] = True
            return self._send(201, {"ok": True})

        if partes == ["agendamentos"]:
            sid = body.get("horario_id", "")
            if not sid or sid in estado["ocupados"]:
                return self._send(409, {"erro": "horário indisponível"})
            tel = body.get("telefone", "")
            pac = estado["pacientes"].setdefault(tel, {"consentimento": True})
            if not pac.get("id"):
                nome = body.get("responsavel_nome") or body.get("paciente_nome") or ""
                pac.update(id=f"pac-{uuid.uuid4().hex[:6]}", nome=nome, primeiro_nome=nome.split(" ")[0])
            prof, inicio = _slot(sid)
            pac["ultimo_profissional_id"] = prof["id"]
            ag_id = f"ag-{uuid.uuid4().hex[:6]}"
            estado["agendamentos"][ag_id] = {"id": ag_id, "telefone": tel, "horario_id": sid, "status": "agendado",
                                             "label": label_horario(inicio), "profissional": prof["nome"], "dados": body}
            estado["ocupados"].add(sid)
            print(f"[agendamento] {ag_id} {body}")
            return self._send(201, {"id": ag_id})

        if len(partes) == 3 and partes[0] == "agendamentos" and partes[2] in ("cancelar", "remarcar"):
            ag = estado["agendamentos"].get(partes[1])
            if not ag or ag["status"] != "agendado":
                return self._send(404, {"erro": "agendamento não encontrado"})
            if partes[2] == "cancelar":
                estado["ocupados"].discard(ag["horario_id"])
                ag["status"] = "cancelado"
                print(f"[cancelamento] {ag['id']}")
                return self._send(200, {"ok": True})
            novo = body.get("horario_id", "")
            if not novo or novo in estado["ocupados"]:
                return self._send(409, {"erro": "horário indisponível"})
            estado["ocupados"].discard(ag["horario_id"])
            prof, inicio = _slot(novo)
            ag.update(horario_id=novo, label=label_horario(inicio), profissional=prof["nome"])
            estado["ocupados"].add(novo)
            print(f"[remarcação] {ag['id']} -> {novo}")
            return self._send(200, {"ok": True})

        if partes == ["lista-espera"]:
            estado["lista_espera"].append(body)
            print(f"[lista de espera] {body}")
            return self._send(201, {"ok": True})

        if partes == ["atendimento-humano"]:
            estado["atendimentos"].append(body)
            print(f"[atendimento solicitado] {body}")
            return self._send(201, {"ok": True})

        if partes == ["alertas-crise"]:
            estado["alertas"].append(body)
            print("\n" + "!" * 60 + f"\n[ALERTA DE CRISE] {body}\n" + "!" * 60 + "\n")
            return self._send(201, {"ok": True})

        if partes == ["eventos-whatsapp"]:
            estado["eventos"].append(body)
            return self._send(202, {"ok": True})

        self._send(404, {"erro": "rota não encontrada"})

    def log_message(self, fmt, *args):
        if "/atendimento/api/" not in self.path:  # a tela consulta a cada poucos segundos
            print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")


def iniciar(porta: int = 0) -> ThreadingHTTPServer:
    """Sobe o servidor em uma thread (usado pelos testes). porta=0 escolhe uma livre."""
    servidor = ThreadingHTTPServer(("127.0.0.1", porta), Handler)
    Thread(target=servidor.serve_forever, daemon=True).start()
    return servidor


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", "8000"))
    print(f"CRM simulado em http://localhost:{porta}  |  tela de atendimento: http://localhost:{porta}/atendimento")
    ThreadingHTTPServer(("0.0.0.0", porta), Handler).serve_forever()
