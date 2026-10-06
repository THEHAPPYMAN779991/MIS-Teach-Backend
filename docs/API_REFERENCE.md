# MIS-Teach Backend API Reference

## Scope and reading notes

This is a **static inventory**, generated from the Flask Blueprint registrations in [`app.py`](../app.py), each Blueprint's `url_prefix`, and the route decorators in the registered source modules. It is not an OpenAPI document and does not prove that a route is reachable with a particular database, provider, or deployment configuration.

- `OPTIONS` is listed when the route decorator declares it explicitly. Flask may also supply framework-level `HEAD` or `OPTIONS` behavior for other routes.
- Authentication is deliberately marked **Source-dependent / verify implementation** unless a deployment has been tested with its configured session, token, and database services. A route's presence does not imply public access.
- The `src/user_guide_api.py` Blueprint is **not registered by `app.py`** and is therefore documented separately as inactive source code, not as a runtime endpoint.
- Asset routes expose paths configured by the deployment. They do not include research assets in this public repository.

## Authentication

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `POST`, `OPTIONS` | `/login/login_user` | `src/login.py` | Sign in with supplied credentials. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/login/logout` | `src/login.py` | End the current login session. | Source-dependent / verify implementation |

## Registration

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `POST`, `OPTIONS` | `/register/register_user` | `src/register.py` | Register an account. | Source-dependent / verify implementation |
| `GET` | `/register/verify/<token>` | `src/register.py` | Verify a registration token. | Source-dependent / verify implementation |

## Quiz

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `POST`, `OPTIONS` | `/quiz/submit-quiz` | `src/quiz.py` | Submit a quiz for the standard grading flow. | Source-dependent / verify implementation |
| `GET`, `OPTIONS` | `/quiz/get-quiz-result/<result_id>` | `src/quiz.py` | Retrieve a quiz result. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/quiz/create-quiz` | `src/quiz.py` | Create a quiz from requested criteria. | Source-dependent / verify implementation |
| `GET`, `OPTIONS` | `/quiz/question-sources` | `src/quiz.py` | List selectable MongoDB question sources. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/quiz/get-exam` | `src/quiz.py` | Retrieve exam-question data. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/quiz/get-exam-filters` | `src/quiz.py` | Retrieve lightweight question-filter metadata. | Source-dependent / verify implementation |
| `GET`, `OPTIONS` | `/quiz/grading-progress/<template_id>` | `src/quiz.py` | Retrieve quiz-grading progress. | Source-dependent / verify implementation |
| `GET` | `/quiz/quiz-progress/<progress_id>` | `src/quiz.py` | Poll quiz-generation progress. | Source-dependent / verify implementation |
| `GET` | `/quiz/quiz-progress-sse/<progress_id>` | `src/quiz.py` | Stream quiz-generation progress using SSE. | Source-dependent / verify implementation |
| `GET` | `/quiz/get-long-answer/<answer_id>` | `src/quiz.py` | Retrieve a stored long answer. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/quiz/get-quiz-from-database` | `src/quiz.py` | Retrieve a quiz from persistent storage. | Source-dependent / verify implementation |
| `GET`, `OPTIONS` | `/quiz/get-quiz/<quiz_id>` | `src/quiz.py` | Retrieve one quiz by identifier. | Source-dependent / verify implementation |

## AI Quiz

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `POST`, `OPTIONS` | `/ai_quiz/submit-quiz` | `src/ai_quiz.py` | Submit a quiz through the AI quiz flow. | Source-dependent / verify implementation |
| `GET`, `OPTIONS` | `/ai_quiz/get-drawing-answer/<quiz_history_id>/<question_id>` | `src/ai_quiz.py` | Retrieve a drawing-answer record. | Source-dependent / verify implementation |
| `GET`, `OPTIONS` | `/ai_quiz/get-quiz-result/<result_id>` | `src/ai_quiz.py` | Retrieve an AI-quiz result. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/ai_quiz/create-quiz` | `src/ai_quiz.py` | Create an AI-quiz record. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/ai_quiz/get-exam` | `src/ai_quiz.py` | Retrieve exam-question data for AI quiz. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/ai_quiz/create-mixed-quiz` | `src/ai_quiz.py` | Create a mixed question-type quiz. | Source-dependent / verify implementation |
| `GET`, `OPTIONS` | `/ai_quiz/grading-progress/<template_id>` | `src/ai_quiz.py` | Retrieve AI-quiz grading progress. | Source-dependent / verify implementation |
| `GET` | `/ai_quiz/quiz-progress/<progress_id>` | `src/ai_quiz.py` | Poll AI-quiz progress. | Source-dependent / verify implementation |
| `GET` | `/ai_quiz/quiz-progress-sse/<progress_id>` | `src/ai_quiz.py` | Stream AI-quiz progress using SSE. | Source-dependent / verify implementation |
| `GET` | `/ai_quiz/get-long-answer/<answer_id>` | `src/ai_quiz.py` | Retrieve a stored long answer. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/ai_quiz/get-quiz-from-database` | `src/ai_quiz.py` | Retrieve an AI quiz from storage. | Source-dependent / verify implementation |
| `GET`, `OPTIONS` | `/ai_quiz/get-latest-quiz` | `src/ai_quiz.py` | Retrieve the most recent quiz. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/ai_quiz/get-user-submissions-analysis` | `src/ai_quiz.py` | Retrieve submission-analysis data. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/ai_quiz/generate-guided-learning-session` | `src/ai_quiz.py` | Generate a guided-learning session. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/ai_quiz/get-user-errors-mongo` | `src/ai_quiz.py` | Retrieve a user's error-question records from MongoDB. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/ai_quiz/generate-content-based-quiz` | `src/ai_quiz.py` | Generate a content-based quiz. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/ai_quiz/submit-ai-quiz` | `src/ai_quiz.py` | Submit answers to an AI-generated quiz. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/ai_quiz/track-learning-progress` | `src/ai_quiz.py` | Record learning-progress data. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/ai_quiz/get-learning-recommendations` | `src/ai_quiz.py` | Return learning recommendations (currently a placeholder-safe response in source). | Source-dependent / verify implementation |

