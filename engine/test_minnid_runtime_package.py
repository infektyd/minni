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


def test_provenance_core_lives_in_runtime_package_and_remains_compat_exports():
    import minnid
    from minnid_runtime.provenance import (
        RECOVERY_ALLOWED_METHODS,
        RPC_CAPABILITY_REQUIREMENTS,
        ProvenanceResolution,
        enforce_method_capability,
        guard_vault_root,
        handler_principal,
        provenance_claim,
        recover,
        resolve_provenance,
    )

    assert minnid.RECOVERY_ALLOWED_METHODS is RECOVERY_ALLOWED_METHODS
    assert minnid._RPC_CAPABILITY_REQUIREMENTS is RPC_CAPABILITY_REQUIREMENTS
    assert minnid.ProvenanceResolution is ProvenanceResolution
    assert minnid.recover is recover
    assert minnid._provenance_claim is provenance_claim
    assert minnid.resolve_provenance is resolve_provenance
    assert minnid._handler_principal is handler_principal
    assert minnid._guard_vault_root is guard_vault_root
    assert minnid._enforce_method_capability is enforce_method_capability


def test_handoff_primitives_live_in_runtime_modules_and_remain_compat_exports():
    import minnid
    from minnid_runtime.handoff import (
        AUDIT_DETAIL_BLOCK_MAX,
        AUDIT_DETAIL_LINE_MAX,
        AUDIT_SUMMARY_MAX,
        HANDOFF_KINDS,
        agent_env_key,
        agent_vault,
        append_handoff_audit,
        compile_handoff_page,
        default_agent_vault,
        ensure_handoff_vault,
        escape_audit_details_block,
        escape_audit_field,
        iso_from_epoch,
        known_agent_vaults,
        parse_iso_ts,
        slugify,
        validate_handoff_packet,
        write_json,
    )
    from minnid_runtime.redaction import redact_text, redact_value

    assert minnid._HANDOFF_KINDS is HANDOFF_KINDS
    assert minnid._AUDIT_SUMMARY_MAX == AUDIT_SUMMARY_MAX
    assert minnid._AUDIT_DETAIL_LINE_MAX == AUDIT_DETAIL_LINE_MAX
    assert minnid._AUDIT_DETAIL_BLOCK_MAX == AUDIT_DETAIL_BLOCK_MAX
    assert minnid._redact_text is redact_text
    assert minnid._redact_value is redact_value
    assert minnid._agent_env_key is agent_env_key
    assert minnid._default_agent_vault is default_agent_vault
    assert minnid._agent_vault is agent_vault
    assert minnid._ensure_handoff_vault is ensure_handoff_vault
    assert minnid._slugify is slugify
    assert minnid._write_json is write_json
    assert minnid._parse_iso_ts is parse_iso_ts
    assert minnid._iso_from_epoch is iso_from_epoch
    assert minnid._known_agent_vaults is known_agent_vaults
    assert minnid._escape_audit_field is escape_audit_field
    assert minnid._escape_audit_details_block is escape_audit_details_block
    assert minnid._append_handoff_audit is append_handoff_audit
    assert minnid._validate_handoff_packet is validate_handoff_packet
    assert minnid._compile_handoff_page is compile_handoff_page


def test_handoff_domain_lives_in_runtime_module_and_registry_delegates():
    import minnid
    from minnid_runtime.handoff import (
        HandoffContext,
        handle_ack_handoff,
        handle_await_handoff,
        handle_daemon_handoff,
        handle_list_pending_handoffs,
        handoff_lease_status,
        iter_handoff_files,
        lease_to_agent,
        pending_handoff_leases,
        store_handoff_lease,
        update_handoff_lease_status,
        write_matching_lease_packets,
    )

    context = minnid._handoff_context()

    assert isinstance(context, HandoffContext)
    assert minnid._runtime_handle_daemon_handoff is handle_daemon_handoff
    assert minnid._runtime_handle_ack_handoff is handle_ack_handoff
    assert minnid._runtime_handle_list_pending_handoffs is handle_list_pending_handoffs
    assert minnid._runtime_handle_await_handoff is handle_await_handoff
    assert minnid._runtime_iter_handoff_files is iter_handoff_files
    assert minnid._runtime_write_matching_lease_packets is write_matching_lease_packets
    assert minnid._runtime_store_handoff_lease is store_handoff_lease
    assert minnid._runtime_update_handoff_lease_status is update_handoff_lease_status
    assert minnid._runtime_pending_handoff_leases is pending_handoff_leases
    assert minnid._runtime_handoff_lease_status is handoff_lease_status
    assert minnid._runtime_lease_to_agent is lease_to_agent
    assert minnid._METHODS["daemon.handoff"] is minnid._handle_daemon_handoff
    assert minnid._METHODS["handoff"] is minnid._handle_daemon_handoff
    assert minnid._METHODS["minni_ack_handoff"] is minnid._handle_ack_handoff
    assert minnid._METHODS["minni_list_pending_handoffs"] is minnid._handle_list_pending_handoffs
    assert minnid._METHODS["minni_await_handoff"] is minnid._handle_await_handoff
