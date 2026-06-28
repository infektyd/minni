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
