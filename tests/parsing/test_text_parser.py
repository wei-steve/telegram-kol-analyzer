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
