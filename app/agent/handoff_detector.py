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
lead — considerando também o turno anterior da Ana E as últimas mensagens do lead — e detecte \
se o lead está indicando OUTRA pessoa para conversar, fornecendo um telefone. Retorne apenas \
JSON puro, sem markdown, sem explicação.

CONTEXTO MULTI-TURNO (IMPORTANTE):
- O lead frequentemente quebra a indicação em várias mensagens curtas (burst). Ex.:
  msg-2: "chama meu amigo do comercial: 65 984010176"
  msg-1: "ele vai comprar um plano"
  msg-atual: "chama lá"
- Nesse caso, o telefone está nas mensagens anteriores do lead, NÃO na atual. Você DEVE \
juntar as últimas mensagens do lead e considerar o burst inteiro como uma única intenção \
de repasse. Se o conjunto contém nome+número OU pedido claro de "chama X" + número visível \
nas últimas mensagens, marque is_handoff=true com confidence="high" e use o telefone \
encontrado no burst.\


ACEITAR como handoff (is_handoff=true):
- "fala com o João: 11 98765-4321"
- "o responsável é a Maria, número 11 9..."
- "manda mensagem aí pra +55 21 99999-0000, é a Marina, sócia"
- "esse é o WhatsApp do meu sócio: ..."
- "quem cuida disso é o financeiro, número ..."
- Qualquer indicação explícita de "fala com X, número Y" / "manda pra Y".
- **Resposta a pedido da Ana:** se o turno anterior da Ana pediu explicitamente nome+telefone \
do responsável (ex.: "me passa o nome e o whatsapp dele", "qual o contato do responsável", \
"pode me mandar o número?") E a mensagem do lead contém nome + número (mesmo sem frase \
"fala com X"), trate como handoff com confidence="high". Ex.: lead responde "Logano, \
11979611556" após Ana perguntar pelo contato ⇒ is_handoff=true, high.

REJEITAR (is_handoff=false):
- Lead informando o PRÓPRIO número ("meu telefone é...", "me liga em...").
- Número de outra empresa sem instrução de falar com a pessoa.
- Telefones em assinatura, footer ou contexto genérico.
- Menção a pessoa sem telefone associado, ou telefone sem indicação de fala.

CUIDADO ESPECIAL com vCard:
- vCard encaminhado aparece como texto começando com "[O lead encaminhou ... card de contato. \
... Telefone(s): +5511...]". O telefone DENTRO do vCard é um candidato a handoff.
- Considere o BURST INTEIRO (mensagem atual + últimas mensagens do lead) ao avaliar contexto \
verbal. O vCard frequentemente vem em mensagem separada do verbal ("chama esse contato aqui" + \
vCard como msg seguinte). Isso conta como contexto verbal presente.
- Se houver indicação verbal de "chama esse contato", "manda mensagem pra esse aqui", "fala com \
ele", "responsável é X", "esse é o chefe", etc. em QUALQUER msg do burst (atual ou últimas) + \
um vCard com telefone ⇒ is_handoff=true, confidence="high".
- Se a Ana JÁ pediu o contato no turno anterior E o lead encaminha um vCard ⇒ confidence="high".
- vCard COMPLETAMENTE isolado (sem qualquer verbo no burst e sem pedido da Ana) ⇒ confidence \
máximo "medium". Esse é o único caso restrito.

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
"esposa", "comercial", etc.) ou null.
- Se houver MAIS DE UM número, escolha apenas o primeiro com indicação clara de fala.
- Só use confidence="high" quando houver telefone + indicação inequívoca de falar com a pessoa \
(ou resposta direta a pedido da Ana).\
"""


_DEFAULT_FALSE = {
    "is_handoff": False,
    "new_phone": None,
    "new_contact_name": None,
    "new_contact_role": None,
    "confidence": "low",
}


def run_handoff_detector(
    lead_message: str,
    last_assistant_message: str = "",
    is_vcard: bool = False,
    recent_lead_messages: list[str] | None = None,
) -> dict:
    """
    Detecta repasse explícito de contato pelo lead (indicação de outro número para falar).

    Args:
        lead_message: Mensagem atual do lead.
        last_assistant_message: Último turno da Ana, usado para detectar respostas a pedido
            explícito de contato (ex.: "Pode me passar o nome e o telefone dele?").
        is_vcard: True se o turno atual do lead é estruturalmente um vCard encaminhado
            (sem texto/áudio/imagem acompanhando). Detector usa para limitar confidence
            quando não há contexto verbal anterior pedindo o contato.
        recent_lead_messages: Últimas N mensagens do lead em ordem cronológica
            (mais antigas primeiro, mais recentes por último). NÃO inclui a atual.
            Permite o detector enxergar bursts onde o número veio em msg anterior e
            a atual é só "chama lá" / "chama agora".

    Returns:
        Dict com is_handoff, new_phone (normalizado E.164 sem '+'), new_contact_name,
        new_contact_role e confidence.
    """
    recent = recent_lead_messages or []
    if recent:
        recent_block = "\n".join(f"  - {m}" for m in recent)
    else:
        recent_block = "  (nenhuma)"

    contexto_usuario = (
        f"turno anterior da Ana: {last_assistant_message or '(nenhum)'}\n"
        f"últimas mensagens do lead (mais antigas primeiro):\n{recent_block}\n"
        f"is_vcard: {str(bool(is_vcard)).lower()}\n"
        f"mensagem ATUAL do lead: {lead_message}"
    )
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=contexto_usuario),
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
