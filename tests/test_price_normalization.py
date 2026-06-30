from telegram_kol_research.price_normalization import extract_normalized_prices


def test_extract_normalized_prices_expands_btc_wan_shorthand():
    prices = extract_normalized_prices(
        "入场：5.89-5.93附近入场 止损5.78 止盈6万/6.07/6.23",
        symbol="BTC",
    )

    assert prices == [58900, 59300, 57800, 60000, 60700, 62300]


def test_extract_normalized_prices_keeps_full_btc_prices():
    prices = extract_normalized_prices("58000-59000 止损 57,300", symbol="BTC")

    assert prices == [58000, 59000, 57300]


def test_extract_normalized_prices_uses_reference_price_when_available():
    prices = extract_normalized_prices("59.3", symbol="BTC", reference_price=59195)

    assert prices == [59300]
