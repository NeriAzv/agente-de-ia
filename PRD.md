# PRD — Agente de IA SDR Autônomo (Btime)

**Versão:** 1.0  
**Data:** 2026-04-13  
**Autor:** Guilherme Neri  
**Status:** Em produção

---

## 1. Visão Geral

### 1.1 Problema

Equipes de SDR humanas têm capacidade limitada de atender leads em volume, manter consistência de abordagem e operar 24/7. O custo de qualificar um lead manualmente (tempo, treinamento, erros de processo) é alto, e a taxa de follow-up adequado é frequentemente baixa.

### 1.2 Solução

Um agente de IA autônomo que opera via WhatsApp, assumindo o papel de SDR da Btime. O agente qualifica leads, lida com objeções, aquece o pitch e agenda reuniões com Google Meet — tudo sem intervenção humana. O vendedor humano só entra na reunião para fechar.

### 1.3 Objetivo Principal

Entregar leads **completamente preparados** para fechamento de contrato antes da reunião com o comercial. O agente faz o trabalho pesado; o humano fecha.

---

## 2. Personas

### Lead (Usuário Final)
- Decisor ou influenciador em empresa B2B brasileira
- Recebe ou inicia contato via WhatsApp
- Pode ter recebido outbound (prospecção ativa) ou ser inbound (conhece a Btime)
- Expectativa: ser atendido de forma ágil, consultiva e sem enrolação

### Time Comercial Btime (Operador)
- Recebe leads já qualificados e com reunião agendada no Google Calendar
- Pode bloquear o agente manualmente via flag `ai_blocked` no lead_info.json
- Confia que o agente não vai "queimar" o lead

---

## 3. Casos de Uso

| # | Caso de Uso | Ator | Descrição |
|---|-------------|------|-----------|
| UC-01 | Atender inbound | Lead | Lead manda mensagem, agente responde com qualificação e pitch |
| UC-02 | Prospecção outbound | Agente | Agente inicia conversa com lead frio via endpoint `/iniciar-conversa` |
| UC-03 | Qualificar lead | Agente | Identifica faturamento, dor, segmento e produto ideal |
| UC-04 | Lidar com objeções | Agente | Detecta e responde contextualmente a 8 tipos de objeção |
| UC-05 | Agendar reunião | Agente | Valida critérios, cria evento no Google Calendar com Meet link |
| UC-06 | Follow-up automático | Agente | Envia mensagens de retomada após 1h, 24h, 15 dias de silêncio |
| UC-07 | Processar mídia | Agente | Transcreve áudio, analisa imagens/vídeos, extrai texto de documentos |
| UC-08 | Respeitar presença | Agente | Cancela/retoma resposta baseado no status de digitação do lead |
| UC-09 | Intervenção humana | Operador | Desativa agente via `ai_blocked=true` para conversa manual |
| UC-10 | Recontato de lead frio | Agente | Retoma contato com lead após longo silêncio (15 dias) |

---

## 4. Arquitetura do Sistema

### 4.1 Visão Macro

```
WhatsApp (Lead)
      │
      ▼
   Z-API
      │  webhook POST
      ▼
Flask HTTP Server (db_app.py :5001)
      │
      ├─ Salva mensagem em history.json
      ├─ Verifica ai_blocked
      ├─ Verifica presença (COMPOSING / PAUSED)
      │
      ▼
Agent_AI.get_ai_response() (core.py)
      │
      ├─ [PARALELO] 4 Micro-Agentes (Claude Haiku)
      │     ├─ ObjectionDetector
      │     ├─ IntentClassifier
      │     ├─ QualificationTracker
      │     └─ SchedulingValidator
      │
      ├─ [SE MÍDIA] Processamento de Mídia
      │     ├─ Áudio → Whisper
      │     ├─ Imagem → GPT-4o Vision
      │     ├─ Vídeo → ffmpeg + Whisper + GPT-4o Vision
      │     └─ Documento → openpyxl / python-docx / PyMuPDF
      │
      ├─ Enriquecimento de contexto (lead_info.json + horários livres)
      │
      ├─ [GERAÇÃO] AnaAgent (Claude Sonnet 4.6)
      │
      ├─ [SE AGENDAMENTO] Criar evento Google Calendar + Meet link
      │
      ├─ Split response em "bolhas" WhatsApp
      ├─ Cálculo de delay de digitação por parte
      └─ Envio via Z-API com delayTyping
            │
            └─ Agendamento de follow-ups (1h, 24h, 15d)
```

