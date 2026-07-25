# Архитектура системы Text2BI

## 1. Полный цикл данных (mermaid)

```mermaid
flowchart TD
    subgraph "Хранилище данных (Simple One / PostgreSQL)"
        DB[(База данных<br/>dash)]
        VM[(Витрины данных<br/>mart_*)]
    end

    subgraph "База настроек и конфигурации (dash_config)"
        CFG[(system_config<br/>ключ-значение)]
        PROMPTS[(prompts<br/>шаблоны)]
        CONNS[(connection_settings<br/>подключения)]
        USERS[(users<br/>пользователи)]
        PROVIDERS[(llm_providers<br/>провайдеры)]
        MODELS[(llm_models<br/>модели)]
        FALLBACKS[(llm_fallback<br/>фолбэки)]
        CASES[(report_prompt_cases<br/>бизнес-кейсы)]
        SAVED[(saved_queries<br/>сохраненные запросы)]
        REPORT_PROMPTS[(prompt_reports<br/>шаблоны отчетов)]
    end

    subgraph "Flask-приложение (dash_flask)"
        SCHEMA[Чтение схемы<br/>information_schema]
        DESC[Бизнес-описание БД<br/>db_desc]
        LLM[LLM-модель<br/>Bothub / Единая платформа]
        SQL_GEN[Генерация SQL<br/>Text-to-SQL]
        VALIDATE[Валидация SQL<br/>SELECT-only + LIMIT]
        EXECUTE[Исполнение SQL<br/>psycopg2]

        CHAT[Чат-бот<br/>контекст: SQL + данные + термины]
        TERMS[Бизнес-термины<br/>modify_sql_for_business_terms]

        CHARTS[Построение графиков<br/>Plotly]
        REPORTS[Генерация отчетов<br/>python-docx]

        EXPORT[Экспорт<br/>DOCX / XLSX / HTML / JPG / TXT]
    end

    subgraph "Пользователи"
        USER[Пользователь / Руководитель]
        ANALYST[Аналитик]
    end

    %% Data flow
    DB -->|Структура таблиц| SCHEMA
    VM -->|Данные для аналитики| DB
    DESC -->|Бизнес-контекст| SQL_GEN
    PROMPTS -->|Шаблоны| SQL_GEN
    PROMPTS -->|Шаблоны| CHAT
    PROMPTS -->|Шаблоны| REPORTS
    CASES -->|Выбор промпта| REPORTS
    CONNS -->|Параметры подключения| EXECUTE

    SCHEMA -->|Схема БД| SQL_GEN
    SQL_GEN -->|Сгенерированный SQL| VALIDATE
    VALIDATE -->|Проверенный SQL| EXECUTE
    EXECUTE -->|DataFrame| CHARTS
    EXECUTE -->|Данные| REPORTS
    EXECUTE -->|Результаты| CHAT

    CHAT -->|Уточняющие вопросы| LLM
    CHAT -->|Переформулировка| TERMS
    TERMS -->|SQL с бизнес-терминами| EXECUTE

    LLM -->|Ответы| CHAT
    LLM -->|Генерация| SQL_GEN
    LLM -->|Анализ| REPORTS

    CHARTS -->|Графики| REPORTS
    CHARTS -->|HTML / JPG| EXPORT
    REPORTS -->|DOCX| EXPORT
    CHAT -->|Ответы| EXPORT

    %% User access
    USER -->|Запросы| SQL_GEN
    USER -->|Диалог| CHAT
    ANALYST -->|Исследование| CHAT
    ANALYST -->|Модификация SQL| TERMS
```

## 2. Описание компонентов и потоков

### 2.1. Источник данных
- **Хранилище** (PostgreSQL / Simple One) содержит пользовательские данные и **витрины данных** (`mart_*`).
- Система автоматически читает структуру через `information_schema.columns`.
- Бизнес-описание (связи таблиц, бизнес-термины) хранится в `system_config.db_desc`.

### 2.2. Генерация и выполнение SQL
1. Пользователь задает вопрос на русском языке.
2. LLM получает: схему БД + бизнес-описание + примеры QA + промпт.
3. Генерируется SQL (Text-to-SQL).
4. Проходит валидацию (только SELECT/WITH, без опасных операций, LIMIT).
5. SQL выполняется против PostgreSQL.
6. Результат возвращается как DataFrame.

### 2.3. Чат-бот с контекстом
- Чат привязан к текущему SQL-запросу и данным.
- LLM может: объяснить SQL, проанализировать ошибку, предложить уточнения, переформулировать запрос.
- Поддерживается **модификация SQL через бизнес-термины** — замена технических алиасов на понятные названия с сохранением структуры запроса.
- В табличном режиме чат работает с данными напрямую, без привязки к SQL.

### 2.4. Визуализация и отчеты
- На основе DataFrame автоматически строятся Plotly-графики (столбчатые, гистограммы).
- Отчет формируется как DOCX-документ: заголовок, данные в виде таблицы, графики, анализ от LLM.
- **Каждый отчет использует выбранный шаблон промпта** — это определяет стиль, глубину анализа и структуру документа.
- Экспорт доступен в HTML, JPG, XLSX, TXT.

### 2.5. Ролевой доступ
- **Администратор** управляет: пользователями, подключениями, промптами, провайдерами, моделями, фолбэками, системными ключами, бизнес-кейсами отчетов.
- **Пользователь** работает с данными: запросы, чат, графики, отчеты.

