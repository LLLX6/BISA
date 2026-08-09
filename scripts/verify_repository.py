"""Compatibility entry point for the canonical BISA repository verifier."""

try:
    from scripts.verify_bisa import main
except ModuleNotFoundError:
    # Support: python scripts/verify_repository.py
    from verify_bisa import main  # type: ignore


if __name__ == "__main__":
    raise SystemExit(main())