### 4.2 Componentes

| Componente | Arquivo | Responsabilidade |
|------------|---------|------------------|
| Servidor HTTP | `app/db_app.py` | Recebe webhooks Z-API, rota mensagens, gerencia presença |
| Orquestrador | `app/agent/core.py` | Coordena todo o fluxo de processamento e envio |
| Contexto/Persona | `app/agent/context.py` | System prompts, regras de conversa, metodologia de vendas |
| Agente Principal | `app/agent/ana_agent.py` | Geração de resposta via Claude Sonnet 4.6 (streaming) |
| Micro-Agentes | `app/agent/micro_agents.py` | Execução paralela dos 4 classificadores |
| Detector de Objeções | `app/agent/objection_detector.py` | Classifica objeções de vendas |
| Classificador de Intenção | `app/agent/intent_classifier.py` | Detecta intenção da mensagem |
| Qualificação | `app/agent/qualification_tracker.py` | Avalia status de qualificação do lead |
| Validador de Agendamento | `app/agent/scheduling_validator.py` | Verifica prontidão para agendar + resolve datas relativas |
| Calendário | `app/agent/calendar.py` | Google Calendar API — criar, verificar, deletar eventos |
| Normalizadores | `app/agent/normalizers.py` | Parsing de datas/horários relativos |

---

## 5. Integrações

| Serviço | Função | Protocolo |
|---------|--------|-----------|
| Z-API | Envio/recebimento de mensagens WhatsApp | REST webhooks + HTTP POST |
| Claude Sonnet 4.6 (Anthropic) | Geração de resposta principal | LangChain |
| Claude Haiku 4.5 (Anthropic) | 4 micro-agentes classificadores | LangChain (paralelo) |
| GPT-4o-mini (OpenAI) | Follow-ups, outreach, recontatos | LangChain ChatOpenAI |
| GPT-4o Vision (OpenAI) | Análise de imagens e vídeos | OpenAI Vision API |
| Whisper (OpenAI) | Transcrição de áudios | OpenAI Audio API |
| Google Calendar | Agendamento de reuniões, disponibilidade | OAuth2, Google API Client |
| Google Meet | Links de videoconferência | Gerado automaticamente pelo Calendar API |
| ffmpeg/ffprobe | Extração de frames/áudio de vídeos | Subprocess |

---

## 6. Modelos de Dados

### 6.1 lead_info.json
```json
{
  "nome": "string",
  "email": "string | null",
  "empresa": "string | null",
  "segmento_mercado": "string | null",
  "faturamento_aproximado": "string | null",
  "tamanho_time": "string | null",
  "sistemas_atuais": ["string"],
  "desafios_identificados": ["string"],
  "produto_indicado": "Squad AI | SaaS Btime | Ambos | null",
  "estagio_conversa": "inicial | qualificando | qualificado | proposta | agendado",
  "reuniao_agendada": "boolean",
  "necessita_followup": "boolean",
  "motivo_followup": "string",
  "followups_agendados": [
    {
      "tipo": "1h | 24h | 15d",
      "horario_iso": "ISO timestamp",
      "phone": "string"
    }
  ],
  "ai_blocked": "boolean",
  "atualizado_em": "ISO timestamp"
}
```

### 6.2 history.json (por chatLid)
```json
[
  {
    "timestamp": "number",
    "data": {
      "chatLid": "string",
      "phone": "string",
      "fromMe": "boolean",
      "messageId": "string",
      "text": { "message": "string" },
      "audio": { "audioUrl": "string", "mimeType": "string", "transcricao": "string" },
      "image": { "imageUrl": "string", "caption": "string", "descricao": "string" },
      "video": { "videoUrl": "string", "caption": "string", "descricao": "string" },
      "document": { "documentUrl": "string", "mimeType": "string", "conteudo": "string" }
    }
  }
]
```

### 6.3 reunioes.json
```json
[
  {
    "chatLid": "string",
    "data_reuniao": "YYYY-MM-DD",
    "horario_reuniao": "HH:MM",
    "nome_lead": "string",
    "email_lead": "string",
    "meet_link": "string",
    "event_id": "string",
    "criada_em": "ISO timestamp"
  }
]
```

---

## 7. Fluxo de Conversa (Funil de 5 Etapas)

### Etapa 1 — Primeiro Contato
- **Inbound:** Lead já conhece a Btime → ir direto para discovery
- **Outbound:** Se apresentar + posicionar Btime + pergunta aberta sobre o negócio

