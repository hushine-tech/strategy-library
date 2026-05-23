"""
Unit tests for gRPC client middleware.
"""
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest


@dataclass
class MockRequest:
    symbol: str = "BTCUSDT"
    quantity: float = 1.5
    order_id: str = ""


@dataclass
class MockResponse:
    accepted: bool = True
    order_id: str = "order-123"


class MockGRPCError(Exception):
    def __init__(self, code, details):
        self._code = code
        self._details = details
        super().__init__(details)
    
    @property
    def code(self):
        return type("Code", (), {"value": (self._code,)})()


class MockStub:
    def PlaceOrder(self, request, **kwargs):
        return MockResponse(accepted=True, order_id="order-123")
    
    def GetOrder(self, request, **kwargs):
        return MockResponse(accepted=True, order_id=request.order_id if hasattr(request, 'order_id') else "unknown")


class TestGRPCClientMiddleware:
    def test_middleware_logs_successful_call(self):
        from utils.middleware.grpc import GRPCClientMiddleware
        
        mock_logger = MagicMock()
        stub = MockStub()
        middleware = GRPCClientMiddleware(stub, "order-service", logger=mock_logger)
        
        request = MockRequest(symbol="BTCUSDT", quantity=1.5)
        response = middleware.PlaceOrder(request)
        
        assert response.accepted is True
        mock_logger.grpc_ext_log.assert_called_once()
        
        call_args = mock_logger.grpc_ext_log.call_args
        entry = call_args[0][1]
        
        assert entry.method == "PlaceOrder"
        assert entry.target_service == "order-service"
        assert entry.latency_ms >= 0
        assert entry.status_code == 0
        assert entry.error == ""
        assert "symbol" in entry.request_params

    def test_middleware_logs_failed_call(self):
        from utils.middleware.grpc import GRPCClientMiddleware
        
        mock_logger = MagicMock()
        
        class FailingStub:
            def PlaceOrder(self, request, **kwargs):
                raise MockGRPCError(13, "internal error")
        
        stub = FailingStub()
        middleware = GRPCClientMiddleware(stub, "order-service", logger=mock_logger)
        
        request = MockRequest()
        
        with pytest.raises(MockGRPCError):
            middleware.PlaceOrder(request)
        
        mock_logger.grpc_ext_log.assert_called_once()
        call_args = mock_logger.grpc_ext_log.call_args
        entry = call_args[0][1]
        
        assert entry.method == "PlaceOrder"
        assert entry.status_code == 13
        assert "internal error" in entry.error

    def test_middleware_disabled_does_not_log(self):
        from utils.middleware.grpc import GRPCClientMiddleware
        
        mock_logger = MagicMock()
        stub = MockStub()
        middleware = GRPCClientMiddleware(stub, "order-service", logger=mock_logger, enabled=False)
        
        request = MockRequest()
        response = middleware.PlaceOrder(request)
        
        assert response.accepted is True
        mock_logger.grpc_ext_log.assert_not_called()

    def test_middleware_without_logger_does_not_crash(self):
        from utils.middleware.grpc import GRPCClientMiddleware
        
        stub = MockStub()
        middleware = GRPCClientMiddleware(stub, "order-service", logger=None)
        
        request = MockRequest()
        response = middleware.PlaceOrder(request)
        
        assert response.accepted is True

    def test_middleware_transparent_to_client_code(self):
        from utils.middleware.grpc import GRPCClientMiddleware
        
        mock_logger = MagicMock()
        stub = MockStub()
        middleware = GRPCClientMiddleware(stub, "order-service", logger=mock_logger)
        
        request = MockRequest(order_id="test-order")
        response = middleware.GetOrder(request)
        
        assert response.order_id == "test-order"

    def test_middleware_preserves_stub_class(self):
        from utils.middleware.grpc import GRPCClientMiddleware
        
        stub = MockStub()
        middleware = GRPCClientMiddleware(stub, "order-service")
        
        assert isinstance(middleware, GRPCClientMiddleware)
        assert not isinstance(middleware, MockStub)

    def test_middleware_wraps_instance_callable_attributes(self):
        from utils.middleware.grpc import GRPCClientMiddleware

        class DynamicStub:
            def __init__(self):
                self.PlaceOrder = lambda request, **kwargs: MockResponse(accepted=True, order_id="dynamic")

        mock_logger = MagicMock()
        middleware = GRPCClientMiddleware(DynamicStub(), "order-service", logger=mock_logger)

        response = middleware.PlaceOrder(MockRequest())

        assert response.order_id == "dynamic"
        mock_logger.grpc_ext_log.assert_called_once()

    def test_logger_failure_does_not_break_rpc(self):
        from utils.middleware.grpc import GRPCClientMiddleware

        mock_logger = MagicMock()
        mock_logger.grpc_ext_log.side_effect = RuntimeError("logger unavailable")
        middleware = GRPCClientMiddleware(MockStub(), "order-service", logger=mock_logger)

        response = middleware.PlaceOrder(MockRequest())

        assert response.accepted is True

    def test_middleware_handles_callable_without_request_args(self):
        from utils.middleware.grpc import GRPCClientMiddleware

        class UtilityStub:
            def Health(self):
                return "ok"

        mock_logger = MagicMock()
        middleware = GRPCClientMiddleware(UtilityStub(), "order-service", logger=mock_logger)

        result = middleware.Health()

        assert result == "ok"
        mock_logger.grpc_ext_log.assert_called_once()
