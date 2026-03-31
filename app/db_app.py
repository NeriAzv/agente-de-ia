from flask import Flask, request
from agent import Agent_AI
from colors import GREEN, RED, YELLOW, BLUE, RESET

import json
import time
import os


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
    """Recebe status de entrega/leitura — não salvamos no histórico."""
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

    if os.path.exists(file):
        with open(file, "r", encoding="utf-8") as f:
            existing_data = json.load(f)
    else:
        existing_data = []

    existing_data.append({"timestamp": time.time(), "data": data})

    with open(file, "w", encoding="utf-8") as f:
        json.dump(existing_data, f, indent=2, ensure_ascii=False)

    print(f" > /webhook/{BLUE}receive{RESET} chamado")

    if not from_me:
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
            saved_data  = agent.answer_list[chatLid]
            saved_phone = saved_data.get("phone")
            print(f" {GREEN} > Reiniciando timer para {chatLid} (phone={saved_phone}) {RESET}")
            agent.get_ai_response(saved_phone, chatLid, saved_data)

    elif status == "AVAILABLE":
        print(f" {GREEN} > {name} está online {RESET}")

    elif status == "UNAVAILABLE":
        print(f" {RED} > {name} está offline {RESET}")

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


if __name__ == "__main__":
    agent = Agent_AI()
    answer_list = agent.answer_list
    app.run(port=5001)
