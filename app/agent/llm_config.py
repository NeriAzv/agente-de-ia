"""
Configuração centralizada dos LLMs do projeto.

Em vez de cada agente instanciar ChatOpenAI com o modelo hardcoded, todos
consomem as factories abaixo. Trocar de modelo ou ajustar temperatura/streaming
passa a ser um único ponto de mudança — e os modelos são configuráveis por
variável de ambiente.
"""
import os
from langchain_openai import ChatOpenAI

# Modelos configuráveis por env var, com defaults seguros.
MODEL_MAIN = os.getenv("LLM_MODEL_MAIN", "gpt-4o")
MODEL_MICRO = os.getenv("LLM_MODEL_MICRO", "gpt-4o-mini")


def make_main_llm() -> ChatOpenAI:
    """LLM do agente principal (AnaAgent) — resposta conversacional via streaming."""
    return ChatOpenAI(model=MODEL_MAIN, temperature=0.7, streaming=True)


def make_micro_llm() -> ChatOpenAI:
    """LLM dos micro-agentes classificadores — saída determinística (temperature=0)."""
    return ChatOpenAI(model=MODEL_MICRO, temperature=0)
