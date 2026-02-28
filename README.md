# Gestão de Empresa — Documentação Técnica

App macOS standalone para gestão de concertos e contabilidade: lê dados do Google Calendar, calcula distâncias, gere faturação, mapa de km e módulo contabilístico completo (IVA, Conta Corrente, Despesas).

---

## Arquitectura

| Camada | Tecnologia |
|---|---|
| UI nativa | PyWebView 4.4+ (WKWebView macOS) |
| Servidor local | Flask em `127.0.0.1:8765` |
| Templates | Jinja2 + Bootstrap 5.3.2 (CDN) |
| Calendário | Google Calendar API v3 (OAuth2) |
| Geocoding | Nominatim (OpenStreetMap) |
| Routing | OSRM (router.project-osrm.org) |
| Despesas | Google Sheets via gspread (Service Account) |
| Frontend | JavaScript puro (sem frameworks) |

**Princípio fundamental:** Flask corre numa thread daemon; PyWebView corre na thread principal com WKWebView a apontar para `http://127.0.0.1:8765`. Usa-se `127.0.0.1` explicitamente (não `localhost`) porque o WKWebView resolve `localhost` para `::1` (IPv6) e o Flask só escuta IPv4.

**Dados locais primeiro:** Os eventos do Google Calendar são sincronizados manualmente para `data/concerts_base.json`. As despesas do Google Sheets são sincronizadas para `data/despesas.json`. Todas as páginas lêem ficheiros locais — sem chamadas de rede em cada carregamento de tab.

---

## Estrutura de ficheiros

```
EMPRESA/
├── app.py                        # Servidor Flask + toda a lógica
├── requirements.txt              # Dependências Python
├── start.sh                      # Script de arranque (cria venv, instala deps, exec python)
├── credentials.json              # Credenciais OAuth Google (não versionar)
├── token.pickle                  # Token OAuth guardado (não versionar)
├── Gestão de Empresa.app         # Bundle macOS (AppleScript compilado, sem terminal)
│
├── templates/
│   ├── _nav.html                 # Navegação partilhada (topbar + tabs + botões sync/tema)
│   ├── concerts.html             # Tab Concertos
│   ├── mapa_km.html              # Tab Mapa KM
│   ├── faturacao.html            # Tab Faturação
│   ├── iva.html                  # Tab IVA (controlo IVA liquidado vs. dedutível)
│   ├── conta_corrente.html       # Tab Conta Corrente (P&L, IRC, pagamentos por conta)
│   ├── despesas.html             # Tab Despesas (faturas classificadas por conta SNC)
│   ├── agencies.html             # Tab Agências
│   ├── conflitos.html            # Tab Conflitos
│   ├── auth.html                 # Página de autenticação Google
│   ├── auth_done.html            # Callback OAuth (mostra no browser)
│   ├── calendars.html            # Selecção de calendário
│   ├── setup.html                # Setup inicial (credentials.json)
│   └── error.html                # Página de erro com traceback
│
└── data/
    ├── concerts_base.json        # Eventos sincronizados do Google Calendar
    ├── concert_data.json         # Overrides do utilizador (artista, cachet, local, etc.)
    ├── distances_cache.json      # Cache de distâncias km (versão 2 = ida+volta)
    ├── agencies.json             # Agências e artistas
    ├── deleted_events.json       # IDs de eventos apagados (não reaparecem no sync)
    ├── config.json               # calendar_id e calendar_name
    ├── config_contab.json        # Configuração fiscal (taxas IRC, IVA, service account)
    ├── despesas.json             # Cache local das despesas do Google Sheets
    ├── despesas_overrides.json   # Overrides de categoria por despesa (não sobrescrito pelo sync)
    ├── secret_key                # Chave secreta Flask (binário)
    ├── app.log                   # Log de erros
    └── oauth_state.tmp           # Estado OAuth temporário (apagado após auth)
```

---

## Modelos de dados

### `data/concerts_base.json`
Base de dados local dos eventos do calendário. Populado pelo botão "↻ Sincronizar".
```json
{
  "last_sync": "19/02/2026 14:30",
  "events": {
    "<event_id>": {
      "start": "2025-06-15T21:00:00+01:00",
      "summary": "Artista | Evento, Local SUB Substituto"
    }
  }
}
```
Eventos criados manualmente têm ID com prefixo `local_<uuid>`.

