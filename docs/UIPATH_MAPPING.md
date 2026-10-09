# UiPath mapping

This project demonstrates a local SANDBOX workflow and is not connected to a
production ServiceNow instance.

| AutoHeal prototype | UiPath concept |
|---|---|
| A row in SQLite `rpa_tasks` with status `queued` | Orchestrator queue item |
| `rpa/worker.py` claiming and processing queued rows | Robot process consuming a queue |
| `rpa/bot.py` Playwright browser steps | Web automation workflow |
| `PORTAL_USER` and `PORTAL_PASS` in local `.env` | Orchestrator assets / credential assets |
| `screenshots/` paths saved to the task row | Execution evidence and audit attachments |
| Task status events (`running`, `done`, `failed`) | Queue item processing status |

The bot uses the portal's HTML forms because the mock legacy portal deliberately
has no JSON API. Service remediation is handled through sandbox APIs and
scripts; RPA is used only to create, update, resolve, or escalate records in
the legacy ITSM portal.