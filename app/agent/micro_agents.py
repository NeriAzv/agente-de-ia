from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from langchain_core.messages import HumanMessage, AIMessage

from agent.objection_detector import run_objection_detector
from agent.intent_classifier import run_intent_classifier
from agent.qualification_tracker import run_qualification_tracker
from agent.scheduling_validator import run_scheduling_validator
from agent.closure_detector import run_closure_detector
from agent.handoff_detector import run_handoff_detector


def _assistant_content_text_only(content):
    """
    Anthropic rejeita blocos não-texto (image/image_url) em turnos de assistente.
    Reduz qualquer conteúdo estruturado a string com só os trechos de texto.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        partes = []
        for bloco in content:
            if isinstance(bloco, dict):
                if bloco.get("type") == "text" and bloco.get("text"):
                    partes.append(str(bloco["text"]))
            elif isinstance(bloco, str):
                partes.append(bloco)
        return "\n".join(partes).strip() or "[mídia enviada]"
    return str(content) if content is not None else ""


def _to_langchain_messages(mensagens_formatadas: list) -> list:
    """Converte lista de dicts {role, content} para HumanMessage/AIMessage."""
    result = []
    for m in mensagens_formatadas:
        if m["role"] == "user":
            result.append(HumanMessage(content=m["content"]))
        else:
            result.append(AIMessage(content=_assistant_content_text_only(m["content"])))
    return result


def run_micro_agents(lead_message: str, mensagens_formatadas: list, has_active_meeting: bool = False) -> dict:
    """
    Executa os 6 micro agentes e retorna o micro_agent_context consolidado.

    Estratégia de paralelismo:
      - Fase 1 (paralelo): ObjectionDetector, IntentClassifier, QualificationTracker, ClosureDetector, HandoffDetector
      - Fase 2 (sequencial): SchedulingValidator (depende do output do QualificationTracker)

    Args:
        lead_message: Mensagem atual do lead.
        mensagens_formatadas: Histórico no formato [{"role": ..., "content": ...}].
        has_active_meeting: Indica se já existe reunião agendada ativa para o lead.

    Returns:
        Dict com as chaves: objection, intent, qualification, scheduling, closure, handoff.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    history = _to_langchain_messages(mensagens_formatadas)

    with ThreadPoolExecutor(max_workers=5) as executor:
        fut_objection = executor.submit(run_objection_detector, lead_message)
        fut_intent = executor.submit(run_intent_classifier, lead_message)
        fut_qualification = executor.submit(run_qualification_tracker, history)
        fut_closure = executor.submit(run_closure_detector, lead_message, has_active_meeting)
        fut_handoff = executor.submit(run_handoff_detector, lead_message)

    objection = fut_objection.result()
    intent = fut_intent.result()
    qualification = fut_qualification.result()
    closure = fut_closure.result()
    handoff = fut_handoff.result()

    scheduling = run_scheduling_validator(lead_message, qualification, today)

    return {
        "objection": objection,
        "intent": intent,
        "qualification": qualification,
        "scheduling": scheduling,
        "closure": closure,
        "handoff": handoff,
    }