### `data/concert_data.json`
Overrides do utilizador por `event_id`. Sobrepõe-se ao parsed do `summary`.
```json
{
  "<event_id>": {
    "artista": "Nome",
    "evento": "Nome do evento",
    "local": "Cidade, País",
    "substituto": "Nome",
    "cachet": "1500"
  }
}
```

### `data/distances_cache.json`
Cache de distâncias km (ida+volta). Versão 2 (v1 era só ida; migração automática ×2 no arranque).
```json
{
  "__version": 2,
  "Lisboa, Portugal": 324.6,
  "Porto, Portugal": 118.2
}
```

### `data/agencies.json`
```json
{
  "agencies": [
    {
      "id": "<uuid>",
      "nome": "Agência XYZ",
      "nif": "123456789",
      "artistas": [
        { "nome": "Artista A", "cachet_base": "1200" }
      ]
    }
  ]
}
```

### `data/config.json`
```json
{
  "calendar_id": "xxxxx@group.calendar.google.com",
  "calendar_name": "Concertos 2025"
}
```

### `data/config_contab.json`
Configuração fiscal, criada automaticamente na primeira chamada a `_get_contab_config()`.
```json
{
  "service_account_path": "/Users/.../FATURAS/service_account.json",
  "sheet_id": "<google_sheet_id>",
  "sheet_name": "Faturas",
  "taxa_iva_rendimentos": 23,
  "irc_taxa_reduzida": 16,
  "irc_limiar_reduzida": 50000,
  "irc_taxa_normal": 21,
  "taxa_derrama": 1.5
}
```
Editável via modal ⚙ na tab Conta Corrente (guardado por `PUT /api/contab_config`).

### `data/despesas_overrides.json`
Overrides de categoria do utilizador, indexados por chave composta `data_fatura|fornecedor|numero_fatura`. Sobrepõe-se ao valor `tipo_despesa` vindo do Sheets antes de `_enrich_despesas`.
```json
{
  "2025-01-15|EDP|FT 2025/1234": "Electricidade e Energia",
  "2025-02-01|NOS|FR 2025/0089": "Telecomunicações"
}
```

### `data/despesas.json`
Cache local das despesas do Google Sheets. Populado pelo botão "↻ Sync Despesas".
```json
{
  "last_sync": "24/02/2026 15:30",
  "rows": [
    {
      "data_fatura": "2025-01-15",
      "fornecedor": "EDP",
      "nif": "503504564",
      "numero_fatura": "FT 2025/1234",
      "descricao": "Electricidade Janeiro",
      "tipo_despesa": "Electricidade e Energia",
      "base_tributavel": 120.50,
      "base_6": 0, "iva_6": 0,
      "base_13": 0, "iva_13": 0,
      "base_23": 120.50, "iva_23": 27.72,
      "iva": 27.72, "total": 148.22, "moeda": "EUR"
    }
  ]
}
```

---

## Módulo de Contabilidade

### Pressupostos legais (Lei Portuguesa)

| Regra | Base legal | Implementação |
|---|---|---|
| IVA liquidado = cachet × 23% | CIVA | `cachet × (taxa_iva_rendimentos / 100)` |
| IVA dedutível — excepções | Art. 21.º n.º 1 CIVA | `_IVA_FACTOR` por categoria |
| Custo IRC = base + IVA não dedutível | Art. 23.º CIRC | `base_tributavel + iva_nao_deducivel` |
| Tributação autónoma 10% em representação | Art. 88.º n.º 7 CIRC | Sobre despesas de Alimentação/Hotelaria |
| Tributação autónoma 5% em compensações km | Art. 88.º n.º 9 CIRC | `km_val × 5%` (Mapa KM, não faturado a clientes) |
| IRC PME: 16% até €50k + 21% acima | OE 2025/2026 | `_calc_irc()`, taxas configuráveis |
| Derrama municipal 1,5% | Lei das Finanças Locais | Sobre resultado, configurável |
| Pagamentos por conta = (IRC+derrama)×80%/3 | Art. 104.º CIRC | Só se IRC anterior > €1.000 |
| Prazo DP IVA trimestral: dia 20 do 2.º mês | CIVA | Calculado em JS na tab IVA |
| Km → gasto dedutível: km × €0,40 | Portaria 467/2010 | Reusa dados do Mapa KM |