### Etapa 2 — Discovery + Qualificação
- Identificar dores do lead de forma consultiva (sem interrogatório)
- Descobrir: segmento, faturamento (~R$4M+/ano para Squad AI), tamanho do time, sistemas atuais
- Mapear se dor é automatizável e se há orçamento/momento

### Etapa 3 — Aquecimento + Pitch
- **Squad AI** (faturamento R$4M+, dores complexas): time como extensão, headcount evitado, entrega contínua de projetos, cases (VIVO, Hub Logística SP, Seconci-SP)
- **SaaS Btime** (empresas menores, dores simples): padronização, visibilidade, transformação digital
- Pitch contextualizado à dor específica do lead; nunca genérico

### Etapa 4 — CTA + Agendamento
- Propor data/horário específico (não "quando você pode?")
- Coletar nome completo + e-mail antes de confirmar
- Criar evento no Google Calendar com Meet link
- Confirmar reunião com link no WhatsApp

### Etapa 5 — Follow-up
- Timers de 1h, 24h, 15d após silêncio do lead
- Tom leve, nunca agressivo
- Cada mensagem tem uma razão contextual (nova informação, pergunta útil, lembrete)

---

## 8. Regras de Negócio

### 8.1 Critérios de Qualificação (todos obrigatórios para agendar)
1. **Faturamento confirmado** — sabe o porte da empresa
2. **Dor identificada e automatizável** — há um problema real que a Btime resolve
3. **Produto roteado** — definido se é Squad AI ou SaaS Btime
4. **Lead convencido** — sem objeções pendentes, pronto para a reunião

### 8.2 Regras de Agendamento
- Apenas dias úteis (segunda a sexta)
- Horário comercial: 10h–17h
- Duração padrão: 30 minutos
- Resolução de datas relativas antes de confirmar ("amanhã" → "2026-04-14")
- Verificação de conflito no Google Calendar antes de criar
- Exige nome completo + e-mail do lead

### 8.3 Tipos de Objeção Tratados
| Tipo | Abordagem |
|------|-----------|
| Preço | ROI; custo de não resolver; alinhar com percepção de orçamento |
| Timing | Investigar o que bloqueia; respeitar se genuíno; marcar check-in |
| Decisor | Oferecer material para facilitar; incluir no call |
| Já tem solução | Reposicionar como extensão/integração |
| Má experiência anterior | Ouvir sem descartar; explicar diferença de abordagem |
| Quer proposta | Redirecionar para conversa (precisa de contexto completo) |
| Quer POC | Explicar que não é padrão; explorar o que validar |
| TI interna | Reposicionar como acelerador, não ameaça |

### 8.4 Regras de Comunicação
- Sem emojis
- Sem abreviações ("vc", "pq", "tbm", "oq", "né", "tô", "pro")
- Máximo 3 linhas por mensagem; partes separadas em bolhas
- Sem markdown, listas ou negrito
- Sem frases de chatbot ("Como posso ajudá-lo?")
- Nunca afirmar ser IA (se perguntado, dizer que é humano)
- Sem travessões longos (—); usar ponto, vírgula, dois-pontos
- Sem "rs", "kkk", "haha"
- Sem linguagem corporativa excessiva ("prezado", "conforme alinhado", "segue em anexo")

---

## 9. Comportamento Técnico

### 9.1 Processamento de Presença
| Status Z-API | Ação do Agente |
|-------------|----------------|
| `COMPOSING` | Cancela timer de resposta pendente; adiciona chatLid ao composing_set |
| `RECORDING` | Mesmo que COMPOSING |
| `PAUSED` | Se resposta pendente → retoma (lead parou de digitar) |
| `AVAILABLE` | Mesma lógica de PAUSED |
| `UNAVAILABLE` | Se estava digitando → retoma resposta |

### 9.2 Simulação de Digitação Humana
- Delay base por resposta: `min(3 + len(texto) * 0.04, 12)` segundos
- Delay por bolha: `max(1, min(delay_calculado, 15))` segundos
- Cada bolha enviada com `delayTyping` para simular digitação real

### 9.3 Timers de Follow-up
- Persistidos em `lead_info.json["followups_agendados"]`
- Restaurados automaticamente ao reiniciar o servidor
- Cancelados quando lead responde
- Apenas disparados se `necessita_followup=true`

