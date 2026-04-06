import json
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage


_LLM = ChatAnthropic(
    model="claude-haiku-4-5-20251001",
    temperature=0,
)

_SYSTEM_PROMPT = """\
Você é um validador de agendamento para um agente de SDR B2B. Analise se o lead está pronto para \
ter uma reunião agendada com base nos critérios de qualificação e na mensagem atual. Resolva datas \
relativas com base na data de hoje fornecida. Retorne apenas JSON puro, sem markdown, sem explicação.

Regras de validação:
- ready_to_schedule só é true se qualification.missing_criteria estiver vazio E a mensagem do lead \
contiver uma solicitação de agendamento ou confirmação de data/horário.
- Se missing_criteria não estiver vazio, ready_to_schedule é false e blocking_reason descreve qual \
critério está faltando em português.
- Se ready_to_schedule for true, resolva expressões temporais relativas ("amanhã", "terça", \
"semana que vem") para YYYY-MM-DD usando a data de hoje como referência.
- Valide que a data é dia útil (segunda a sexta) e o horário está entre 10h e 17h — se não estiver, \
ready_to_schedule é false e blocking_reason explica o problema.

Regras para action_json — monte conforme o caso:
- Lead pediu retorno em horário específico:
  {"acao": "agendar_retorno", "horario": "HH:MM", "mensagem": ""}
- Lead mencionou dia mas não informou horário:
  {"acao": "consultar_disponibilidade", "data": "YYYY-MM-DD"}
- Lead confirmou data e horário:
  {"acao": "agendar_reuniao", "data": "YYYY-MM-DD", "horario": "HH:MM", "nome": "", "email": ""}
  Se nome ou email não estiverem disponíveis, deixe strings vazias e ready_to_schedule deve ser \
false com blocking_reason informando o que falta.
- Se nenhuma das situações acima se aplicar, action_json é null.

Formato de resposta obrigatório:
{
  "ready_to_schedule": bool,
  "blocking_reason": string | null,
  "resolved_date": string | null,
  "resolved_time": string | null,
  "action_json": object | null
}\
"""


def run_scheduling_validator(
    lead_message: str,
    qualification: dict,
    today: str,
) -> dict:
    """
    Valida se o lead está pronto para agendamento e resolve datas relativas.

    Args:
        lead_message: Mensagem atual do lead.
        qualification: Output do QualificationTrackerAgent.
        today: Data atual no formato YYYY-MM-DD.

    Returns:
        Dict com ready_to_schedule, blocking_reason, resolved_date, resolved_time e action_json.
    """
    user_content = (
        f"Data de hoje: {today}\n\n"
        f"Critérios de qualificação:\n{json.dumps(qualification, ensure_ascii=False, indent=2)}\n\n"
        f"Mensagem do lead: {lead_message}"
    )

    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_content),
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
        # Fallback: lead não está pronto para agendar
        return {
            "ready_to_schedule": False,
            "blocking_reason": "Não foi possível validar o estado de agendamento",
            "resolved_date": None,
            "resolved_time": None,
            "action_json": None,
        }