## AI Teacher / Tutoring

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `POST`, `OPTIONS` | `/ai_teacher/ai-tutoring` | `src/ai_teacher.py` | Generate an AI tutoring reply. | Source-dependent / verify implementation |
| `GET`, `OPTIONS` | `/ai_teacher/get-quiz-result/<result_id>` | `src/ai_teacher.py` | Retrieve a quiz result for tutoring. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/ai_teacher/get-quiz-from-database` | `src/ai_teacher.py` | Retrieve a stored quiz for tutoring. | Source-dependent / verify implementation |

## Learning Analytics

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `POST`, `OPTIONS` | `/api/learning-analytics/ai-diagnosis` | `src/learning_analytics.py` | Diagnose specified knowledge points with AI support. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/api/learning-analytics/init-data` | `src/learning_analytics.py` | Initialise learning-analysis data. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/api/learning-analytics/ai-practice-parallel` | `src/learning_analytics.py` | Generate AI practice through the parallel practice path. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/api/learning-analytics/difficulty-analysis` | `src/learning_analytics.py` | Run difficulty analysis. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/api/learning-analytics/forgetting-analysis` | `src/learning_analytics.py` | Run forgetting analysis. | Source-dependent / verify implementation |

## RAG

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `GET` | `/api/rag/backend` | `src/rag_backend_api.py` | Read active RAG backend status. | Source-dependent / verify implementation |
| `POST` | `/api/rag/backend` | `src/rag_backend_api.py` | Set a runtime RAG backend override. | Source-dependent / verify implementation |
| `POST` | `/api/rag/backend/reset` | `src/rag_backend_api.py` | Clear the runtime RAG backend override. | Source-dependent / verify implementation |
| `GET` | `/api/rag/traces` | `src/rag_backend_api.py` | List saved RAG traces. | Source-dependent / verify implementation |
| `GET` | `/api/rag/traces/<backend>/<trace_id>` | `src/rag_backend_api.py` | Retrieve one RAG trace. | Source-dependent / verify implementation |
| `GET` | `/api/rag/comparisons` | `src/rag_backend_api.py` | List RAG comparison records. | Source-dependent / verify implementation |
| `GET` | `/api/rag/comparisons/<comparison_id>` | `src/rag_backend_api.py` | Retrieve one RAG comparison record. | Source-dependent / verify implementation |
| `GET` | `/api/rag/trace-files/<backend>` | `src/rag_backend_api.py` | List trace files for a backend. | Source-dependent / verify implementation |
| `GET` | `/api/rag/trace-files/<backend>/<path:filename>` | `src/rag_backend_api.py` | Download or display one trace file. | Source-dependent / verify implementation |
| `POST` | `/api/rag/compare` | `src/rag_backend_api.py` | Run a controlled GraphRAG / ChromaDB / LLM-only comparison. | Source-dependent / verify implementation |
| `GET` | `/api/rag/health` | `src/rag_backend_api.py` | Read RAG and GraphRAG health information. | Source-dependent / verify implementation |

## GraphRAG

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `GET` | `/api/graphrag/schema` | `src/graphrag_proxy.py` | Read the active graph schema and readiness. | Source-dependent / verify implementation |
| `POST` | `/api/graphrag/question-concepts/map` | `src/graphrag_proxy.py` | Inspect canonical-concept mapping for a question. | Source-dependent / verify implementation |
| `GET` | `/api/graphrag/concepts/search` | `src/graphrag_proxy.py` | Search concept names for autocomplete. | Source-dependent / verify implementation |
| `GET` | `/api/graphrag/concepts/categories` | `src/graphrag_proxy.py` | List concept categories and counts. | Source-dependent / verify implementation |
| `GET` | `/api/graphrag/concepts/by_category` | `src/graphrag_proxy.py` | List concepts in a category. | Source-dependent / verify implementation |
| `GET` | `/api/graphrag/concept/<name>` | `src/graphrag_proxy.py` | Read concept detail and graph relationships. | Source-dependent / verify implementation |
| `GET` | `/api/graphrag/learning_path/<target>` | `src/graphrag_proxy.py` | Build a learning path to a target concept. | Source-dependent / verify implementation |
| `GET` | `/api/graphrag/concept/<path:name>/paths` | `src/graphrag_proxy.py` | Read horizontal learning paths for a concept. | Source-dependent / verify implementation |
| `GET` | `/api/graphrag/concept/<name>/graph` | `src/graphrag_proxy.py` | Read a concept-centred graph for visualisation. | Source-dependent / verify implementation |
| `GET` | `/api/graphrag/concept/<name>/locations` | `src/graphrag_proxy.py` | Proxy source-material locations from the Knowledge Graph API. | Source-dependent / verify implementation |
| `GET` | `/api/graphrag/concept/<name>/material_check` | `src/graphrag_proxy.py` | Check whether a local material rendering exists. | Source-dependent / verify implementation |
| `GET` | `/api/graphrag/pdf/<book_id>` | `src/graphrag_proxy.py` | Stream a book PDF from the Knowledge Graph API. | Source-dependent / verify implementation |
| `POST` | `/api/graphrag/ask` | `src/graphrag_proxy.py` | Proxy a GraphRAG question-answer request. | Source-dependent / verify implementation |
| `GET` | `/api/graphrag/stats` | `src/graphrag_proxy.py` | Read overall graph statistics. | Source-dependent / verify implementation |

## Materials

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `GET` | `/materials/<filename>` | `src/materials_api.py` | Return rendered material Markdown. | Source-dependent / verify implementation |
| `GET` | `/materials/key_points` | `src/materials_api.py` | Return deduplicated question key points. | Source-dependent / verify implementation |
| `GET` | `/materials/domain` | `src/materials_api.py` | Return material domains. | Source-dependent / verify implementation |
| `GET` | `/materials/block` | `src/materials_api.py` | Return material blocks. | Source-dependent / verify implementation |
| `GET` | `/materials/micro_concept` | `src/materials_api.py` | Return micro concepts. | Source-dependent / verify implementation |

## Notes

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `GET` | `/note/highlights` | `src/note.py` | List material highlights. | Source-dependent / verify implementation |
| `POST` | `/note/highlights` | `src/note.py` | Save a material highlight. | Source-dependent / verify implementation |
| `DELETE` | `/note/highlights/<highlight_id>` | `src/note.py` | Delete a material highlight. | Source-dependent / verify implementation |
| `GET` | `/note/notes` | `src/note.py` | List notes. | Source-dependent / verify implementation |
| `POST` | `/note/notes` | `src/note.py` | Create a note. | Source-dependent / verify implementation |
| `PUT` | `/note/notes/<note_id>` | `src/note.py` | Update a note. | Source-dependent / verify implementation |
| `DELETE` | `/note/notes/<note_id>` | `src/note.py` | Delete a note. | Source-dependent / verify implementation |
| `POST` | `/note/highlights/clear` | `src/note.py` | Clear highlights for a material scope. | Source-dependent / verify implementation |

## Dashboard

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `POST`, `OPTIONS` | `/dashboard/get-user-name` | `src/dashboard.py` | Retrieve a user's display name. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/dashboard/get-user-info` | `src/dashboard.py` | Retrieve user profile information. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/dashboard/update-user-info` | `src/dashboard.py` | Update user profile information. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/dashboard/events` | `src/dashboard.py` | List calendar events. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/dashboard/events/create` | `src/dashboard.py` | Create a calendar event. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/dashboard/events/update` | `src/dashboard.py` | Update a calendar event. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/dashboard/events/delete` | `src/dashboard.py` | Delete a calendar event. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/dashboard/dashboard-stats` | `src/dashboard.py` | Retrieve dashboard summary statistics. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/dashboard/recent-activities` | `src/dashboard.py` | Retrieve recent activity records. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/dashboard/daily-checkin` | `src/dashboard.py` | Record a daily check-in. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/dashboard/checkin-status` | `src/dashboard.py` | Retrieve daily check-in status. | Source-dependent / verify implementation |

## Web AI

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `POST`, `OPTIONS` | `/web-ai/chat` | `src/web_ai_assistant.py` | Generate a Web AI assistant reply. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/web-ai/quick-action` | `src/web_ai_assistant.py` | Run a predefined Web AI quick action. | Source-dependent / verify implementation |
| `GET` | `/web-ai/graphrag-traces` | `src/web_ai_assistant.py` | List recent GraphRAG trace summaries. | Source-dependent / verify implementation |
| `GET` | `/web-ai/graphrag-traces/latest` | `src/web_ai_assistant.py` | Retrieve the newest GraphRAG trace. | Source-dependent / verify implementation |
| `GET` | `/web-ai/graphrag-traces/<trace_id>` | `src/web_ai_assistant.py` | Retrieve one GraphRAG trace. | Source-dependent / verify implementation |
| `GET` | `/web-ai/graphrag-traces/<trace_id>/markdown` | `src/web_ai_assistant.py` | Retrieve a Markdown GraphRAG trace. | Source-dependent / verify implementation |
| `GET` | `/web-ai/rag-comparison` | `src/web_ai_assistant.py` | List RAG-comparison reports. | Source-dependent / verify implementation |
| `GET` | `/web-ai/rag-comparison/<run_id>/json` | `src/web_ai_assistant.py` | Retrieve one comparison report as JSON. | Source-dependent / verify implementation |
| `GET` | `/web-ai/rag-comparison/<run_id>/markdown` | `src/web_ai_assistant.py` | Retrieve one comparison report as Markdown. | Source-dependent / verify implementation |
| `GET`, `OPTIONS` | `/web-ai/status` | `src/web_ai_assistant.py` | Retrieve Web AI assistant status. | Source-dependent / verify implementation |
| `GET`, `OPTIONS` | `/web-ai/health` | `src/web_ai_assistant.py` | Retrieve Web AI assistant health. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/web-ai/get-quiz-from-database` | `src/web_ai_assistant.py` | Retrieve a quiz for the Web AI flow. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/web-ai/execute-action` | `src/web_ai_assistant.py` | Run the route associated with a quick action. | Source-dependent / verify implementation |