---

## 3. Процесс работы пользователя (user workflow)

```mermaid
flowchart TD
    START([Пользователь открывает систему]) --> LOGIN[Вход в систему<br/>/login]
    LOGIN --> MAIN[Главная страница<br/>Модуль «Запрос»]

    MAIN --> INPUT[Ввод вопроса<br/>на русском языке]
    INPUT --> GENERATE[Генерация SQL<br/>LLM + схема БД + db_desc]
    GENERATE --> EXECUTE[Выполнение SQL<br/>PostgreSQL / Simple One]
    EXECUTE --> RESULTS[Результат<br/>таблица + время + LLM время]

    RESULTS --> EDIT_SQL{Редактировать SQL?}
    EDIT_SQL -->|Да| EDIT[Правка SQL<br/>вручную]
    EDIT --> EXECUTE

    RESULTS --> ACTIONS{Действие с результатом}
    ACTIONS -->|Чат| CHAT[Чат-бот<br/>/chat]
    ACTIONS -->|График| GRAF[График<br/>/graf]
    ACTIONS -->|Таблица| TABLE[Таблица<br/>/table]
    ACTIONS -->|Отчет| REPORT[Отчет DOCX<br/>/generate_report]
    ACTIONS -->|BI| BI[Послать в BI<br/>/send_to_redash]
    ACTIONS -->|Excel| EXCEL[Скачать Excel<br/>XLSX]

    CHAT --> CHAT_ASK[Задать вопрос LLM<br/>по SQL и данным]
    CHAT_ASK --> CHAT_ANSWER[Ответ LLM<br/>+ анализ + SQL]
    CHAT_ANSWER --> CHAT_RERUN{Перезапустить SQL?}
    CHAT_RERUN -->|Да| EXECUTE
    CHAT_RERUN -->|Нет| CHAT_ACTIONS{Действие}
    CHAT_ACTIONS -->|График| GRAF
    CHAT_ACTIONS -->|Отчет| REPORT
    CHAT_ACTIONS -->|Таблица| TABLE

    GRAF --> CHART_BUILD[Построение Plotly-графика<br/>столбчатая / гистограмма]
    CHART_BUILD --> CHART_EXPORT{Экспорт}
    CHART_EXPORT -->|HTML| CHART_HTML[HTML интерактивный]
    CHART_EXPORT -->|JPG| CHART_JPG[JPG изображение]
    CHART_EXPORT -->|Отчет| REPORT

    TABLE --> TABLE_EDIT[Редактирование таблицы<br/>добавление строк/колонок<br/>изменение ячеек]
    TABLE_EDIT --> TABLE_CHAT[Чат по таблице<br/>вопросы LLM о данных]
    TABLE_EDIT --> TABLE_GRAF[Построение графика<br/>из таблицы]
    TABLE_EDIT --> TABLE_EXCEL[Экспорт в Excel]

    REPORT --> REPORT_DOWNLOAD[Скачивание DOCX<br/>с графиками и анализом]

    BI --> BI_SEND[SQL отправлен<br/>в Redash]

    EXCEL --> XLSX_DOWNLOAD[Скачивание XLSX]

    MAIN -->|Настройки| ADMIN_PANEL{Администратор?}
    ADMIN_PANEL -->|Да| ADMIN[Панель администратора<br/>/config, /providers, /models<br/>/prompts, /connections и др.]
    ADMIN --> MAIN

    style START fill:#00bfff,color:#000
    style MAIN fill:#2a2a2a,stroke:#00bfff,color:#fff
    style RESULTS fill:#2a2a2a,stroke:#00bfff,color:#fff
    style REPORT fill:#2a2a2a,stroke:#00bfff,color:#fff
    style CHAT fill:#2a2a2a,stroke:#00bfff,color:#fff
    style GRAF fill:#2a2a2a,stroke:#00bfff,color:#fff
    style TABLE fill:#2a2a2a,stroke:#00bfff,color:#fff
```

### Описание этапов пользовательского процесса

| Этап | Действие | Модуль | Исходные данные |
|------|----------|--------|-----------------|
| 1 | Авторизация | `/login` | Логин / пароль |
| 2 | Ввод вопроса на русском | `/` (Запрос) | Текст, например: «Покажи выручку по месяцам» |
| 3 | Генерация SQL | LLM | Схема БД + `db_desc` + промпт + примеры QA |
| 4 | Выполнение SQL | PostgreSQL | Сгенерированный SELECT |
| 5 | Просмотр результата | Таблица | Данные + время выполнения + время LLM |
| 6 | Редактирование SQL | Текстовое поле | Пользователь правки → повторный запуск |
| 7 | Чат с LLM | `/chat` | SQL + данные + история диалога |
| 8 | Построение графика | `/graf` | DataFrame → Plotly |
| 9 | Работа с таблицей | `/table` | Редактирование строк/колонок/ячеек + чат |
| 10 | Генерация отчета | `/generate_report` | DOCX с графиками и анализом LLM |
| 11 | Отправка в BI | `/send_to_redash` | SQL → Redash REST API |
| 12 | Скачивание Excel | XLSX export | Данные таблицы |
| 13 | Администрирование | `/config` и др. | Управление системой (только для админа) |

---

## 4. Docker-топология