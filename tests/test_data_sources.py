import datetime as dt

import pytest

from stockSystem.data_sources import (
    DataSourceUnavailableError,
    FixtureProvider,
    SourceTag,
    TwseOpenApiProvider,
)


def test_fixture_provider_tags_data_as_synthetic():
    snapshot = FixtureProvider().get_snapshot(dt.date.today())
    assert snapshot.source_tag == SourceTag.SYNTHETIC_TEST_DATA
    assert snapshot.is_synthetic() is True


def test_fixture_provider_produces_multiple_sectors_with_expected_trend_ranking():
    snapshot = FixtureProvider(seed=1).get_snapshot(dt.date.today())
    sectors = set(snapshot.industry_map.values())
    assert {"光通訊", "半導體", "傳產", "航運"} <= sectors


def test_fixture_provider_excludes_are_populated():
    snapshot = FixtureProvider().get_snapshot(dt.date.today())
    assert len(snapshot.disposition_list) >= 1
    assert len(snapshot.full_delivery_list) >= 1
    assert len(snapshot.watch_list) >= 1


def test_fixture_provider_deterministic_with_same_seed():
    s1 = FixtureProvider(seed=7).get_snapshot(dt.date.today())
    s2 = FixtureProvider(seed=7).get_snapshot(dt.date.today())
    assert s1.ohlcv["close"].tolist() == s2.ohlcv["close"].tolist()


def test_real_provider_raises_clear_error_instead_of_silent_failure():
    """真實資料源目前應該明確失敗並指向 KNOWN_ISSUES.md，而不是回傳空殼資料
    讓下游誤以為抓到了真實資料（這是最容易產生 AI 幻覺結果的失敗模式）。
    """
    provider = TwseOpenApiProvider()
    with pytest.raises(DataSourceUnavailableError) as exc_info:
        provider.get_snapshot(dt.date.today())
    assert "KNOWN_ISSUES.md" in str(exc_info.value)
