from telegram_kol_research.recognition_profiles import list_recognition_profiles


def test_recognition_profiles_include_bitcoin_junzhang_metadata():
    profiles = list_recognition_profiles()

    junzhang = next(profile for profile in profiles if profile.id == "junzhang_profile")

    assert junzhang.chat_id == -1002282384698
    assert junzhang.title == "比特币军长-11分组"
    assert junzhang.parse_source == "junzhang_profile"
    assert "现价开仓" in junzhang.capabilities
    assert "止损上移到开仓价" in junzhang.capabilities
    assert "缺少止损/止盈" in junzhang.risk_policy
