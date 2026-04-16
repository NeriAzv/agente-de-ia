import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, request
from agent import Agent_AI
from colors import GREEN, RED, YELLOW, BLUE, RESET

import json
import time
import threading

_file_lock = threading.Lock()


def _is_ai_blocked(chatLid: str) -> bool:
    """Retorna True se o lead tiver ai_blocked=true no lead_info.json."""
    lead_info_path = os.path.join("chats", chatLid, "lead_info.json")
    if not os.path.exists(lead_info_path):
        return False
    try:
        with open(lead_info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        return bool(info.get("ai_blocked", False))
    except (json.JSONDecodeError, ValueError):
        return False


app = Flask(__name__)
answer_list = None


# ------------------------------------------------------------------
# Webhooks
# ------------------------------------------------------------------

@app.route("/webhook/connect", methods=["POST"])
def webhook_connect():
    """Endpoint de conexão inicial."""
    print(f" > /webhook/{BLUE}connect{RESET} chamado")
    return {"status": "ok"}, 200


@app.route("/webhook/send", methods=["POST"])
def webhook_send():
    """Chamado quando enviamos uma mensagem via API."""
    print(f" > /webhook/{BLUE}send{RESET} chamado")
    return {"status": "ok"}, 200


@app.route("/webhook/message-status", methods=["POST"])
def webhook_message_status():
    """Recebe status de entrega/leitura, não salvamos no histórico."""
    print(f" > /webhook/{BLUE}message-status{RESET} chamado")
    return {"status": "ok"}, 200


@app.route("/webhook/receive", methods=["POST"])
def webhook_receive():
    """Mensagens recebidas chegam neste webhook."""
    data    = request.json
    chatLid = data.get("chatLid")
    from_me = data.get("fromMe")
    phone   = data.get("phone")

    if not chatLid:
        return {"status": "ignored", "reason": "chatLid ausente"}, 200

# Salva mensagem no histórico
    lead_dir = os.path.join("chats", chatLid)
    os.makedirs(lead_dir, exist_ok=True)
    file = os.path.join(lead_dir, "history.json")

    with _file_lock:
        if os.path.exists(file):
            try:
                with open(file, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
            except (json.JSONDecodeError, ValueError):
                existing_data = []
        else:
            existing_data = []

        existing_data.append({"timestamp": time.time(), "data": data})

        with open(file, "w", encoding="utf-8") as f:
            json.dump(existing_data, f, indent=2, ensure_ascii=False)

    print(f" > /webhook/{BLUE}receive{RESET} chamado")

    # Migra pasta temporária {phone}@lid → chatLid real (primeira resposta do lead)
    if phone and chatLid:
        agent.migrar_lead_se_necessario(phone, chatLid)

    if not from_me:
        if _is_ai_blocked(chatLid):
            print(f" {RED} > IA bloqueada para {chatLid}, mensagem ignorada {RESET}")
            return {"status": "ignored", "reason": "ai_blocked"}, 200
        timers_ativos = list(agent.pending_timers.keys())
        print(f" {GREEN} > Nova mensagem recebida para {chatLid} {RESET}")
        print(f" {YELLOW} > Timers ativos antes: {timers_ativos} {RESET}")
        print(f" {YELLOW} > phone={phone} já tem timer: {chatLid in agent.pending_timers} {RESET}")
        agent.composing_set.discard(chatLid)
        agent.get_ai_response(phone, chatLid, data)
        print(f" {YELLOW} > Timers ativos depois: {list(agent.pending_timers.keys())} {RESET}")

    return {"status": "ok"}, 200


@app.route("/webhook/presence", methods=["POST"])
def webhook_presence():
    """Processa eventos de presença (digitando, gravando, pausado, etc.)."""
    data    = request.json
    phone   = data.get("phone")
    chatLid = data.get("chatLid") or (f"{phone}@lid" if phone else None)
    name    = data.get("name", chatLid)
    status  = data.get("status")

    print(f" \n\n\n{RED} > /webhook/presence{RESET} chamado")
    print(f"{GREEN} >>> PRESENÇA de {BLUE}{name}{RESET} | phone={phone} | status={status}")
    print(f" {YELLOW} > Timers ativos: {list(agent.pending_timers.keys())} {RESET}")
    print(f" {YELLOW} > answer_list: {list(agent.answer_list.keys())} {RESET}")

    if status == "COMPOSING":
        print(f"\n\n{BLUE}Name: {name} Está digitando, resposta cancelada{RESET}")
        agent.composing_set.add(chatLid)
        if chatLid in agent.pending_timers:
            agent.pending_timers[chatLid].cancel()
            del agent.pending_timers[chatLid]
            print(f" {GREEN} > Timer cancelado para {chatLid} {RESET}")

            # Safety net: COMPOSING às vezes chega DEPOIS do RECEIVED (race condition do Z-API).
            # Aguarda 2 minutos — se o lead ainda estiver sem timer e com mensagem pendente,
            # significa que o PAUSED/AVAILABLE nunca chegou (bug) e é seguro responder.
            def _composing_safety_net():
                if chatLid in agent.composing_set:
                    # Lead ainda "está digitando" depois de 2min → claramente bugado
                    if chatLid not in agent.pending_timers and chatLid in agent.answer_list:
                        if not _is_ai_blocked(chatLid):
                            saved = agent.answer_list[chatLid]
                            saved_phone = saved.get("phone")
                            print(f" {YELLOW} > Safety net (2min): COMPOSING travado para {chatLid}, disparando resposta {RESET}")
                            agent.composing_set.discard(chatLid)
                            agent.get_ai_response(saved_phone, chatLid, saved)
            threading.Timer(120.0, _composing_safety_net).start()
        else:
            print(f" {YELLOW} > Nenhum timer ativo para {chatLid} {RESET}")

    elif status == "RECORDING":
        agent.composing_set.add(chatLid)
        if chatLid in agent.pending_timers:
            agent.pending_timers[chatLid].cancel()
            del agent.pending_timers[chatLid]
            print(f" {GREEN} > Timer cancelado para {chatLid} {RESET}")
        print(f"🎤 {name} está gravando áudio")

    elif status == "PAUSED":
        print(f"{BLUE} > {name} pausou a digitação/gravação{RESET}")
        agent.composing_set.discard(chatLid)
        if chatLid not in agent.pending_timers and chatLid in agent.answer_list:
            if _is_ai_blocked(chatLid):
                print(f" {RED} > IA bloqueada para {chatLid}, resposta cancelada {RESET}")
            else:
                saved_data  = agent.answer_list[chatLid]
                saved_phone = saved_data.get("phone")
                print(f" {GREEN} > Reiniciando timer para {chatLid} (phone={saved_phone}) {RESET}")
                agent.get_ai_response(saved_phone, chatLid, saved_data)

    elif status == "AVAILABLE":
        print(f" {GREEN} > {name} está online {RESET}")
        # Mesmo tratamento: se estava digitando e mudou pra AVAILABLE sem PAUSED
        was_composing = chatLid in agent.composing_set
        agent.composing_set.discard(chatLid)
        if was_composing and chatLid not in agent.pending_timers and chatLid in agent.answer_list:
            if _is_ai_blocked(chatLid):
                print(f" {RED} > IA bloqueada para {chatLid}, resposta cancelada {RESET}")
            else:
                saved_data  = agent.answer_list[chatLid]
                saved_phone = saved_data.get("phone")
                print(f" {GREEN} > Usuário voltou online sem PAUSED, reiniciando resposta para {chatLid} (phone={saved_phone}) {RESET}")
                agent.get_ai_response(saved_phone, chatLid, saved_data)

    elif status == "UNAVAILABLE":
        print(f" {RED} > {name} está offline {RESET}")
        # Se o usuário estava digitando (composing_set) e foi direto pra offline
        # sem PAUSED, precisamos reiniciar a resposta para não perder mensagens
        was_composing = chatLid in agent.composing_set
        agent.composing_set.discard(chatLid)
        if was_composing and chatLid not in agent.pending_timers and chatLid in agent.answer_list:
            if _is_ai_blocked(chatLid):
                print(f" {RED} > IA bloqueada para {chatLid}, resposta cancelada {RESET}")
            else:
                saved_data  = agent.answer_list[chatLid]
                saved_phone = saved_data.get("phone")
                print(f" {GREEN} > Usuário saiu offline sem PAUSED, reiniciando resposta para {chatLid} (phone={saved_phone}) {RESET}")
                agent.get_ai_response(saved_phone, chatLid, saved_data)

    return {"status": "ok"}, 200


@app.route("/iniciar-conversa", methods=["POST"])
def iniciar_conversa():
    """
    Inicia uma conversa com um lead que ainda não tem histórico.
    Corpo esperado: {"phone": "5511999999999", "chatLid": "5511999999999@lid"}
    """
    data    = request.json or {}
    phone   = data.get("phone")
    chatLid = data.get("chatLid")

    if not phone or not chatLid:
        return {"status": "error", "reason": "phone e chatLid são obrigatórios"}, 400

    print(f" > /iniciar-conversa chamado para chatLid={chatLid}")
    enviado = agent.iniciar_conversa(phone, chatLid)

    if enviado:
        return {"status": "ok", "message": "Mensagem de abertura enviada"}, 200
    else:
        return {"status": "skipped", "reason": "Lead já possui histórico ou chatLid não permitido"}, 200


@app.route("/chamar-lead", methods=["POST"])
def chamar_lead():
    """
    Chama um lead manualmente.
    Corpo esperado:
        {
            "nome": "Nome Completo",
            "numero": "5511999999999",
            "descricao": "Contexto opcional sobre o lead"
        }
    """
    data      = request.json or {}
    nome      = data.get("nome")
    numero    = data.get("numero")
    descricao = data.get("descricao", "")
    mensagem  = data.get("mensagem", "")

    if not nome or not numero:
        return {"status": "error", "reason": "nome e numero são obrigatórios"}, 400

    # Normaliza o número (remove espaços, traços, parênteses)
    numero = numero.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    chatLid  = f"{numero}@lid"
    lead_dir = os.path.join("chats", chatLid)
    os.makedirs(lead_dir, exist_ok=True)

    # Cria/atualiza lead_info.json com os dados passados
    lead_info_path = os.path.join(lead_dir, "lead_info.json")
    lead_info = {}
    if os.path.exists(lead_info_path):
        try:
            with open(lead_info_path, "r", encoding="utf-8") as f:
                lead_info = json.load(f)
        except (json.JSONDecodeError, ValueError):
            lead_info = {}

    lead_info["nome"] = nome
    lead_info["phone"] = numero
    if descricao:
        lead_info["descricao_manual"] = descricao

    with open(lead_info_path, "w", encoding="utf-8") as f:
        json.dump(lead_info, f, indent=2, ensure_ascii=False)

    print(f" > /chamar-lead chamado para {nome} ({numero})")

    enviado = agent.iniciar_conversa(numero, chatLid, mensagem)

    if enviado:
        return {"status": "ok", "message": f"Lead {nome} chamado com sucesso"}, 200
    else:
        return {"status": "skipped", "reason": "Lead já possui histórico de conversa"}, 200


@app.route("/forcar-resposta", methods=["POST"])
def forcar_resposta():
    """
    Força o agente a processar uma mensagem perdida de um lead (ex: z-api bugou).
    Se 'mensagem' for informada, injeta ela no histórico antes de responder.

    Corpo esperado:
        {
            "phone": "5511999999999",
            "chatLid": "26658979463196@lid",
            "mensagem": "Sim"           ← opcional: texto da mensagem perdida
        }
    """
    data    = request.json or {}
    phone   = data.get("phone")
    chatLid = data.get("chatLid")
    mensagem = data.get("mensagem", "").strip()

    if not phone or not chatLid:
        return {"status": "error", "reason": "phone e chatLid são obrigatórios"}, 400

    lead_dir = os.path.join("chats", chatLid)
    os.makedirs(lead_dir, exist_ok=True)
    file = os.path.join(lead_dir, "history.json")

    # Injeta a mensagem perdida no histórico se informada
    if mensagem:
        fake_data = {
            "chatLid": chatLid,
            "phone": phone,
            "fromMe": False,
            "momment": int(time.time() * 1000),
            "status": "RECEIVED",
            "type": "ReceivedCallback",
            "fromApi": False,
            "text": {"message": mensagem},
        }
        with _file_lock:
            existing = []
            if os.path.exists(file):
                try:
                    with open(file, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except (json.JSONDecodeError, ValueError):
                    existing = []
            existing.append({"timestamp": time.time(), "data": fake_data})
            with open(file, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
        print(f" > /forcar-resposta: mensagem '{mensagem}' injetada no histórico de {chatLid}")
        trigger_data = fake_data
    else:
        # Sem mensagem nova: usa a última mensagem do lead no histórico como gatilho
        trigger_data = None
        if os.path.exists(file):
            try:
                with open(file, "r", encoding="utf-8") as f:
                    historico = json.load(f)
                for entry in reversed(historico):
                    d = entry.get("data", {})
                    if not d.get("fromMe"):
                        trigger_data = d
                        break
            except (json.JSONDecodeError, ValueError):
                pass
        if not trigger_data:
            return {"status": "error", "reason": "Nenhuma mensagem do lead encontrada no histórico"}, 400
        print(f" > /forcar-resposta: reprocessando última mensagem do lead {chatLid}")

    threading.Thread(
        target=agent.get_ai_response,
        args=(phone, chatLid, trigger_data),
        daemon=True,
    ).start()

    return {"status": "ok", "message": f"Resposta forçada para {chatLid}"}, 200


if __name__ == "__main__":
    agent = Agent_AI()
    answer_list = agent.answer_list
    app.run(host='0.0.0.0', port=5001)