## News

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `GET`, `OPTIONS` | `/api/news` | `src/news_api.py` | List news items. | Source-dependent / verify implementation |
| `GET`, `OPTIONS` | `/api/news/<int:news_id>` | `src/news_api.py` | Retrieve one news item. | Source-dependent / verify implementation |
| `GET`, `OPTIONS` | `/api/news/stats` | `src/news_api.py` | Retrieve news statistics. | Source-dependent / verify implementation |

## User Guide

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `GET`, `OPTIONS` | `/guide/actions-config` | `src/website_guide.py` | Retrieve configured AI actions for the guide. | Source-dependent / verify implementation |
| `GET`, `OPTIONS` | `/guide/api/user-guide/status` | `src/website_guide.py` | Retrieve guide-completion status. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/guide/api/user-guide/mark-guided` | `src/website_guide.py` | Mark a user as having completed the guide. | Source-dependent / verify implementation |

### Unregistered source Blueprint

`src/user_guide_api.py` declares `user_guide_bp`, but `app.py` does not call `app.register_blueprint(user_guide_bp)`. The following decorators are therefore **not active runtime endpoints under the current `app.py`**:

| Method | Decorated path | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `GET`, `OPTIONS` | `/api/user-guide/status` | `src/user_guide_api.py` | Alternate guide-status API. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/api/user-guide/mark-guided` | `src/user_guide_api.py` | Alternate completion-marking API. | Source-dependent / verify implementation |
| `POST` | `/api/user-guide/reset` | `src/user_guide_api.py` | Reset guide status. | Source-dependent / verify implementation |
| `GET` | `/api/user-guide/stats` | `src/user_guide_api.py` | Read guide statistics. | Source-dependent / verify implementation |
| `GET` | `/api/user-guide/test` | `src/user_guide_api.py` | Test the guide API implementation. | Source-dependent / verify implementation |

