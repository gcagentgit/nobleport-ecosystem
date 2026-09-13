import argparse
import json
import logging
import os
import signal
from pathlib import Path
from .config import load_config
from .storage import Store


def main():
    parser = argparse.ArgumentParser(description="Steph email operations")
    parser.add_argument("command", choices=["serve", "once", "demo", "check-config", "new-secrets", "speak-brief"])
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=5050, type=int)
    parser.add_argument("--env-file", help="Load a trusted local environment file; values are not printed")
    args = parser.parse_args()
    os.umask(0o077)
    if args.env_file:
        # Literal KEY=value only: do not execute a shell or interpolate values.
        for line in Path(args.env_file).read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                key, sep, value = line.partition("=")
                if not sep or not key.strip().isidentifier():
                    parser.error("Invalid environment file")
                os.environ.setdefault(key.strip(), value.strip())
    if args.command == "new-secrets":
        import secrets
        from cryptography.fernet import Fernet
        print("STEPH_DASHBOARD_TOKEN=" + secrets.token_urlsafe(36))
        print("STEPH_SESSION_SECRET=" + secrets.token_urlsafe(36))
        print("STEPH_TOKEN_ENCRYPTION_KEY=" + Fernet.generate_key().decode())
        return
    config = load_config(args.config)
    if args.command == "check-config":
        print(json.dumps({"configuration": "valid", "dry_run": config.settings["dry_run"], "accounts_enabled": sum(bool(a.get("enabled")) for a in config.accounts), "timezone": config.settings["timezone"], "live_connections_tested": False}, indent=2))
        return
    if args.command == "demo":
        config.settings.update(demo_mode=True, dry_run=True, aggregation_enabled=False)
        config.accounts = []
        config.data_dir = config.data_dir.parent / "demo-data"
    from .engine import Engine
    store = Store(config.data_dir)
    if args.command == "speak-brief":
        from .local_voice import speak_local
        stats = store.stats()
        print(json.dumps(speak_local(f"Steph for NoblePort. {stats['total_processed']} emails processed today. {stats['urgent_flagged']} flagged urgent. {stats['expected_pending']} replies pending. Please review your dashboard.")))
        return
    engine = Engine(config, store)
    if args.command == "once":
        print(json.dumps(engine.run_cycle(), default=str))
        engine.tick()
        return
    from .web import create_app
    from waitress import serve
    app = create_app(engine, store, config)
    if args.command == "demo":
        engine.demo()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = engine.start_background()

    def shutdown(signum, frame):
        stop.set()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"Steph dashboard: http://{args.host}:{args.port} (authentication required)")
    try:
        serve(app, host=args.host, port=args.port, threads=8, clear_untrusted_proxy_headers=True)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
