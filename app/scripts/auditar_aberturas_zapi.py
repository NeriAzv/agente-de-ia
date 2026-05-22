"""Auditoria: confere contra a Z-API quais aberturas registradas no banco
foram REALMENTE enviadas pelo SDR.

Contexto
--------
Um bug antigo (corrigido no commit 088fbd3b, 2026-05-19) gravava o evento
`abertura` em `messages` mesmo quando o envio pela Z-API falhava. Resultado:
o banco marca leads como "chamados" que nunca receberam mensagem.

Metodo (compativel com WhatsApp Multi-Device)
---------------------------------------------
O endpoint `/chat-messages` nao funciona em multi-device. Em vez dele:

  1. `/chats`            -> lista TODA conversa que existe na instancia.
  2. `phone-exists/{n}`  -> diz se o numero tem WhatsApp + retorna o LID real.

Veredito por abertura:
  - numero sem WhatsApp (exists=false)          -> FANTASMA (impossivel ter enviado)
  - tem WhatsApp E existe conversa na instancia -> ENVIOU
  - tem WhatsApp mas SEM conversa na instancia  -> FANTASMA (envio falhou/nao ocorreu)

Saida
-----
- Imprime as listas: ENVIOU x FANTASMA x ERRO.
- Grava `app/scripts/auditoria_aberturas.csv` com o detalhamento.

SOMENTE LEITURA. Nao escreve nada no Supabase nem na Z-API, nao apaga nada.

Uso
---
    python app/scripts/auditar_aberturas_zapi.py
"""

from __future__ import annotations

import csv
import os
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / "app" / ".env"
CSV_OUT = Path(__file__).resolve().parent / "auditoria_aberturas.csv"

FIX_DATE = "2026-05-19"  # aberturas a partir daqui ja usam o codigo corrigido


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
        valor = valor.strip().strip('"').strip("'")
        if chave and chave not in os.environ:
            os.environ[chave] = valor


carregar_env(ENV_PATH)

# --- Z-API (mesmas credenciais do core.py) ---
ZAPI_INSTANCE = "3EFABF270A13919DE95AD6F4B026E35E"
ZAPI_TOKEN = "E1014DD5B87C05F2DF8F2547"
ZAPI_SEC_TOKEN = os.environ.get("ZAPI_SEC_TOKEN", "")
ZAPI_BASE = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}"
ZAPI_HEADERS = {"client-token": ZAPI_SEC_TOKEN}

