"""Gerador dedicado da mensagem de abertura para handoffs.

Diferente da abertura cold outbound (`get_instrucao_abertura` em context.py),
o handoff parte de uma ponte humana explícita: alguém indicou esse novo
contato. O objetivo da primeira mensagem aqui é REFERENCIAR essa ponte
imediatamente, em vez de criar rapport do zero.

Este módulo é intencionalmente isolado:
  - Slots estruturados como entrada (sem reparsear free-text).
  - Prompt focado, sem competir com regras de cold outbound.
  - Saída validada: 2 balões curtos separados por linha em branco; falhou,
    cai num template determinístico de fallback.
"""

from __future__ import annotations

import os
from typing import TypedDict, Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage


class HandoffSlots(TypedDict, total=False):
    new_contact_name: str           # obrigatório — quem vamos abordar
    new_contact_role: Optional[str] # cargo/papel do novo contato (ex.: "comercial")
    referrer_name: str              # obrigatório — quem indicou
    referrer_company: Optional[str] # empresa de quem indicou
    pain_summary: Optional[str]     # dor/contexto que motivou a indicação
    product: Optional[str]          # produto Btime já mapeado (ex.: "Squad AI")


_SYSTEM_PROMPT = """\
Você é Ana, SDR da Btime. Está iniciando a PRIMEIRA mensagem por WhatsApp para um lead \
que foi INDICADO por outra pessoa com quem você já conversou. Sua tarefa: abrir a conversa \
referenciando claramente essa ponte humana — não soar como prospecção fria.

Saída obrigatória: 2 mensagens curtas separadas por UMA linha em branco. Texto natural. \
Sem emojis. Sem JSON. Sem markdown. Sem assinatura.

Regras (todas obrigatórias):
1. Saudação natural ("Olá", "Oi", "Bom dia", "Boa tarde") + nome do novo contato.
2. Apresentação curta: "Sou a Ana, da Btime." (ou variação equivalente, NUNCA "Meu nome é").
3. Mencionar EXPLICITAMENTE quem indicou, pelo nome dado nos slots. Se houver empresa do \
indicador, citar ("o {referrer_name}, da {referrer_company}"). Se houver papel do novo \
contato no contexto (ex.: comercial, financeiro), encaixar de forma natural.
4. Se houver pain_summary ou product nos slots, ancorar o motivo em uma frase curta \
("ele comentou que vocês estão olhando X" ou "ele me pediu pra te passar contexto do Y"). \
NÃO inventar dor, produto, cargo ou empresa que não estejam nos slots.
5. Terminar com UMA pergunta direta convidando a confirmar abertura para conversar \
("Faz sentido a gente trocar uma ideia rápida?" ou variação). Sem "tudo bem?".
6. Tom: profissional, breve, humano. Sem pitch longo, sem demo, sem "vi seu perfil", \
sem explicar como conseguiu o número (a indicação JÁ é a justificativa).

Slots ausentes (None ou vazios) devem ser simplesmente omitidos — nunca preencher com \
suposições. Os ÚNICOS dados garantidos são new_contact_name e referrer_name.\
"""


def _format_slots(slots: HandoffSlots) -> str:
    """Serializa slots como pares chave: valor para o turno do usuário no LLM."""
    linhas = []
    for k in ("new_contact_name", "new_contact_role", "referrer_name",
              "referrer_company", "pain_summary", "product"):
        v = slots.get(k)
        if v:
            linhas.append(f"- {k}: {v}")
    return "\n".join(linhas) if linhas else "(slots vazios)"


def _fallback_template(slots: HandoffSlots) -> str:
    """Mensagem determinística usada se o LLM falhar ou violar o formato.

    Mantém a referência ao indicador e o tom mínimo viável. Sempre 2 balões.
    """
    nome = slots.get("new_contact_name") or "tudo bem"
    referrer = slots.get("referrer_name") or "um contato"
    empresa = slots.get("referrer_company")
    motivo = slots.get("pain_summary") or slots.get("product")

    ponte = f"O {referrer}"
    if empresa:
        ponte += f", da {empresa},"
    ponte += " me passou seu contato"
    if motivo:
        ponte += f" — ele comentou sobre {motivo}"
    ponte += "."

    return (
        f"Olá, {nome}! Sou a Ana, da Btime.\n\n"
        f"{ponte} Faz sentido a gente trocar uma ideia rápida?"
    )


def _validar_formato(texto: str) -> bool:
    """Aceita se houver pelo menos 2 blocos não-vazios separados por linha em branco."""
    if not texto or not texto.strip():
        return False
    partes = [p.strip() for p in texto.split("\n\n") if p.strip()]
    return len(partes) >= 2


def gerar_abertura_handoff(slots: HandoffSlots) -> str:
    """Gera a mensagem de abertura para um novo lead recebido via handoff.

    Inputs:
        slots: dicionário estruturado com os campos do handoff. Obrigatórios:
            `new_contact_name` e `referrer_name`. Os demais são opcionais e
            simplesmente omitidos do prompt quando ausentes.

    Returns:
        String com a mensagem pronta para enviar via Z-API. Garantida não-vazia
        e com pelo menos 2 balões separados por `\\n\\n`. Em caso de falha do
        LLM ou violação de formato, retorna o template determinístico.

    Side effects:
        Uma chamada HTTP ao OpenAI Chat Completions API. Nenhum disco/estado.

    Raises:
        Nenhuma exceção esperada. Falhas de rede caem no fallback.
    """
    if not slots.get("new_contact_name") or not slots.get("referrer_name"):
        return _fallback_template(slots)

    try:
        llm = ChatOpenAI(
            model="gpt-4o-mini",
            api_key=os.environ.get("OPENAI_API_KEY"),
            temperature=0.6,
        )
        resposta = llm.invoke([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content="Slots:\n" + _format_slots(slots)),
        ])
        texto = (resposta.content or "").strip()
        if _validar_formato(texto):
            return texto
    except Exception:
        pass

    return _fallback_template(slots)
