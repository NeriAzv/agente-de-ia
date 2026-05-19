import json
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from agent.normalizers import normalizar_telefone


_LLM = ChatAnthropic(
    model="claude-haiku-4-5-20251001",
    temperature=0,
)

_SYSTEM_PROMPT = """\
Você é um analisador de repasse de contato em vendas B2B por WhatsApp. Analise a mensagem do \
lead e detecte se ele está indicando OUTRA pessoa para conversar, fornecendo um telefone. \
Retorne apenas JSON puro, sem markdown, sem explicação.

ACEITAR como handoff (is_handoff=true):
- "fala com o João: 11 98765-4321"
- "o responsável é a Maria, número 11 9..."
- "manda mensagem aí pra +55 21 99999-0000, é a Marina, sócia"
- "esse é o WhatsApp do meu sócio: ..."
- "quem cuida disso é o financeiro, número ..."
- Qualquer indicação explícita de "fala com X, número Y" / "manda pra Y".

REJEITAR (is_handoff=false):
- Lead informando o PRÓPRIO número ("meu telefone é...", "me liga em...").
- Número de outra empresa sem instrução de falar com a pessoa.
- Telefones em assinatura, footer ou contexto genérico.
- Menção a pessoa sem telefone associado, ou telefone sem indicação de fala.

Formato de resposta obrigatório:
{
  "is_handoff": bool,
  "new_phone": string | null,
  "new_contact_name": string | null,
  "new_contact_role": string | null,
  "confidence": "high" | "medium" | "low"
}

Regras adicionais:
- new_phone: copie o telefone como aparece na mensagem (sem normalizar); o sistema normaliza depois.
- new_contact_role: cargo/relação explícita ("sócio", "financeiro", "responsável", "diretor", \
"esposa", etc.) ou null.
- Se houver MAIS DE UM número, escolha apenas o primeiro com indicação clara de fala.
- Só use confidence="high" quando houver telefone + indicação inequívoca de falar com a pessoa.\
"""


_DEFAULT_FALSE = {
    "is_handoff": False,
    "new_phone": None,
    "new_contact_name": None,
    "new_contact_role": None,
    "confidence": "low",
}


def run_handoff_detector(lead_message: str) -> dict:
    """
    Detecta repasse explícito de contato pelo lead (indicação de outro número para falar).

    Args:
        lead_message: Mensagem atual do lead.

    Returns:
        Dict com is_handoff, new_phone (normalizado E.164 sem '+'), new_contact_name,
        new_contact_role e confidence.
    """
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=lead_message),
    ]

    response = _LLM.invoke(messages)

    content = response.content.strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        return dict(_DEFAULT_FALSE)

    if not result.get("is_handoff"):
        return {
            "is_handoff": False,
            "new_phone": None,
            "new_contact_name": result.get("new_contact_name"),
            "new_contact_role": result.get("new_contact_role"),
            "confidence": result.get("confidence", "low"),
        }

    raw_phone = result.get("new_phone")
    normalized = normalizar_telefone(raw_phone) if raw_phone else None

    if not normalized:
        return {
            "is_handoff": False,
            "new_phone": None,
            "new_contact_name": result.get("new_contact_name"),
            "new_contact_role": result.get("new_contact_role"),
            "confidence": "low",
        }

    return {
        "is_handoff": True,
        "new_phone": normalized,
        "new_contact_name": result.get("new_contact_name"),
        "new_contact_role": result.get("new_contact_role"),
        "confidence": result.get("confidence", "low"),
    }
