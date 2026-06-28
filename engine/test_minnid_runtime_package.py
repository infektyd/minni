import asyncio


def test_json_rpc_helpers_live_in_runtime_package_and_remain_compat_exports():
    import minnid
    from minnid_runtime.rpc import make_error, make_response

    assert minnid._make_response is make_response
    assert minnid._make_error is make_error
    assert make_response({"ok": True}, "req-1") == {
        "jsonrpc": "2.0",
        "result": {"ok": True},
        "id": "req-1",
    }
    assert make_error(-32601, "Method not found", "req-2") == {
        "jsonrpc": "2.0",
        "error": {"code": -32601, "message": "Method not found"},
        "id": "req-2",
    }


def test_transport_helpers_live_in_runtime_package_and_remain_compat_exports():
    import minnid
    from minnid_runtime.transport import SOCKET_BODY_LIMIT, parse_request

    assert minnid._SOCKET_BODY_LIMIT == SOCKET_BODY_LIMIT
    assert minnid._parse_request is parse_request
    assert parse_request(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n') == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "ping",
    }
    assert parse_request(b"") is None
    assert parse_request(b"{") is None


def test_dispatch_runtime_rejects_non_object_params_before_handler():
    from minnid_runtime.dispatch import DispatchContext, dispatch_request
    from minnid_runtime.rpc import make_error, make_response

    calls = []

    def handler(params, request_id):
        calls.append((params, request_id))
        return make_response({"called": True}, request_id)

    class Obs:
        def __init__(self):
            self.keys = []

        def incr(self, key):
            self.keys.append(key)

    class Logger:
        def exception(self, *args, **kwargs):
            raise AssertionError("logger.exception should not be called")

        def warning(self, *args, **kwargs):
            raise AssertionError("logger.warning should not be called")

    context = DispatchContext(
        methods={"echo": handler},
        recovery_allowed_methods=frozenset(),
        resolve_provenance=lambda request: type(
            "Resolved",
            (),
            {"recovery": None, "principal": None},
        )(),
        enforce_method_capability=lambda method, principal, request_id: None,
        make_error=make_error,
        make_response=make_response,
        obs=Obs(),
        logger=Logger(),
    )

    response = asyncio.run(
        dispatch_request(
            {"jsonrpc": "2.0", "id": "bad", "method": "echo", "params": []},
            context,
        )
    )

    assert response == make_error(
        -32602,
        "Invalid params: expected a JSON object",
        "bad",
    )
    assert calls == []
