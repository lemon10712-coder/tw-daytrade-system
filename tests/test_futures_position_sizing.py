from stockSystem.futures_position_sizing import build_futures_position


def test_never_exceeds_capital_cap_via_margin_limit():
    plan = build_futures_position(
        direction="long",
        entry_reference=18000.0,
        stop_price=17950.0,  # 止損距離50點
        target_price=18075.0,
        capital_cap_twd=100_000.0,
        margin_per_contract_twd=17_850.0,
        point_value_twd=10.0,
        risk_pct=0.5,  # 故意給很寬鬆的風險%，讓保證金額度變成真正限制口數的那一個
    )
    assert plan is not None
    assert plan.margin_used_twd <= plan.capital_cap_twd
    assert plan.contracts == 5  # 100000 // 17850 = 5


def test_never_exceeds_risk_budget_via_stop_distance():
    plan = build_futures_position(
        direction="long",
        entry_reference=18000.0,
        stop_price=17000.0,  # 止損距離1000點，風險很大
        target_price=19500.0,
        capital_cap_twd=100_000.0,
        margin_per_contract_twd=17_850.0,
        point_value_twd=10.0,
        risk_pct=0.01,  # 風險預算=1000元，1000點*10元=10000元/口 > 1000元預算 -> 0口
    )
    assert plan is None


def test_allocates_fewer_contracts_to_wider_stop_distance():
    # 用較大的資金額度讓「風險預算」而不是「保證金額度」成為兩邊真正的限制因素，
    # 才能單純比較出止損距離對口數的影響（否則兩邊都會被保證金額度同樣封頂，看不出差異）。
    tight = build_futures_position(
        direction="long", entry_reference=18000.0, stop_price=17990.0, target_price=18015.0,
        capital_cap_twd=1_000_000.0, margin_per_contract_twd=17_850.0, point_value_twd=10.0, risk_pct=0.01,
    )
    wide = build_futures_position(
        direction="long", entry_reference=18000.0, stop_price=17950.0, target_price=18075.0,
        capital_cap_twd=1_000_000.0, margin_per_contract_twd=17_850.0, point_value_twd=10.0, risk_pct=0.01,
    )
    assert tight is not None and wide is not None
    assert tight.contracts > wide.contracts


def test_returns_none_when_stop_distance_is_zero():
    assert build_futures_position(
        direction="long", entry_reference=18000.0, stop_price=18000.0, target_price=18100.0,
        capital_cap_twd=100_000.0,
    ) is None


def test_returns_none_when_margin_too_high_for_a_single_contract():
    assert build_futures_position(
        direction="long", entry_reference=18000.0, stop_price=17990.0, target_price=18015.0,
        capital_cap_twd=10_000.0, margin_per_contract_twd=17_850.0,
    ) is None


def test_max_loss_matches_manual_calculation():
    plan = build_futures_position(
        direction="short", entry_reference=18000.0, stop_price=18050.0, target_price=17925.0,
        capital_cap_twd=100_000.0, margin_per_contract_twd=17_850.0, point_value_twd=10.0, risk_pct=0.5,
    )
    assert plan is not None
    expected_max_loss = 50.0 * 10.0 * plan.contracts
    assert plan.max_loss_twd == expected_max_loss
