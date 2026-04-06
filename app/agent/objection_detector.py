import json
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage


_LLM = ChatAnthropic(
    model="claude-haiku-4-5-20251001",
    temperature=0,
)

_SYSTEM_PROMPT = """\
Você é um analisador de objeções de vendas B2B. Analise a mensagem do lead e identifique se há \
uma objeção de vendas presente. Retorne apenas JSON puro, sem markdown, sem explicação.

Tipos de objeção válidos:
- "price": lead diz que está caro, não tem budget ou pede valor
- "timing": não é o momento, quer esperar
- "decision_maker": precisa aprovar com sócio, chefe ou financeiro
- "already_has_solution": já usa outro sistema ou já tem TI interna
- "bad_past_experience": já tentou algo parecido e não funcionou
- "wants_proposal": pede proposta, orçamento ou e-mail com detalhes
- "wants_poc": pede teste grátis ou POC
- "internal_it": diz que tem time de TI interno que resolve

Formato de resposta obrigatório:
{"has_objection": bool, "type": string | null, "raw": string | null}

Se não houver objeção: {"has_objection": false, "type": null, "raw": null}
O campo raw deve conter o trecho exato da mensagem que gerou a detecção.\
"""


def run_objection_detector(lead_message: str) -> dict:
    """
    Analisa a mensagem do lead e detecta objeções de vendas.

    Args:
        lead_message: Mensagem atual do lead.

    Returns:
        Dict com has_objection, type e raw.
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
        # Fallback: retorna sem objeção se não conseguir parsear
        return {"has_objection": False, "type": None, "raw": None}
