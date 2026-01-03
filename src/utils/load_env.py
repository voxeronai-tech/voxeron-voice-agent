from dotenv import load_dotenv

def load_env() -> None:
    # Loads .env if present; safe no-op if missing
    load_dotenv()