### Categorias de despesa e tratamento fiscal

| Categoria | Conta SNC | IVA dedutível | Representação |
|---|---|---|---|
| Telecomunicações | 6228 | 100% | Não |
| Electricidade e Energia | 6221 | 100% | Não |
| Água e Saneamento | 6221 | 100% | Não |
| Combustíveis e Lubrificantes | 6226 | **50%** (art. 21.º) | Não |
| Material de Escritório | 6224 | 100% | Não |
| Alimentação e Bebidas | 6227 | **0%** (art. 21.º) | **Sim** (TA 10%) |
| Alojamento e Hotelaria | 6227 | **0%** (art. 21.º) | **Sim** (TA 10%) |
| Transportes e Deslocações | 6227 | 100% | Não |
| Software e Tecnologia | 628 | 100% | Não |
| Publicidade e Marketing | 625 | 100% | Não |
| Seguros | 6229 | 100% | Não |
| Contabilidade e Consultoria | 6233 | 100% | Não |
| Serviços Jurídicos | 6232 | 100% | Não |
| Saúde e Bem-estar | 628 | 100% | Não |
| Formação e Educação | 628 | 100% | Não |
| Manutenção e Reparação | 624 | 100% | Não |
| Rendas e Alugueres | 6299 | 100% | Não |
| Outros | 628 | 100% | Não |

### Edição de categoria na tab Despesas

Na vista "Lista detalhada" da tab Despesas, a coluna **Categoria** é um dropdown editável com as 18 categorias da app. Ao alterar a categoria de uma despesa:

1. O novo valor é guardado em `data/despesas_overrides.json` (chave = `data_fatura|fornecedor|numero_fatura`)
2. A página recarrega — a despesa éreenriquecida com a nova conta SNC, factor IVA, tributação autónoma, etc.

Os overrides persistem entre syncs: o ficheiro `despesas_overrides.json` é independente de `despesas.json` e nunca é sobrescrito pela sincronização do Sheets.

### Dropdown de categoria no Google Sheets

O botão **"⚙ Dropdown Sheets"** (tab Despesas, filter bar) configura validação de dados nativa na coluna "Tipo Despesa" do spreadsheet de faturas. Operação única — não precisa de ser repetida a cada sync.

- Detecta o índice da coluna "Tipo Despesa" dinamicamente pelo cabeçalho
- Aplica `setDataValidation` (tipo `ONE_OF_LIST`) via Google Sheets API (`spreadsheet.batch_update`)
- `strict: false` — células com texto livre pré-existente não são invalidadas
- Requer que a service account tenha permissão de **Editor** no spreadsheet

### Sync de despesas (`/api/sync_despesas`)

Lê o Google Sheets da app FATURAS via gspread com `value_render_option='UNFORMATTED_VALUE'` (valores numéricos puros, sem formatação de locale). Normaliza e guarda em `data/despesas.json`.

> **Importante — valores numéricos:** `UNFORMATTED_VALUE` é obrigatório. gspread 6.x com `FORMATTED_VALUE` (default) trata vírgulas como separadores de milhar e converte `"0,55"` → `int("055")` = **55**, corrompendo os valores.

> **Importante — datas:** Com `UNFORMATTED_VALUE`, o Sheets API devolve datas como **números de série** (inteiro = dias desde 30/12/1899). A função `_sheets_date()` detecta se o valor é numérico e converte para `YYYY-MM-DD` usando a época do Sheets (`datetime(1899,12,30) + timedelta(days=serial)`). Fallback para strings `DD/MM/YYYY` e `YYYY-MM-DD`.

Autenticação via service account JSON reutilizado da app FATURAS (configurado em `config_contab.json`).

---

## Parse do título do evento

Formato esperado no Google Calendar:
```
Artista | Evento, Local SUB Substituto
```

- `|` separa artista do resto
- `,` separa evento do local
- `SUB` (palavra inteira) separa local do substituto
- Todos os campos são opcionais

Exemplos:
```
Banda X | Festival Y, Lisboa                  → artista=Banda X, evento=Festival Y, local=Lisboa
Banda X | Festival Y, Porto SUB João Silva    → substituto=João Silva, cachet=0 (automático)
Banda X                                       → só artista, resto vazio
```

