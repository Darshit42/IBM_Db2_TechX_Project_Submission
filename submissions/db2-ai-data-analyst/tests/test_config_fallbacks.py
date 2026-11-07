from analyst.config import Settings


def make(**kw) -> Settings:
    base = dict(google_api_key="", google_api_keys_extra="", gemini_model="gemini-2.5-flash",
                gemini_fallback_models="gemini-2.5-flash-lite")
    base.update(kw)
    return Settings(**base)


def test_no_key_means_offline():
    assert not make().llm_enabled


def test_placeholder_key_is_ignored():
    assert not make(google_api_key="your_gemini_api_key_here").llm_enabled


def test_keys_are_deduplicated_and_ordered():
    s = make(google_api_key="k1", google_api_keys_extra=" k2, k1 ,,k3")
    assert s.api_keys == ["k1", "k2", "k3"]


def test_models_primary_first_without_duplicates():
    s = make(gemini_fallback_models="gemini-2.5-flash, gemini-2.5-flash-lite")
    assert s.models == ["gemini-2.5-flash", "gemini-2.5-flash-lite"]


def test_candidate_order_is_model_major(monkeypatch):
    from analyst import agent

    s = make(google_api_key="k1", google_api_keys_extra="k2")
    a = agent.Analyst.__new__(agent.Analyst)
    a.cfg = s
    a._llm = None
    cands = a._candidates()
    assert [(c.model, c.google_api_key.get_secret_value()) for c in cands] == [
        ("gemini-2.5-flash", "k1"), ("gemini-2.5-flash", "k2"),
        ("gemini-2.5-flash-lite", "k1"), ("gemini-2.5-flash-lite", "k2"),
    ]
