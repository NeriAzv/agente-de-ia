import json
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage


_LLM = ChatAnthropic(
    model="claude-haiku-4-5-20251001",
    temperature=0,
)

_SYSTEM_PROMPT = """\
Você é um analisador de qualificação de leads B2B. Leia o histórico completo da conversa e avalie \
o estado atual dos 4 critérios de qualificação da Btime. Retorne apenas JSON puro, sem markdown, \
sem explicação.

Regras de preenchimento:
- revenue_confirmed: true apenas se o lead informou explicitamente um valor ou faixa de faturamento
- revenue_value: valor exato mencionado (ex: "R$ 5M/ano", "uns 10 milhões"), ou null
- pain_identified: true se o lead descreveu um processo ou problema concreto
- pain_description: resumo curto da dor identificada, ou null
- product_routed: "squad_ai" se faturamento ~R$4M+/ano, "saas_btime" se abaixo, null se faturamento não confirmado
- lead_convinced: true apenas se o lead demonstrou intenção clara de avançar sem objeções pendentes
- missing_criteria: lista das chaves dos critérios ainda não preenchidos, entre: \
"revenue_confirmed", "pain_identified", "product_routed", "lead_convinced"

Formato de resposta obrigatório:
{
  "revenue_confirmed": bool,
  "revenue_value": string | null,
  "pain_identified": bool,
  "pain_description": string | null,
  "product_routed": "squad_ai" | "saas_btime" | null,
  "lead_convinced": bool,
  "missing_criteria": string[]
}\
"""


def _serialize_history(history: list) -> str:
    """Serializa lista de HumanMessage/AIMessage para string legível."""
    lines = []
    for msg in history:
        if isinstance(msg, HumanMessage):
            lines.append(f"Lead: {msg.content}")
        elif isinstance(msg, AIMessage):
            lines.append(f"Ana: {msg.content}")
    return "\n".join(lines)


def run_qualification_tracker(history: list) -> dict:
    """
    Analisa o histórico completo da conversa e retorna o estado dos critérios de qualificação.

    Args:
        history: Lista de HumanMessage/AIMessage com o histórico completo da conversa.

    Returns:
        Dict com revenue_confirmed, revenue_value, pain_identified, pain_description,
        product_routed, lead_convinced e missing_criteria.
    """
    history_str = _serialize_history(history)

    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=f"Histórico da conversa:\n\n{history_str}"),
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
        # Fallback: retorna todos os critérios vazios
        return {
            "revenue_confirmed": False,
            "revenue_value": None,
            "pain_identified": False,
            "pain_description": None,
            "product_routed": None,
            "lead_convinced": False,
            "missing_criteria": ["revenue_confirmed", "pain_identified", "product_routed", "lead_convinced"],
        }