**Regra do substituto:** Se `substituto` não está vazio, o `cachet` é forçado a `0` em toda a aplicação (faturação, totais, CSV).

---

## Fluxo de arranque

```
app.py __main__
  ├── Thread daemon → run_flask() → Flask em 127.0.0.1:8765
  ├── wait_for_flask() → polling urllib até Flask responder
  └── webview.create_window() → WKWebView aponta para APP_URL
```

No arranque, `_migrate_distances_cache()` converte automaticamente caches v1 (ida simples) para v2 (ida+volta, ×2).

---

## Autenticação Google OAuth

**Problema:** Google bloqueia OAuth em WebViews embebidos (WKWebView).

**Solução:** `webbrowser.open(auth_url)` abre o browser do sistema. O estado OAuth é guardado em `data/oauth_state.tmp` (ficheiro) em vez de sessão Flask, porque PyWebView e o browser do sistema têm cookies diferentes. A página `auth.html` faz polling a `/auth/status` de 1,5 em 1,5s e redireciona quando o token está pronto.

---

## Distâncias (km)

- Origem fixa: `"Rua de Macau, Coimbra, Portugal"`
- Geocoding: Nominatim → coordenadas lat/lon
- Routing: OSRM → distância em metros → ÷1000 × 2 (ida+volta)
- Cache em memória (`_distances_mem`) carregada uma vez do disco; persistida em `distances_cache.json`
- Distâncias são pré-calculadas durante o sync (`/api/sync`), nunca durante carregamento de página
- Taxa km: **€0,40/km**

---

## Rotas Flask

### Páginas
| Rota | Template | Descrição |
|---|---|---|
| `GET /` | — | Redirect para `/concerts` ou setup/auth |
| `GET /auth` | `auth.html` | Página de autenticação |
| `GET /auth/start` | — | Abre browser OAuth |
| `GET /auth/status` | — | JSON `{done: bool}` |
| `GET /oauth/callback` | `auth_done.html` | Callback OAuth |
| `GET /calendars` | `calendars.html` | Lista calendários |
| `POST /calendars/select` | — | Guarda calendário e redireciona |
| `GET /change_calendar` | — | Limpa config e redireciona |
| `GET /concerts` | `concerts.html` | Tab Concertos |
| `GET /mapa_km` | `mapa_km.html` | Tab Mapa KM |
| `GET /faturacao` | `faturacao.html` | Tab Faturação |
| `GET /iva` | `iva.html` | Tab IVA |
| `GET /conta_corrente` | `conta_corrente.html` | Tab Conta Corrente |
| `GET /despesas` | `despesas.html` | Tab Despesas |
| `GET /agencias` | `agencies.html` | Tab Agências |
| `GET /conflitos` | `conflitos.html` | Tab Conflitos |

### API
| Rota | Método | Descrição |
|---|---|---|
| `/api/sync` | POST | Sync com Google Calendar + pré-aquece distâncias |
| `/api/sync_despesas` | POST | Sync despesas do Google Sheets → `despesas.json` |
| `/api/despesas/set_categoria` | POST | Guarda override de categoria para uma despesa |
| `/api/despesas/setup_sheets_dropdown` | POST | Configura dropdown de categoria na coluna "Tipo Despesa" do Google Sheets |
| `/api/contabilidade` | GET | Dados contabilísticos consolidados (JSON) |
| `/api/contab_config` | GET | Lê configuração fiscal |
| `/api/contab_config` | PUT | Guarda configuração fiscal |
| `/api/update_concert` | POST | Edita campo de um concerto |
| `/api/add_concert` | POST | Cria novo concerto local |
| `/api/delete_concert` | POST | Remove concerto dos dados locais |
| `/api/export_csv` | POST | Escreve CSV em `~/Downloads/` |
| `/api/artistas` | GET | Lista artistas únicos |
| `/api/agencias` | GET/POST | Lista / cria agências |
| `/api/agencias/<id>` | PUT/DELETE | Edita / elimina agência |
| `/api/agencias/<id>/artistas` | POST/DELETE | Adiciona / remove artista |
| `/api/agencias/<id>/artistas/cachet` | PUT | Actualiza cachet_base do artista |
| `/api/agencias/<id>/artistas/refresh` | POST | Aplica cachet_base a concertos futuros |
| `/api/conflitos_count` | GET | Nº de eventos sobrepostos (badge da tab) |

