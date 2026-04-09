import json
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from agent.context import get_contexto


_LLM = ChatAnthropic(
    model="claude-sonnet-4-6",
    temperature=0.7,
    streaming=True,
)

_MICRO_AGENT_RULES = """\
CONTEXTO DOS MICRO AGENTES, use para tomar decisões mais precisas antes de responder:

{micro_agent_context}

Regras de prioridade:
- Se objection.has_objection for true, trate a objeção antes de qualquer outra ação.
- Se intent.intent for scheduling_request, verifique scheduling.ready_to_schedule antes de avançar. \
se for false, use scheduling.blocking_reason para continuar o aquecimento.
- Se qualification.missing_criteria não estiver vazio, não avance para agendamento e foque em \
preencher os critérios faltantes.\
"""


def run_ana_agent(
    lead_message: str,
    history: list,
    micro_agent_context: dict,
    system_context: str | None = None,
) -> str:
    """
    Executa o AnaAgent e retorna a resposta completa via streaming.

    Args:
        lead_message: Mensagem atual do lead.
        history: Lista de HumanMessage/AIMessage com o histórico da conversa.
        micro_agent_context: Dict com as chaves objection, intent, qualification e scheduling.
        system_context: System prompt enriquecido (com lead_info, slots de agenda etc.).
                        Se None, usa get_contexto() diretamente.

    Returns:
        Texto gerado pelo agente.
    """
    micro_context_json = json.dumps(micro_agent_context, ensure_ascii=False, indent=2)
    micro_rules = _MICRO_AGENT_RULES.format(micro_agent_context=micro_context_json)

    base_context = system_context if system_context is not None else get_contexto()

    messages = [
        SystemMessage(content=base_context),
        SystemMessage(content=micro_rules),
        *history,
        HumanMessage(content=lead_message),
    ]

    response_text = ""
    for chunk in _LLM.stream(messages):
        if chunk.content:
            response_text += chunk.content

    return response_text
