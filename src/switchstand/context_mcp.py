from .mcp import build_context_server, controller_from_env, protect_provider_logs
from .provision import require_current_schema


def main() -> None:
    protect_provider_logs()
    require_current_schema()
    service = controller_from_env()
    build_context_server(service, service.authority.active_work_id).run()


if __name__ == "__main__":
    main()
