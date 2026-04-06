import json
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage


_LLM = ChatAnthropic(
    model="claude-haiku-4-5-20251001",
    temperature=0,
)

_SYSTEM_PROMPT = """\
Você é um classificador de intenção em conversas de vendas B2B via WhatsApp. Analise a mensagem \
do lead e classifique sua intenção principal. Retorne apenas JSON puro, sem markdown, sem explicação.

Valores válidos para intent:
- "interest": demonstra interesse no produto ou na Btime
- "question": faz uma dúvida sobre o produto, processo ou empresa
- "price_request": pergunta quanto custa ou pede valor
- "buy_signal": sinal claro de querer comprar ou avançar (ex: "quero contratar", "quando começa", "vamos fazer")
- "scheduling_request": quer marcar reunião ou sugere data/horário
- "objection": demonstra resistência ou barreira para avançar
- "off_topic": mensagem não relacionada ao contexto de vendas
- "unclear": mensagem ambígua ou sem intenção clara

Formato de resposta obrigatório:
{"intent": string, "confidence": "high" | "medium" | "low"}\
"""


def run_intent_classifier(lead_message: str) -> dict:
    """
    Classifica a intenção principal da mensagem do lead.

    Args:
        lead_message: Mensagem atual do lead.

    Returns:
        Dict com intent e confidence.
    """
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=lead_message),
    ]

    response = _LLM.invoke(messages)

    # Limpa markdown se houver
    content = response.content.strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Fallback: retorna intenção ambígua se não conseguir parsear
        return {"intent": "unclear", "confidence": "low"}
