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
    writer.mark(
        "signal",
        text="BUY",
        price=12,
        color="#16a34a",
        position="belowBar",
        shape="arrowUp",
    )

    frame = writer.drain()

    assert frame.values == {"alpha": 12.0}
    assert frame.markers == {
        "signal": [{
            "text": "BUY",
            "price": 12.0,
            "color": "#16a34a",
            "position": "belowBar",
            "shape": "arrowUp",
        }],
    }
    assert writer.drain().values == {}


def test_indicator_definition_rejects_invalid_type():
    with pytest.raises(ValueError, match="type must be one of"):
        parse_indicator_definitions({"alpha": {"type": "table", "pane": "strategy"}})


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ([], "config.config"),
        ({"position": "top"}, "config.position"),
        ({"shape": "triangle"}, "config.shape"),
        ({"threshold": float("nan")}, "JSON-compatible"),
        ({"threshold": object()}, "JSON-compatible"),
    ],
)
def test_indicator_definition_rejects_invalid_transport_config(config, message):
    with pytest.raises(ValueError, match=message):
        parse_indicator_definitions({
            "signal": {
                "type": "marker",
                "pane": "price",
                "config": config,
            },
        })


def test_indicator_writer_rejects_invalid_marker_position_and_shape():
    definitions = parse_indicator_definitions({
        "signal": {"type": "marker", "pane": "price"},
    })
    writer = IndicatorWriter(definitions)

    writer.mark("signal", position="left", shape="triangle")

    frame = writer.drain()
    assert frame.markers == {}
    assert frame.warnings == [
        "marker position must be aboveBar, belowBar, or inBar: signal"
    ]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_indicator_writer_treats_non_finite_scalar_as_missing(value):
    definitions = parse_indicator_definitions({
        "alpha": {"type": "line", "pane": "strategy"},
    })
    writer = IndicatorWriter(definitions)

    writer.set("alpha", value)

    frame = writer.drain()
    assert frame.values == {}
    assert frame.warnings == ["indicator value must be finite: alpha"]


@pytest.mark.parametrize("price", [float("nan"), float("inf"), float("-inf")])
def test_indicator_writer_rejects_non_finite_marker_price(price):
    definitions = parse_indicator_definitions({
        "signal": {"type": "marker", "pane": "price"},
    })
    writer = IndicatorWriter(definitions)

    writer.mark("signal", text="BUY", price=price)

    frame = writer.drain()
    assert frame.markers == {}
    assert frame.warnings == ["marker price must be finite: signal"]