## LINE integration

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `POST`, `OPTIONS` | `/linebot/generate-qr` | `src/linebot.py` | Generate a LINE binding QR payload. | Source-dependent / verify implementation |
| `POST`, `OPTIONS` | `/linebot/check-binding` | `src/linebot.py` | Check LINE binding status. | Source-dependent / verify implementation |
| `POST` | `/linebot/webhook` | `src/linebot.py` | Receive LINE webhook callbacks. | Source-dependent / verify implementation |

## Application asset routes

| Method | Endpoint | Module | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| `GET` | `/api/assets/<path:filename>` | `app.py` | Serve configured PDF-derived asset files. | Source-dependent / verify implementation |
| `GET` | `/output_json/<path:filename>` | `app.py` | Serve configured conversion-output assets. | Source-dependent / verify implementation |
| `GET` | `/static/images/<path:filename>` | `app.py` | Serve configured static question images. | Source-dependent / verify implementation |
| `GET` | `/static/<path:filename>` | `app.py` | Serve configured course-image files. | Source-dependent / verify implementation |

## Route source of truth

For release work, re-run a static scan whenever Blueprint registrations or decorators change:

```powershell
rg -n "register_blueprint|@.*route\\(" app.py src
```

This command is an inventory aid only. Validate authentication, request schemas, database requirements, external service requirements, and response schemas against the relevant source module before exposing an endpoint in a production deployment.
