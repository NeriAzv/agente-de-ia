# Agente de IA — SDR Autônomo para WhatsApp

Agente inteligente de vendas que atua como SDR via WhatsApp. Conduz conversas de qualificação B2B, detecta objeções, valida prontidão para agendamento e cria reuniões com Google Meet de forma totalmente autônoma.

## Fluxo de uma mensagem

```
WhatsApp (lead)
     │
     ▼
Z-API  →  POST /webhook/receive
     │
     ▼
Flask (db_app.py — porta 5001)
     │  salva history.json por chatLid
     │  aguarda delay dinâmico (3–12s)
     ▼
Agent_AI.get_ai_response()
     │
     ├── [paralelo, Claude Haiku]
     │     ├── ObjectionDetector    → tipo e gravidade da objeção
     │     ├── IntentClassifier     → intenção da mensagem
     │     └── QualificationTracker → critérios preenchidos / faltando
     │
     ├── [sequencial, Claude Haiku]
     │     └── SchedulingValidator  → ready_to_schedule + blocking_reason
     │
     ├── AnaAgent (Claude Sonnet) → gera a resposta final com contexto enriquecido
     │
     ├── verificar_reuniao()      → se resposta contém JSON de agendamento:
     │     └── cria evento Google Calendar + Google Meet
     │
     └── Z-API  →  envia resposta em partes para o lead
```

## Contexto injetado em cada resposta

- Dados conhecidos do lead (`lead_info.json`): nome, email, segmento, etc.
- Horários livres dos próximos 3 dias úteis (consultados ao vivo no Google Calendar)
- Output consolidado dos 4 micro agentes

## Modelos usados por função

| Função | Modelo |
|--------|--------|
| Resposta principal | Claude Sonnet 4.6 (AnaAgent) |
| Micro agentes (análise paralela) | Claude Haiku 4.5 |
| Follow-up de inatividade (1h / 24h / 15d) | GPT-4o-mini |
| Abertura outbound / recontato | GPT-4o-mini |
| Transcrição de áudio | OpenAI Whisper |
| Análise de imagem | GPT-4o Vision |
| Análise de vídeo (frames + áudio) | GPT-4o Vision + Whisper + ffmpeg |

## Tipos de mídia suportados

- **Texto** — fluxo principal
- **Áudio** — transcrito via Whisper antes de processar
- **Imagem** — descrita via GPT-4o Vision, descrição entra no contexto
- **Vídeo** — frames extraídos via ffmpeg (até 2min30s) + áudio transcrito; vídeos maiores ignoram frames
- **Documentos** — `.docx`, `.xlsx`, `.csv`, `.pdf` são lidos e o conteúdo entra no contexto

## Follow-up automático por inatividade

Após cada resposta enviada, o agente agenda 3 timers por lead:

| Timer | Disparo |
|-------|---------|
| 1h | 1 hora sem resposta |
| 24h | 24 horas sem resposta |
| 15d | 15 dias sem resposta |

Os timers são persistidos em `lead_info.json` e restaurados se o servidor reiniciar. Quando o lead responde, todos os timers são cancelados automaticamente. O campo `necessita_followup: false` no `lead_info.json` desativa os follow-ups para aquele lead.

## Fluxo de qualificação

1. **Saudação** — coleta nome e segmento
2. **Descoberta** — desafios e sistemas atuais
3. **Qualificação** — faturamento, tamanho do time
4. **Roteamento de produto** — `squad_ai` (faturamento ≥ R$4M/ano) ou `saas_btime`
5. **Pitch** — direcionado à dor identificada
6. **Demo** — convite para demonstração
7. **Agendamento** — capta data/hora e cria evento com Google Meet

## Stack

| Camada | Tecnologia |
|--------|-----------|
| API HTTP | Flask |
| WhatsApp | Z-API (webhooks) |
| Exposição local | ngrok |
| Resposta principal | LangChain + Anthropic Claude Sonnet 4.6 |
| Micro agentes | Anthropic Claude Haiku 4.5 |
| Follow-up / mídia | OpenAI GPT-4o-mini / GPT-4o / Whisper |
| Calendário | Google Calendar API (OAuth2) |
| Extração de vídeo | ffmpeg / ffprobe |

## Estrutura de arquivos

```
agente-de-ia/
├── app/
│   ├── db_app.py                      # Flask + webhooks
│   ├── .env                           # Chaves de API (não commitado)
│   ├── client_secret.json             # OAuth2 Google (não commitado)
│   ├── token.json                     # Token OAuth Google (gerado no 1º login)
│   ├── reunioes.json                  # Reuniões agendadas (runtime)
│   └── agent/
│       ├── core.py                    # Agent_AI — orquestração principal
│       ├── micro_agents.py            # Executa os 4 micro agentes em paralelo
│       ├── intent_classifier.py       # Haiku: classifica intenção da mensagem
│       ├── objection_detector.py      # Haiku: detecta e tipifica objeções
│       ├── qualification_tracker.py   # Haiku: avalia critérios de qualificação
│       ├── scheduling_validator.py    # Haiku: valida prontidão para agendar
│       ├── ana_agent.py               # Sonnet: gera a resposta final
│       ├── calendar.py                # Google Calendar (criar/deletar/verificar eventos)
│       ├── context.py                 # System prompt, instruções e estado
│       └── normalizers.py             # Normalização de datas e horários
└── chats/
    └── {chatLid}/
        ├── history.json               # Histórico completo da conversa
        └── lead_info.json             # Dados do lead + followups agendados
```

## Variáveis de ambiente

```env
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
ZAPI_SEC_TOKEN=...
```

## Como rodar

### 1. Instalar dependências

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configurar Google Calendar

Coloque o `client_secret.json` (OAuth2 Desktop App) em `app/`. Na primeira execução o browser abrirá para autenticação e o `token.json` será gerado automaticamente.

### 3. Subir o servidor

```bash
cd app
python db_app.py
```

Sobe na porta **5001**.

### 4. Expor via ngrok

```bash
ngrok http 5001
```

Configure a URL gerada como webhook no painel do Z-API.

### 5. Webhooks no Z-API

| Evento | Endpoint |
|--------|----------|
| Mensagem recebida | `POST /webhook/receive` |
| Presença (digitando/gravando) | `POST /webhook/presence` |
| Status de mensagem | `POST /webhook/message-status` |
| Conexão | `POST /webhook/connect` |

## Endpoint extra

`POST /iniciar-conversa` — inicia conversa outbound com lead que ainda não tem histórico.

```json
{ "phone": "5511999999999", "chatLid": "5511999999999@lid" }
```
