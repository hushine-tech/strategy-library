"""
gRPC client middleware for automatic logging.
"""
import time
from typing import Any, Dict, Optional

try:
    from opentelemetry import trace, propagate
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


class GRPCClientMiddleware:
    """
    Wrapper for gRPC client stubs that automatically logs all RPC calls.
    
    Usage:
        # Before
        stub = MyServiceStub(channel)
        
        # After
        stub = GRPCClientMiddleware(MyServiceStub(channel), "my-service")
        """
    
    def __init__(
        self,
        stub: Any,
        target_service: str,
        logger: Optional[Any] = None,
        enabled: bool = True,
    ):
        self._stub = stub
        self._target_service = target_service
        self._logger = logger
        self._enabled = enabled
    
    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._stub, name)
        if callable(attr):
            return _WrappedMethod(
                original_method=attr,
                middleware=self,
                method_name=name,
            )
        return attr
    
    def _log_grpc_ext(
        self,
        method: str,
        latency_ms: int,
        status_code: int,
        error: str,
        request_params: Dict[str, Any],
        response: Dict[str, Any],
    ) -> None:
        if not self._enabled or self._logger is None:
            return

        try:
            from utils.log import GRPCExtLog
        except ImportError:
            return

        try:
            entry = GRPCExtLog(
                method=method,
                target_service=self._target_service,
                latency_ms=latency_ms,
                status_code=status_code,
                error=error,
                request_params=request_params,
                response=response,
            )
            self._logger.grpc_ext_log(None, entry)
        except Exception:
            # Logging is best-effort and should never affect client calls.
            return


class _WrappedMethod:
    GRPC_STATUS_OK = 0
    GRPC_STATUS_UNKNOWN = 2
    
    def __init__(
        self,
        original_method: Any,
        middleware: GRPCClientMiddleware,
        method_name: str,
    ):
        self._original_method = original_method
        self._middleware = middleware
        self._method_name = method_name

    def _extract_status_code(self, err: Exception) -> int:
        code_attr = getattr(err, "code", None)

        try:
            grpc_code = code_attr() if callable(code_attr) else code_attr
            if grpc_code is None:
                return self.GRPC_STATUS_UNKNOWN

            value = getattr(grpc_code, "value", None)
            if isinstance(value, tuple) and value:
                return int(value[0])
            if isinstance(value, int):
                return value

            if isinstance(grpc_code, int):
                return grpc_code
        except Exception:
            return self.GRPC_STATUS_UNKNOWN

        return self.GRPC_STATUS_UNKNOWN

    def __call__(self, *args, **kwargs):
        # Track whether we injected metadata (caller may not have passed it).
        _had_metadata = "metadata" in kwargs

        # Propagate session_id as gRPC metadata.
        try:
            from utils.log.context import get_session_id
            sid = get_session_id()
            if sid:
                existing_metadata = list(kwargs.get("metadata") or [])
                existing_metadata.append(("x-session-id", sid))
                kwargs["metadata"] = existing_metadata
        except Exception:
            pass

        # Create a client span and inject trace context into metadata.
        _span_mgr = None
        if _OTEL_AVAILABLE:
            try:
                _tracer = trace.get_tracer("middleware.grpcclient")
                _span_mgr = _tracer.start_as_current_span(self._method_name)
                _span_mgr.__enter__()
                # Inject traceparent into gRPC metadata.
                carrier = {}
                propagate.inject(carrier)
                if carrier:
                    md = list(kwargs.get("metadata") or [])
                    md.extend(carrier.items())
                    kwargs["metadata"] = md
            except Exception:
                _span_mgr = None

        start_time = time.time()
        error_msg = ""
        status_code = self.GRPC_STATUS_OK
        request_params = {}
        response_data = {}
        request = args[0] if args else None

        try:
            try:
                response = self._original_method(*args, **kwargs)
            except TypeError:
                if _had_metadata or "metadata" not in kwargs:
                    raise  # TypeError not caused by our metadata injection
                # Method may not accept 'metadata' kwarg (e.g., non-gRPC utility methods).
                kwargs_no_meta = {k: v for k, v in kwargs.items() if k != "metadata"}
                response = self._original_method(*args, **kwargs_no_meta)

            status_code = self.GRPC_STATUS_OK

            if request is not None and hasattr(request, "__dict__"):
                request_params = {k: v for k, v in request.__dict__.items() if not k.startswith("_")}

            if hasattr(response, "__dict__"):
                response_data = {k: v for k, v in response.__dict__.items() if not k.startswith("_")}

            return response
        except Exception as e:  # noqa: BLE001
            error_msg = str(e)
            status_code = self._extract_status_code(e)
            raise
        finally:
            latency_ms = int((time.time() - start_time) * 1000)

            middleware = object.__getattribute__(self, "_middleware")
            middleware._log_grpc_ext(
                method=self._method_name,
                latency_ms=latency_ms,
                status_code=status_code,
                error=error_msg,
                request_params=request_params,
                response=response_data,
            )

            if _span_mgr is not None:
                try:
                    _span_mgr.__exit__(None, None, None)
                except Exception:
                    pass
