from boxgrid.notify import humanize as H


def test_won_formats():
    assert H.won(104_523_000) == "1억 452만원"
    assert H.won(200_000_000) == "2억원"
    assert H.won(99_346_214) == "9,935만원"
    assert H.won(12_340) == "12,340원"
    assert H.won(1_234) == "1,234원"
    assert H.won(199_999_999) == "2억원"
    assert H.won(None) == "-"
    assert H.won(65432.1, "USDT") == "65,432.10 USDT"


def test_pct_and_lines():
    assert H.pct(101.7, 100) == "+1.7%"
    assert H.pct(96.4, 100) == "-3.6%"
    lv = {"prices": [104e6, 103e6], "weights_pct": [50, 50], "sl": 99e6, "tp": 113e6}
    lines = H.level_lines(lv, 105e6, [1], [2], "KRW", 10e6)
    assert "1단" in lines[0] and "체결" in lines[0] and "500만원" in lines[0]
    assert "2단" in lines[1] and "대기" in lines[1]
    ex = H.exit_lines(lv, 105e6, "KRW", 50)
    assert "손절" in ex[0] and "9,900만원" in ex[0] and "익절" in ex[1]