---

## Lógica de negócio

### Sync (`/api/sync`)
1. Chama Google Calendar API (janela: −3 anos a +3 anos, máx 2500 eventos)
2. Actualiza `concerts_base.json`: eventos novos são adicionados; eventos existentes têm `start` e `summary` actualizados — overrides em `concert_data.json` nunca são tocados
3. Pré-aquece `distances_cache.json` para todos os locais conhecidos
4. Guarda `last_sync` com timestamp

### Sync Despesas (`/api/sync_despesas`)
1. Lê configuração de `config_contab.json` (service account, sheet ID)
2. Autentica com gspread via service account (sem OAuth)
3. Lê todas as linhas da worksheet com `value_render_option='UNFORMATTED_VALUE'`
4. Converte datas com `_sheets_date()` (ver abaixo), converte campos numéricos com `_to_float()`
5. Guarda em `despesas.json` com timestamp

### Construção da contabilidade (`_build_contabilidade`)
Agrega dados por `(year, month)` a partir de três fontes:
- **Rendimentos:** concertos sem substituto com cachet > 0 → base tributável + IVA liquidado
- **Gastos despesas:** `despesas.json` → base + IVA dedutível/não dedutível por categoria
- **Gastos km:** Mapa KM → km × €0,40
Aplica `_IVA_FACTOR` por categoria, calcula tributação autónoma por componente:
- `ta_representacao` = base representação × 10% (art. 88.º n.º 7)
- `ta_km` = gastos_km × 5% (art. 88.º n.º 9)
- `tributacao_autonoma` = `ta_representacao` + `ta_km` (total incluído em `irc_total`)

### Construção da lista de concertos (`_build_concerts_from_local`)
1. Lê `concerts_base.json` ordenado por `start` (ISO string, ordena lexicograficamente)
2. Para cada evento: parse do `summary` → aplica overrides de `concert_data.json` → cachet_base da agência se cachet vazio
3. Cachet forçado a `'0'` se `substituto` não estiver vazio
4. Km lido do cache em memória (sem HTTP)

### Refresh de cachet (`/api/agencias/<id>/artistas/refresh`)
Itera `concerts_base.json`, filtra concertos futuros (> hoje UTC) do artista, actualiza `cachet` em `concert_data.json`. Não usa Google Calendar API.

---

## Templates — variáveis Jinja2

Todas as páginas principais recebem:
- `calendar_name` — nome do calendário activo (para `_nav.html`)
- `last_sync` — timestamp da última sincronização do calendário

Tabs contabilísticas recebem adicionalmente:
- `despesas_last_sync` — timestamp do último sync de despesas (exibido na topbar)

### `_nav.html`
Incluído em todas as páginas com `{% set active_tab = 'nome_tab' %}` antes do include.
- Botão "↻ Sincronizar": sempre visível; chama `/api/sync`
- Botão "↻ Sync Despesas": visível apenas nas tabs `iva`, `conta_corrente`, `despesas`; chama `/api/sync_despesas`
- Botão ☀/🌙: alternância de tema

---

## Tema (modo claro / escuro)

A aplicação suporta modo claro e escuro, alternado pelo botão ☀/🌙 na topbar.

- **Persistência:** preferência guardada em `localStorage` (chave `theme`: `'light'` ou `'dark'`)
- **Inicialização:** cada template tem um script inline no `<head>` que aplica a classe `dark` ao elemento `<html>` antes do CSS ser processado — evita flash de tema errado ao carregar
- **Fallback:** se não houver preferência guardada, segue a preferência do sistema (`prefers-color-scheme`)
- **Implementação:** CSS inline em cada template com bloco `html.dark { ... }` de overrides; sem ficheiro CSS externo
- **Toggle:** `toggleTheme()` em `_nav.html`, partilhado por todas as páginas via `{% include %}`

---

## Badge de conflitos na navegação

A tab "Conflitos" mostra um badge vermelho com o número de eventos sobrepostos do ano corrente, calculado ao carregar qualquer página.

- **Definição:** dias com 2 ou mais eventos sem substituto → conta os eventos nesses dias
- **Filtro:** apenas o ano corrente (igual ao filtro por defeito da página)
- **Implementação:** `_nav.html` faz `fetch('/api/conflitos_count')` ao arranque; se `count > 0`, mostra o badge
- **Endpoint:** `GET /api/conflitos_count` → `{"count": N}`

