import json
from dataclasses import asdict

from . import GRPCAccessLog, GRPCExtLog, KafkaSentLog, KafkaRecvLog


def test_grpc_access_log_instantiation_and_json_serialization():
    entry = GRPCAccessLog(
        method="GetOrder",
        client_ip="127.0.0.1",
        latency_ms=12,
        status_code=0,
        error="",
        request_params={"order_id": "123"},
        response={"order_id": "123", "status": "FILLED"},
    )

    payload = json.loads(json.dumps(asdict(entry)))
    assert payload == {
        "method": "GetOrder",
        "client_ip": "127.0.0.1",
        "latency_ms": 12,
        "status_code": 0,
        "error": "",
        "request_params": {"order_id": "123"},
        "response": {"order_id": "123", "status": "FILLED"},
    }


def test_grpc_ext_log_instantiation_and_json_serialization():
    entry = GRPCExtLog(
        method="PlaceOrder",
        target_service="risk-service",
        latency_ms=25,
        status_code=13,
        error="internal",
        request_params={"symbol": "BTCUSDT"},
        response={"accepted": False},
    )

    payload = json.loads(json.dumps(asdict(entry)))
    assert payload == {
        "method": "PlaceOrder",
        "target_service": "risk-service",
        "latency_ms": 25,
        "status_code": 13,
        "error": "internal",
        "request_params": {"symbol": "BTCUSDT"},
        "response": {"accepted": False},
    }


def test_kafka_sent_log_instantiation_and_json_serialization():
    entry = KafkaSentLog(
        topic="orders",
        partition=2,
        offset=1024,
        message_size=512,
        data={"key": "order-1"},
    )

    payload = json.loads(json.dumps(asdict(entry)))
    assert payload == {
        "topic": "orders",
        "partition": 2,
        "offset": 1024,
        "message_size": 512,
        "data": {"key": "order-1"},
    }


def test_kafka_recv_log_instantiation_and_json_serialization():
    entry = KafkaRecvLog(
        topic="fills",
        partition=1,
        offset=88,
        lag_ms=7,
        message_size=128,
        consumer_group="portfolio-consumer",
        data={"fill_id": "f-001"},
    )

    payload = json.loads(json.dumps(asdict(entry)))
    assert payload == {
        "topic": "fills",
        "partition": 1,
        "offset": 88,
        "lag_ms": 7,
        "message_size": 128,
        "consumer_group": "portfolio-consumer",
        "data": {"fill_id": "f-001"},
    }
