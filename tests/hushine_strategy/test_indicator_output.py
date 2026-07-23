import pytest

from hushine_strategy.indicator_output import IndicatorWriter, parse_indicator_definitions


def test_parse_indicator_definitions_matches_hosted_contract():
    definitions = parse_indicator_definitions({
        "alpha": {"type": "line", "pane": "strategy"},
        "signal": {"type": "marker", "pane": "price", "name": "Signal"},
    })

    assert [(item.key, item.name, item.type, item.pane) for item in definitions] == [
        ("alpha", "alpha", "line", "strategy"),
        ("signal", "Signal", "marker", "price"),
    ]


def test_indicator_writer_resets_and_drains_one_bar():
    definitions = parse_indicator_definitions({
        "alpha": {"type": "line", "pane": "strategy"},
        "signal": {"type": "marker", "pane": "price"},
    })
    writer = IndicatorWriter(definitions)
    writer.set("alpha", 12)
    writer.mark("signal", text="BUY", price=12, color="#16a34a")

    frame = writer.drain()

    assert frame.values == {"alpha": 12.0}
    assert frame.markers == {
        "signal": [{"text": "BUY", "price": 12.0, "color": "#16a34a"}],
    }
    assert writer.drain().values == {}


def test_indicator_definition_rejects_invalid_type():
    with pytest.raises(ValueError, match="type must be one of"):
        parse_indicator_definitions({"alpha": {"type": "table", "pane": "strategy"}})