---

## Exportação CSV

- **Faturação:** botão "⬇ CSV" por mês
- **Mapa KM:** botão "⬇ Exportar CSV" com filtro activo
- **IVA:** botão por trimestre e mensal
- **Conta Corrente:** botão "⬇ Exportar CSV" global
- **Despesas:** botão "⬇ Exportar CSV" com filtro activo

O JS envia o conteúdo via `POST /api/export_csv`; o Flask escreve o ficheiro directamente em `~/Downloads/`. Um `alert` confirma o nome do ficheiro guardado.

Formato: separador `;`, decimais com vírgula, BOM UTF-8 (para Excel PT abrir correctamente).

---

## Lançamento da app (macOS)

**Gestão de Empresa.app** é um bundle AppleScript compilado com `osacompile`.
O script dentro chama `do shell script "/path/to/start.sh"` — corre sem abrir terminal.

**start.sh:**
1. Cria `.venv` se não existir
2. Instala/actualiza dependências se `requirements.txt` for mais recente que o venv
3. `exec .venv/bin/python app.py` (substitui o processo shell por Python)

Se o Gatekeeper bloquear na primeira abertura: botão direito → **Abrir** → **Abrir**.

Para recompilar o .app:
```bash
osacompile -o "Gestão de Empresa.app" /tmp/launcher.applescript
xattr -cr "Gestão de Empresa.app"
```

---

## Configuração inicial (primeiro uso)

1. Ir à [Google Cloud Console](https://console.cloud.google.com/)
2. Criar projecto → activar **Google Calendar API**
3. Criar credenciais OAuth 2.0 → tipo "Aplicação de computador"
4. Adicionar redirect URI: `http://127.0.0.1:8765/oauth/callback`
5. Adicionar o e-mail como utilizador de teste no ecrã de consentimento
6. Fazer download do JSON → guardar como `credentials.json` na raiz de `EMPRESA/`
7. Abrir **Gestão de Empresa.app** → autenticar → escolher calendário → **↻ Sincronizar**
8. Configurar módulo contabilístico: tab Conta Corrente → ⚙ → preencher caminho do `service_account.json` e Sheet ID → **↻ Sync Despesas**

---

## Dependências Python

```
flask>=2.3.0
google-auth>=2.22.0
google-auth-oauthlib>=1.1.0
google-auth-httplib2>=0.1.1
google-api-python-client>=2.97.0
requests>=2.31.0
pywebview>=4.4.0
gspread>=5.0.0
```

---

## Problemas conhecidos e soluções

| Problema | Causa | Solução aplicada |
|---|---|---|
| Janela em branco ao arrancar | Flask ainda não iniciou | `wait_for_flask()` faz polling antes de criar a janela |
| OAuth não funciona no WKWebView | Google bloqueia OAuth em WebViews | Abre browser do sistema; estado OAuth guardado em ficheiro |
| `localhost` não resolve | WKWebView resolve para `::1` (IPv6) | Usar sempre `127.0.0.1` explicitamente |
| Porta 5000 ocupada | macOS AirPlay Receiver usa porta 5000 | App usa porta **8765** |
| App lenta a mudar de tab | Chamada à API Google em cada carregamento | Dados guardados localmente; sync só manual |
| Substituto com cachet errado | Concerto com substituto não deve faturar | `cachet` forçado a `'0'` quando `substituto != ''` |
| .app não abre (Gatekeeper) | App não assinada | Botão direito → Abrir; ou `xattr -cr app.app` |
| Valores decimais multiplicados por 100 no sync | gspread 6.x trata vírgulas como separadores de milhar: `"0,55"` → `55` | `get_all_records(value_render_option='UNFORMATTED_VALUE')` |
| Datas aparecem como números no sync | `UNFORMATTED_VALUE` devolve datas como números de série do Sheets | `_sheets_date()` converte serial → `YYYY-MM-DD` via época 30/12/1899 |
| "Internal Server Error" ao arrancar | Token OAuth expirado/revogado pelo Google (`invalid_grant`) — ocorre quando a app está em modo "teste" na Cloud Console e passaram 7 dias sem uso | `get_credentials()` apanha a excepção, apaga `token.pickle` automaticamente e redireciona para `/auth` |
