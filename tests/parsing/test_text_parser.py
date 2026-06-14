from telegram_kol_research.parsing.text_parser import parse_signal_text


def test_parse_signal_text_extracts_basic_long_setup():
    parsed = parse_signal_text("BTC long 68000-68200, SL 67500, TP 69000 / 70000")
    assert parsed.symbol == "BTC"
    assert parsed.side == "long"
    assert parsed.stop_loss == 67500
    assert parsed.take_profits == [69000, 70000]


def test_parse_signal_text_recognizes_bullish_hashtag_signal():
    parsed = parse_signal_text("#TAO Bullish\nAdd more")
    assert parsed.symbol == "TAO"
    assert parsed.side == "long"
    assert parsed.confidence >= 0.4


def test_parse_signal_text_recognizes_adding_signal_with_symbol():
    parsed = parse_signal_text("starts adding #ZEC from here to 280 level")
    assert parsed.symbol == "ZEC"
    assert parsed.side == "long"
    assert parsed.confidence >= 0.4


def test_parse_signal_text_recognizes_takeoff_signal_as_long_bias():
    parsed = parse_signal_text("$XMR looks ready to take off.")
    assert parsed.symbol == "XMR"
    assert parsed.side == "long"
    assert parsed.confidence >= 0.4


def test_parse_signal_text_extracts_chinese_btc_long_setup():
    parsed = parse_signal_text("比特币现货，62800-60000做多，均价61400，64200-65400-66600止盈，止损59500")
    assert parsed.symbol == "BTC"
    assert parsed.side == "long"
    assert parsed.entry_range == (62800, 60000)
    assert parsed.stop_loss == 59500
    assert parsed.take_profits == [64200, 65400, 66600]
    assert parsed.event_type == "entry_signal"


def test_parse_signal_text_extracts_chinese_btc_short_setup_with_labels():
    parsed = parse_signal_text("Btc 方向：空 建仓：63600-64700 止损：65100 止盈：62900-62200-61500")
    assert parsed.symbol == "BTC"
    assert parsed.side == "short"
    assert parsed.entry_range == (63600, 64700)
    assert parsed.stop_loss == 65100
    assert parsed.take_profits == [62900, 62200, 61500]


def test_parse_signal_text_recognizes_chinese_market_short_entry():
    parsed = parse_signal_text("BTC 现价开一层空单")
    assert parsed.symbol == "BTC"
    assert parsed.side == "short"
    assert parsed.event_type == "entry_signal"
    assert parsed.confidence >= 0.4


def test_parse_signal_text_recognizes_chinese_full_take_profit_exit():
    parsed = parse_signal_text("BTC 剩余仓位全部止盈出局")
    assert parsed.symbol == "BTC"
    assert parsed.event_type == "take_profit_update"
