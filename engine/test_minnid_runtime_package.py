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

    # Faithful main behavior: JSON-RPC `"params": []` is falsy and coerces to {}.
    response = asyncio.run(
        dispatch_request(
            {"jsonrpc": "2.0", "id": "empty-list", "method": "echo", "params": []},
            context,
        )
    )

    assert response == make_response({"called": True}, "empty-list")
    assert calls == [({}, "empty-list")]

    calls.clear()
    response = asyncio.run(
        dispatch_request(
            {"jsonrpc": "2.0", "id": "bad", "method": "echo", "params": [1, 2, 3]},
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
        ProvenanceContext,
        ProvenanceResolution,
        enforce_method_capability,
        guard_vault_root,
        handler_principal,
        provenance_claim,
        recover,
        resolve_provenance as runtime_resolve_provenance,
    )

    assert minnid.RECOVERY_ALLOWED_METHODS is RECOVERY_ALLOWED_METHODS
    assert minnid._RPC_CAPABILITY_REQUIREMENTS is RPC_CAPABILITY_REQUIREMENTS
    assert minnid.ProvenanceResolution is ProvenanceResolution
    assert minnid.recover is recover
    assert minnid._provenance_claim is provenance_claim
    assert minnid._runtime_resolve_provenance is runtime_resolve_provenance
    assert minnid._runtime_handler_principal is handler_principal
    assert minnid.ProvenanceContext is ProvenanceContext
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


def test_recall_domain_lives_in_runtime_module_and_registry_delegates():
    import minnid
    from minnid_runtime.recall import (
        RecallContext,
        anchor_for_result,
        expand_reference,
        handle_expand,
        handle_feedback,
        handle_read,
        handle_search,
        handle_sm_drill,
        handle_sm_export_pack,
        handle_trace,
        merge_document_results,
        resolve_backend,
        resolve_document_scope,
        tag_document_results,
    )

    context = minnid._recall_context()

    assert isinstance(context, RecallContext)
    assert minnid._runtime_handle_search is handle_search
    assert minnid._runtime_handle_feedback is handle_feedback
    assert minnid._runtime_handle_trace is handle_trace
    assert minnid._runtime_handle_expand is handle_expand
    assert minnid._runtime_handle_sm_drill is handle_sm_drill
    assert minnid._runtime_handle_sm_export_pack is handle_sm_export_pack
    assert minnid._runtime_handle_read is handle_read
    assert minnid._runtime_tag_document_results is tag_document_results
    assert minnid._runtime_merge_document_results is merge_document_results
    assert minnid._runtime_resolve_document_scope is resolve_document_scope
    assert minnid._runtime_resolve_backend is resolve_backend
    assert minnid._runtime_expand_reference is expand_reference
    assert minnid._runtime_anchor_for_result is anchor_for_result
    assert minnid._METHODS["search"] is minnid._handle_search
    assert minnid._METHODS["feedback"] is minnid._handle_feedback
    assert minnid._METHODS["trace"] is minnid._handle_trace
    assert minnid._METHODS["expand"] is minnid._handle_expand
    assert minnid._METHODS["sm_drill"] is minnid._handle_sm_drill
    assert minnid._METHODS["sm_export_pack"] is minnid._handle_sm_export_pack
    assert minnid._METHODS["read"] is minnid._handle_read


def test_governance_domain_lives_in_runtime_module_and_registry_delegates():
    import minnid
    from minnid_runtime.governance import (
        GovernanceContext,
        extract_assertion,
        handle_learn,
        handle_log_event,
        handle_resolve_contradiction,
        handle_subscribe_contradictions,
        list_candidates,
        resolve_candidate,
        stage_candidate,
    )

    context = minnid._governance_context()

    assert isinstance(context, GovernanceContext)
    assert minnid._runtime_handle_learn is handle_learn
    assert minnid._runtime_handle_log_event is handle_log_event
    assert minnid._runtime_handle_resolve_contradiction is handle_resolve_contradiction
    assert minnid._runtime_handle_subscribe_contradictions is handle_subscribe_contradictions
    assert minnid._runtime_stage_candidate is stage_candidate
    assert minnid._runtime_list_candidates is list_candidates
    assert minnid._runtime_resolve_candidate is resolve_candidate
    assert minnid._runtime_extract_assertion is extract_assertion
    assert minnid._METHODS["learn"] is minnid._handle_learn
    assert minnid._METHODS["resolve_contradiction"] is minnid._handle_resolve_contradiction
    assert minnid._METHODS["minni_subscribe_contradictions"] is minnid._handle_subscribe_contradictions
    assert minnid._METHODS["log_event"] is minnid._handle_log_event
    assert minnid._METHODS["stage_candidate"] is minnid._stage_candidate
    assert minnid._METHODS["list_candidates"] is minnid._list_candidates
    assert minnid._METHODS["resolve_candidate"] is minnid._resolve_candidate


def test_operational_domains_live_in_runtime_modules_and_registry_delegates():
    import minnid
    from minnid_runtime.afm import (
        AFMContext,
        afm_loop_enabled,
        afm_loop_runner,
        apply_consolidation_result,
        handle_daemon_compile,
        handle_daemon_endorse,
        mark_candidate_review,
        maybe_archive_inbox_source,
        promote_candidate_durable,
        reject_candidate_dedup,
    )
    from minnid_runtime.ax import AXContext, handle_ax_snapshot_get, handle_ax_snapshot_store
    from minnid_runtime.health import (
        HealthContext,
        faiss_cache_age_seconds,
        faiss_cache_status,
        handle_health_report,
        handle_hygiene_report,
        handle_status,
    )
    from minnid_runtime.vault_index import (
        MAX_VAULT_PAGE_CHARS,
        VaultIndexContext,
        handle_vault_index_doc,
    )

    assert isinstance(minnid._health_context(), HealthContext)
    assert minnid._runtime_handle_status is handle_status
    assert minnid._runtime_handle_health_report is handle_health_report
    assert minnid._runtime_handle_hygiene_report is handle_hygiene_report
    assert minnid._runtime_faiss_cache_status is faiss_cache_status
    assert minnid._runtime_faiss_cache_age_seconds is faiss_cache_age_seconds
    assert minnid._METHODS["status"] is minnid._handle_status
    assert minnid._METHODS["health_report"] is minnid._handle_health_report
    assert minnid._METHODS["hygiene_report"] is minnid._handle_hygiene_report

    assert isinstance(minnid._afm_context(), AFMContext)
    assert minnid._runtime_afm_loop_enabled is afm_loop_enabled
    assert minnid._runtime_afm_loop_runner is afm_loop_runner
    assert minnid._runtime_apply_consolidation_result is apply_consolidation_result
    assert minnid._runtime_handle_daemon_compile is handle_daemon_compile
    assert minnid._runtime_handle_daemon_endorse is handle_daemon_endorse
    assert minnid._runtime_maybe_archive_inbox_source is maybe_archive_inbox_source
    assert minnid._runtime_promote_candidate_durable is promote_candidate_durable
    assert minnid._runtime_reject_candidate_dedup is reject_candidate_dedup
    assert minnid._runtime_mark_candidate_review is mark_candidate_review
    assert minnid._METHODS["daemon.compile"] is minnid._handle_daemon_compile
    assert minnid._METHODS["daemon.endorse"] is minnid._handle_daemon_endorse

    assert isinstance(minnid._ax_context(), AXContext)
    assert minnid._runtime_handle_ax_snapshot_store is handle_ax_snapshot_store
    assert minnid._runtime_handle_ax_snapshot_get is handle_ax_snapshot_get
    assert minnid._METHODS["ax_snapshot_store"] is minnid._handle_ax_snapshot_store
    assert minnid._METHODS["ax_snapshot_get"] is minnid._handle_ax_snapshot_get

    assert isinstance(minnid._vault_index_context(), VaultIndexContext)
    assert minnid._MAX_VAULT_PAGE_CHARS == MAX_VAULT_PAGE_CHARS
    assert minnid._runtime_handle_vault_index_doc is handle_vault_index_doc
    assert minnid._METHODS["vault_index_doc"] is minnid._handle_vault_index_doc
