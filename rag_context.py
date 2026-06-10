import contextvars

current_project_id = contextvars.ContextVar('rag_project_id', default=None)
current_chat_id = contextvars.ContextVar('rag_chat_id', default=None)
