def test_imports_smoke():
    # If this fails, your pythonpath/layout is broken
    import src.api.tenant_manager  # noqa: F401
    import src.api.policy  # noqa: F401