### 9.4 Concorrência e Thread Safety
- 4 micro-agentes executados em paralelo via `ThreadPoolExecutor(max_workers=3)`
- `_file_lock` para escritas thread-safe em `history.json`
- Estado global gerenciado em dicionários compartilhados:
  - `answer_list` — respostas pendentes por chatLid
  - `pending_timers` — timers de delay de resposta
  - `followup_timers` — timers de follow-up por chatLid
  - `composing_set` — chatLids digitando no momento
  - `processing_chats` — chatLids com processamento em andamento

---

## 10. Endpoints HTTP

| Método | Rota | Função |
|--------|------|--------|
| POST | `/webhook/receive` | Recebe mensagens do Z-API |
| POST | `/webhook/presence` | Recebe eventos de presença (digitando, gravando) |
| POST | `/webhook/message-status` | Confirmação de entrega/leitura |
| POST | `/webhook/connect` | Eventos de conexão |
| POST | `/webhook/send` | Confirmação de envio de mensagem do agente |
| POST | `/iniciar-conversa` | Inicia conversa outbound com lead |
| POST | `/chamar-lead` | Chamada manual a lead existente |

---

## 11. Configuração e Ambiente

### 11.1 Variáveis de Ambiente (.env)
```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
ZAPI_SEC_TOKEN=...
```

### 11.2 Arquivos de Credenciais
- `app/client_secret.json` — OAuth2 Google Desktop App (não versionado)
- `app/token.json` — Token OAuth2 gerado automaticamente na primeira execução

### 11.3 Stack Tecnológica
| Camada | Tecnologia |
|--------|-----------|
| HTTP Server | Flask 3.1.3 |
| WhatsApp | Z-API |
| LLM Principal | Claude Sonnet 4.6 (Anthropic) |
| LLM Micro-Agentes | Claude Haiku 4.5 (Anthropic) |
| LLM Follow-up | GPT-4o-mini (OpenAI) |
| Transcrição | Whisper (OpenAI) |
| Visão | GPT-4o Vision (OpenAI) |
| Calendário | Google Calendar API (OAuth2) |
| Processamento de Mídia | ffmpeg, python-docx, openpyxl, PyMuPDF |
| Orquestração LLM | LangChain, LangGraph |
| Runtime | Python 3.x, multi-threaded |
| Exposição local | ngrok |

---

## 12. Métricas de Sucesso

| Métrica | Descrição |
|---------|-----------|
| Taxa de qualificação | % de leads que chegam à etapa de produto roteado |
| Taxa de agendamento | % de leads qualificados que agendam reunião |
| Taxa de comparecimento | % de reuniões que acontecem (dado externo ao agente) |
| Taxa de follow-up efetivo | % de leads que retomam conversa após follow-up automático |
| Tempo médio de qualificação | Tempo do primeiro contato até produto roteado |
| Taxa de objeções resolvidas | % de objeções detectadas que foram superadas |
| Uptime do agente | Disponibilidade do servidor Flask + timers |

---

## 13. Limitações Conhecidas

- **Persistência em JSON:** Todos os dados são armazenados em arquivos `.json` locais — sem banco de dados. Escalabilidade horizontal é limitada.
- **Sem painel de monitoramento:** Não há dashboard para visualizar leads, funil ou métricas em tempo real.
- **Sem autenticação nos webhooks:** Endpoints `/webhook/*` não têm validação de assinatura além do `ZAPI_SEC_TOKEN`.
- **ngrok para exposição:** Ambiente de produção depende de ngrok ou solução similar para expor o servidor local.
- **Google OAuth2 manual:** A autorização inicial do Google Calendar requer interação humana para gerar `token.json`.
- **Sem retry robusto:** Falhas de envio via Z-API não têm mecanismo de retry automático.
- **Sem multi-tenant:** O agente está configurado para uma única conta Z-API e um único organizador de calendário.

---

## 14. Evolução Futura (Backlog)

| Prioridade | Item |
|-----------|------|
| Alta | Migrar persistência para banco de dados (PostgreSQL ou Redis) |
| Alta | Painel web para monitoramento de leads e funil |
| Alta | Autenticação HMAC nos webhooks Z-API |
| Média | Deploy em servidor próprio (sem depender de ngrok) |
| Média | Suporte a múltiplas contas Z-API (multi-tenant) |
| Média | Retry automático para falhas de envio |
| Baixa | Integração com CRM (HubSpot, Pipedrive) |
| Baixa | Testes automatizados para os micro-agentes |
| Baixa | Logging estruturado (substituir prints coloridos por structured logs) |
