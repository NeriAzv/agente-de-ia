"""Chama em lote uma lista de leads usando o fluxo oficial /chamar-lead.

Le os leads de `app/scripts/leads_para_chamar.json` — uma lista de objetos
`{"nome": ..., "numero": ..., "descricao": ...}` — e, para cada um, faz
POST em `/chamar-lead`. Ou seja: passa pelo MESMO caminho do disparo
individual (normalizacao do numero, guards de ja-abordado, checagem
phone-exists, geracao da abertura via IA, espelhamento no Supabase).

Dois modos
----------
--dry-run  (PADRAO) : nao envia nada. Normaliza o numero, consulta
                      phone-exists na Z-API e mostra quem receberia,
                      quem esta sem WhatsApp e quem tem numero invalido.
--enviar            : processa pra valer. Numero COM WhatsApp -> dispara
                      via /chamar-lead (intervalo de 10-15s entre envios).
                      Numero SEM WhatsApp -> registra o marcador
                      `numero_sem_whatsapp` no Supabase e nao envia.

Uso
---
    python app/scripts/chamar_lista_leads.py             # dry-run
    python app/scripts/chamar_lista_leads.py --enviar    # dispara pra valer

SOMENTE o modo --enviar escreve/dispara algo. O dry-run e read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "app"
ENV_PATH = APP_DIR / ".env"
LEADS_FILE = Path(__file__).resolve().parent / "leads_para_chamar.json"

# Reaproveita modulos oficiais do projeto, importando-os direto (sem carregar
# o pacote `agent` inteiro, que puxaria langchain/openai).
sys.path.insert(0, str(APP_DIR / "agent"))
sys.path.insert(0, str(APP_DIR / "agent" / "storage"))
from normalizers import normalizar_telefone  # noqa: E402
import supabase_repo  # noqa: E402

CHAMAR_LEAD_URL = "http://localhost:5001/chamar-lead"
DELAY_MIN, DELAY_MAX = 10.0, 15.0

ZAPI_INSTANCE = "3EFABF270A13919DE95AD6F4B026E35E"
ZAPI_TOKEN = "E1014DD5B87C05F2DF8F2547"
ZAPI_BASE = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}"


def carregar_env(path: Path) -> None:
    """Carrega KEY=VALUE de um .env para os.environ (sem sobrescrever)."""
    if not path.exists():
        return
    for linha in path.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave = chave.strip()
        if chave and chave not in os.environ:
            os.environ[chave] = valor.strip().strip('"').strip("'")


carregar_env(ENV_PATH)
ZAPI_SEC_TOKEN = os.environ.get("ZAPI_SEC_TOKEN", "")


def carregar_leads() -> list[dict]:
    """Le e valida a lista de leads do arquivo JSON.

    Returns:
        Lista de dicts com pelo menos `nome` e `numero`. `descricao` e
        opcional (string vazia quando ausente).
    """
    if not LEADS_FILE.exists():
        raise SystemExit(f"Arquivo nao encontrado: {LEADS_FILE}\n"
                         f"Crie o arquivo com a lista de leads antes de rodar.")
    try:
        dados = json.loads(LEADS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"JSON invalido em {LEADS_FILE}: {e}")
    if not isinstance(dados, list):
        raise SystemExit("O arquivo de leads deve conter uma LISTA de objetos.")

    leads = []
    for i, item in enumerate(dados, 1):
        if not isinstance(item, dict) or not item.get("nome") or not item.get("numero"):
            raise SystemExit(f"Lead #{i} invalido: precisa de 'nome' e 'numero'.")
        leads.append({
            "nome": str(item["nome"]).strip(),
            "numero": str(item["numero"]).strip(),
            "descricao": str(item.get("descricao", "")).strip(),
        })
    return leads


def phone_exists(phone: str) -> str:
    """Consulta phone-exists na Z-API. Returns: 'sim' | 'nao' | 'erro'."""
    if not ZAPI_SEC_TOKEN:
        return "erro"
    try:
        r = requests.get(
            f"{ZAPI_BASE}/phone-exists/{phone}",
            headers={"client-token": ZAPI_SEC_TOKEN},
            timeout=20,
        )
        if r.status_code != 200:
            return "erro"
        body = r.json()
    except (requests.RequestException, ValueError):
        return "erro"
    if not isinstance(body, dict) or "exists" not in body:
        return "erro"
    return "sim" if body.get("exists") else "nao"


def registrar_fantasma_supabase(numero_norm: str) -> bool:
    """Grava no Supabase o marcador `numero_sem_whatsapp` para um numero que
    nao tem conta de WhatsApp.

    Reaproveita o `supabase_repo` oficial: garante a conversa e insere um
    evento com `payload.type = 'numero_sem_whatsapp'`. O messageId e
    deterministico (`sysmark:no-wa:{chatLid}`), entao re-registros do mesmo
    numero fazem upsert na mesma linha — nunca duplica.

    Returns:
        True se registrou sem erro; False caso contrario.
    """
    chat_lid = f"{numero_norm}@lid"
    evento = {
        "timestamp": time.time(),
        "data": {
            "fromMe": True,
            "phone": numero_norm,
            "chatLid": chat_lid,
            "type": "numero_sem_whatsapp",
            "messageId": f"sysmark:no-wa:{chat_lid}",
            "text": {"message": "[sistema] Numero sem conta de WhatsApp — "
                                "nao chamado (verificado em lote)."},
        },
    }
    try:
        supabase_repo.upsert_conversation(chat_lid, phone=numero_norm)
        supabase_repo.append_message(chat_lid, evento)
        return True
    except Exception as e:
        print(f"      ERRO ao registrar fantasma no Supabase: {e}")
        return False


def dry_run(leads: list[dict]) -> None:
    """Valida a lista sem enviar nada."""
    print(f"DRY-RUN — {len(leads)} leads. Nada sera enviado.\n")
    receberia = invalidos = sem_wa = erros = 0

    for i, lead in enumerate(leads, 1):
        norm = normalizar_telefone(lead["numero"])
        if not norm:
            invalidos += 1
            print(f"[{i:>3}] {lead['nome'][:30]:<30} {lead['numero']:<20} NUMERO INVALIDO")
            continue
        existe = phone_exists(norm)
        if existe == "sim":
            receberia += 1
            tag = "RECEBERIA"
        elif existe == "nao":
            sem_wa += 1
            tag = "SEM WHATSAPP (sera registrado no Supabase, nao enviado)"
        else:
            erros += 1
            tag = "ERRO ao checar phone-exists"
        print(f"[{i:>3}] {lead['nome'][:30]:<30} {lead['numero']:<20} -> {norm:<16} {tag}")
        time.sleep(0.25)

    print("\n" + "=" * 60)
    print(f"  Receberia a abertura : {receberia}")
    print(f"  Sem WhatsApp         : {sem_wa}")
    print(f"  Numero invalido      : {invalidos}")
    print(f"  Erro na checagem     : {erros}")
    print("=" * 60)
    print("\nSe estiver ok, rode de novo com:  --enviar")


def enviar(leads: list[dict]) -> None:
    """Processa a lista:

    - número inválido      -> pula.
    - número SEM WhatsApp   -> registra o marcador `numero_sem_whatsapp` no
                               Supabase e pula (não chama o endpoint).
    - número COM WhatsApp   -> dispara via POST /chamar-lead (fluxo oficial).

    O intervalo de 10-15s é aplicado apenas após um envio real — números
    fantasma não mandam mensagem, então não precisam de espaço anti-spam.
    """
    print(f"ENVIO REAL — {len(leads)} leads\n")
    contagem: dict[str, int] = {}
    fantasmas: list[str] = []

    def conta(chave: str) -> None:
        contagem[chave] = contagem.get(chave, 0) + 1

    for i, lead in enumerate(leads, 1):
        rotulo = f"[{i}/{len(leads)}] {lead['nome']}"
        norm = normalizar_telefone(lead["numero"])

        if not norm:
            conta("numero_invalido")
            print(f"{rotulo}: NÚMERO INVÁLIDO ({lead['numero']}) — pulado")
            continue

        if phone_exists(norm) == "nao":
            ok = registrar_fantasma_supabase(norm)
            conta("fantasma_registrado" if ok else "fantasma_erro_registro")
            fantasmas.append(norm)
            print(f"{rotulo}: SEM WHATSAPP -> registrado no Supabase (numero_sem_whatsapp)")
            continue  # não houve envio: sem intervalo anti-spam

        # Tem WhatsApp (ou phone-exists falhou: fail-open) -> fluxo oficial.
        try:
            resp = requests.post(
                CHAMAR_LEAD_URL,
                json={"nome": lead["nome"], "numero": lead["numero"],
                      "descricao": lead["descricao"]},
                timeout=60,
            )
        except requests.ConnectionError:
            raise SystemExit(
                f"{rotulo}: não consegui conectar em {CHAMAR_LEAD_URL}.\n"
                f"O servidor Flask (app) precisa estar rodando na porta 5001."
            )
        except requests.RequestException as e:
            conta("erro_request")
            print(f"{rotulo}: ERRO de request -> {e}")
        else:
            try:
                body = resp.json()
            except ValueError:
                body = {"raw": resp.text[:200]}
            status = body.get("status", "?")
            reason = body.get("reason") or body.get("message") or ""
            conta(f"{resp.status_code}/{status}")
            print(f"{rotulo}: HTTP {resp.status_code} {status} {('- ' + reason) if reason else ''}")

        if i < len(leads):
            espera = random.uniform(DELAY_MIN, DELAY_MAX)
            print(f"      aguardando {espera:.1f}s ...")
            time.sleep(espera)

    print("\n" + "=" * 60)
    print("RESUMO DO ENVIO")
    print("=" * 60)
    for chave, qtd in sorted(contagem.items()):
        print(f"  {chave:<28} {qtd}")
    if fantasmas:
        print(f"\n  Fantasmas registrados no Supabase ({len(fantasmas)}):")
        for n in fantasmas:
            print(f"    {n}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Chama uma lista de leads via /chamar-lead.")
    parser.add_argument("--enviar", action="store_true",
                        help="Dispara de verdade. Sem esta flag, roda em dry-run.")
    args = parser.parse_args()

    leads = carregar_leads()
    if not leads:
        raise SystemExit("Lista de leads vazia.")

    if args.enviar:
        enviar(leads)
    else:
        dry_run(leads)


if __name__ == "__main__":
    main()
