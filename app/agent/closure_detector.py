import json
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage


_LLM = ChatAnthropic(
    model="claude-haiku-4-5-20251001",
    temperature=0,
)

_SYSTEM_PROMPT = """\
Você é um analisador de encerramento de conversa em vendas B2B por WhatsApp. Analise a mensagem \
do lead e decida se o agente deve responder ou ficar em silêncio neste turno. Retorne apenas JSON \
puro, sem markdown, sem explicação.

Contexto adicional fornecido:
- has_active_meeting (bool): indica se já existe uma reunião agendada ativa com este lead.

Regras de decisão:
1. Mensagens puramente de encerramento ("obrigado", "valeu", "ok", "combinado", "tudo certo", \
"até mais", "show", "perfeito") sem pergunta e sem novo pedido ⇒ should_respond=false, \
reason="closure_message", confidence="high".
2. Se has_active_meeting=true e a mensagem é apenas confirmação curta ("ok", "perfeito", \
"combinado", "show", "até lá") ⇒ should_respond=false, reason="meeting_confirmed_ack", \
confidence="high".
3. Se a mensagem contém "?" OU pedido explícito (preço, prazo, link, informação, dúvida, "me \
manda", "como funciona", etc.) ⇒ should_respond=true, reason=null.
4. Em qualquer ambiguidade, dúvida sobre intenção ou conteúdo substantivo (mesmo após um \
"obrigado") ⇒ should_respond=true, confidence="low" ou "medium".
5. Só silencie (should_respond=false) quando a confidence puder ser "high". Nos demais casos \
should_respond=true.

Formato de resposta obrigatório:
{"should_respond": bool, "reason": "closure_message" | "meeting_confirmed_ack" | null, \
"confidence": "high" | "medium" | "low"}\
"""


def run_closure_detector(lead_message: str, has_active_meeting: bool) -> dict:
    """
    Decide se o agente principal deve responder ou silenciar este turno.

    Args:
        lead_message: Mensagem atual do lead.
        has_active_meeting: True se já existe reunião agendada ativa com o lead.

    Returns:
        Dict com should_respond, reason e confidence.
    """
    user_content = (
        f"has_active_meeting={str(bool(has_active_meeting)).lower()}\n"
        f"mensagem: {lead_message}"
    )

    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_content),
    ]

    response = _LLM.invoke(messages)

    content = response.content.strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"should_respond": True, "reason": None, "confidence": "low"}
