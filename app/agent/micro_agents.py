from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from langchain_core.messages import HumanMessage, AIMessage

from agent.objection_detector import run_objection_detector
from agent.intent_classifier import run_intent_classifier
from agent.qualification_tracker import run_qualification_tracker
from agent.scheduling_validator import run_scheduling_validator


def _to_langchain_messages(mensagens_formatadas: list) -> list:
    """Converte lista de dicts {role, content} para HumanMessage/AIMessage."""
    result = []
    for m in mensagens_formatadas:
        if m["role"] == "user":
            result.append(HumanMessage(content=m["content"]))
        else:
            result.append(AIMessage(content=m["content"]))
    return result


def run_micro_agents(lead_message: str, mensagens_formatadas: list) -> dict:
    """
    Executa os 4 micro agentes e retorna o micro_agent_context consolidado.

    Estratégia de paralelismo:
      - Fase 1 (paralelo): ObjectionDetector, IntentClassifier, QualificationTracker
      - Fase 2 (sequencial): SchedulingValidator (depende do output do QualificationTracker)

    Args:
        lead_message: Mensagem atual do lead.
        mensagens_formatadas: Histórico no formato [{"role": ..., "content": ...}].

    Returns:
        Dict com as chaves: objection, intent, qualification, scheduling.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    history = _to_langchain_messages(mensagens_formatadas)

    with ThreadPoolExecutor(max_workers=3) as executor:
        fut_objection = executor.submit(run_objection_detector, lead_message)
        fut_intent = executor.submit(run_intent_classifier, lead_message)
        fut_qualification = executor.submit(run_qualification_tracker, history)

    objection = fut_objection.result()
    intent = fut_intent.result()
    qualification = fut_qualification.result()

    scheduling = run_scheduling_validator(lead_message, qualification, today)

    return {
        "objection": objection,
        "intent": intent,
        "qualification": qualification,
        "scheduling": scheduling,
    }