# --- Supabase ---
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def buscar_aberturas() -> list[dict]:
    """Le do Supabase toda conversa que tem um evento `abertura`.

    Returns:
        Lista de dicts {chat_lid, phone, abertura_data}, uma por chat_lid.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Faltam SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY no app/.env")

    url = f"{SUPABASE_URL}/rest/v1/messages"
    params = {
        "select": "event_ts,conversations(chat_lid,phone)",
        "payload->>type": "eq.abertura",
    }
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    resp = requests.get(url, params=params, headers=headers, timeout=60)
    resp.raise_for_status()

    por_chat: dict[str, dict] = {}
    for row in resp.json():
        conv = row.get("conversations") or {}
        chat_lid = conv.get("chat_lid")
        phone = conv.get("phone")
        if not chat_lid or not phone:
            continue
        data = (row.get("event_ts") or "")[:10]
        atual = por_chat.get(chat_lid)
        if atual is None or data < atual["abertura_data"]:
            por_chat[chat_lid] = {"chat_lid": chat_lid, "phone": phone, "abertura_data": data}
    return sorted(por_chat.values(), key=lambda x: x["abertura_data"])


def buscar_todos_chats() -> tuple[set[str], set[str]]:
    """Pagina o endpoint /chats e devolve os conjuntos de LIDs e phones que
    tem conversa real na instancia do WhatsApp.

    Returns:
        (lids, phones) — dois sets de strings.
    """
    lids: set[str] = set()
    phones: set[str] = set()
    page = 1
    while True:
        r = requests.get(
            f"{ZAPI_BASE}/chats",
            headers=ZAPI_HEADERS,
            params={"page": page, "pageSize": 200},
            timeout=30,
        )
        r.raise_for_status()
        lote = r.json()
        if not isinstance(lote, list) or not lote:
            break
        for c in lote:
            if not isinstance(c, dict):
                continue
            if c.get("lid"):
                lids.add(str(c["lid"]))
            if c.get("phone"):
                phones.add(str(c["phone"]))
        if len(lote) < 200:
            break
        page += 1
        time.sleep(0.2)
    return lids, phones


def phone_exists(phone: str) -> dict:
    """Consulta phone-exists. Returns {status, exists, lid, detalhe}."""
    try:
        r = requests.get(f"{ZAPI_BASE}/phone-exists/{phone}", headers=ZAPI_HEADERS, timeout=30)
    except requests.RequestException as e:
        return {"status": "erro", "exists": None, "lid": "", "detalhe": f"request_falhou: {e}"}
    if r.status_code != 200:
        return {"status": "erro", "exists": None, "lid": "",
                "detalhe": f"HTTP {r.status_code}: {r.text[:140]}"}
    try:
        body = r.json()
    except ValueError:
        return {"status": "erro", "exists": None, "lid": "",
                "detalhe": f"resposta nao-JSON: {r.text[:140]}"}
    if isinstance(body, dict) and "error" in body:
        return {"status": "erro", "exists": None, "lid": "",
                "detalhe": str(body.get("error"))}
    return {
        "status": "ok",
        "exists": bool(body.get("exists")),
        "lid": str(body.get("lid") or ""),
        "detalhe": "",
    }


def main() -> None:
    print("Buscando aberturas no Supabase...")
    aberturas = buscar_aberturas()
    print(f"  {len(aberturas)} conversas com evento 'abertura'.")

    print("Baixando lista de conversas reais da instancia (/chats)...")
    chat_lids, chat_phones = buscar_todos_chats()
    print(f"  {len(chat_lids)} conversas reais na instancia.\n")

    enviou: list[dict] = []
    fantasma: list[dict] = []
    erro: list[dict] = []
    linhas_csv: list[dict] = []

    for i, ab in enumerate(aberturas, 1):
        phone = ab["phone"]
        print(f"[{i}/{len(aberturas)}] {phone} ({ab['chat_lid']}) ...", end=" ")
        pe = phone_exists(phone)
        time.sleep(0.25)

        tem_conversa = (
            (pe["lid"] and pe["lid"] in chat_lids)
            or ab["chat_lid"] in chat_lids
            or phone in chat_phones
        )

        if pe["status"] == "erro":
            veredito, motivo = "ERRO", pe["detalhe"]
            erro.append({**ab, "motivo": motivo})
            print(f"ERRO -> {motivo}")
        elif pe["exists"] is False:
            veredito, motivo = "FANTASMA", "numero sem WhatsApp"
            fantasma.append({**ab, "motivo": motivo})
            print("FANTASMA (numero sem WhatsApp)")
        elif tem_conversa:
            veredito, motivo = "ENVIOU", "conversa existe na instancia"
            enviou.append({**ab, "motivo": motivo})
            print("ENVIOU (conversa existe na instancia)")
        else:
            veredito, motivo = "FANTASMA", "tem WhatsApp mas sem conversa na instancia"
            fantasma.append({**ab, "motivo": motivo})
            print("FANTASMA (tem WhatsApp, mas nenhuma conversa)")

        linhas_csv.append({
            "phone": phone,
            "chat_lid": ab["chat_lid"],
            "abertura_data": ab["abertura_data"],
            "pre_fix": "sim" if ab["abertura_data"] < FIX_DATE else "nao",
            "veredito": veredito,
            "motivo": motivo,
            "tem_whatsapp": {True: "sim", False: "nao"}.get(pe["exists"], "?"),
            "lid_real": pe["lid"],
        })

    with CSV_OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(linhas_csv[0].keys()) if linhas_csv else [])
        w.writeheader()
        w.writerows(linhas_csv)

    print("\n" + "=" * 64)
    print(f"RESUMO  ({len(aberturas)} aberturas auditadas)")
    print("=" * 64)
    print(f"  ENVIOU de verdade : {len(enviou)}")
    print(f"  FANTASMA (nao foi): {len(fantasma)}")
    print(f"  ERRO (rever)      : {len(erro)}")

    print("\n--- FANTASMAS (abertura no banco, mas o SDR NAO enviou) ---")
    for x in fantasma:
        print(f"  {x['phone']:<16} {x['abertura_data']}  {x['chat_lid']:<24} [{x['motivo']}]")

    if erro:
        print("\n--- ERROS (rever manualmente) ---")
        for x in erro:
            print(f"  {x['phone']:<16} {x['abertura_data']}  {x['motivo']}")

    print(f"\nCSV completo: {CSV_OUT}")


if __name__ == "__main__":
    main()
